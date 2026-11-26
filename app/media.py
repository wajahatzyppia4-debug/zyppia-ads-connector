"""Phase 5: images, videos and text assets for Performance Max and Display ads.

Images can be given as a public web address or as an existing image asset ID
from the account's asset library. Images from web addresses are downloaded
(public addresses only, size limited) and checked for type, size and shape
before Google sees them.

The image bytes are too big to seal into a confirm token, so the token only
keeps each image's address and fingerprint (SHA-256). apply_change downloads
the image again and refuses if it changed since the preview (see Writer).
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from .ads import Account, AdsError
from .research import USER_AGENT, check_url

MAX_IMAGE_BYTES = 5_120 * 1024  # Google's limit for image assets

# kind -> (Google field type, aspect ratio width/height, min width, min height, plain name)
IMAGE_KINDS: dict[str, tuple[str, float, int, int, str]] = {
    "landscape": ("MARKETING_IMAGE", 1.91, 600, 314, "landscape image (1.91:1, at least 600x314, e.g. 1200x628)"),
    "square": ("SQUARE_MARKETING_IMAGE", 1.0, 300, 300, "square image (1:1, at least 300x300, e.g. 1200x1200)"),
    "portrait": ("PORTRAIT_MARKETING_IMAGE", 0.8, 480, 600, "portrait image (4:5, at least 480x600, e.g. 960x1200)"),
    "logo": ("LOGO", 1.0, 128, 128, "square logo (1:1, at least 128x128, e.g. 1200x1200)"),
    "landscape_logo": ("LANDSCAPE_LOGO", 4.0, 512, 128, "wide logo (4:1, at least 512x128, e.g. 1200x300)"),
}
RATIO_TOLERANCE = 0.02

_YOUTUBE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def image_info(data: bytes) -> tuple[str, int, int]:
    """(mime type, width, height) of a JPEG, PNG or GIF. Raises for anything else."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "image/png", w, h
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return "image/gif", w, h
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "image/jpeg", w, h
            i += 2 + seg
    raise AdsError("Images must be JPEG, PNG or GIF files.")


def fetch_image(url: str, timeout: float = 15.0) -> bytes:
    """Download an image from a public address (redirects checked, 5 MB limit)."""
    current = check_url(url)
    with httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as http:
        for _ in range(5):
            with http.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = check_url(urljoin(current, resp.headers["location"]))
                    continue
                if resp.status_code >= 400:
                    raise AdsError(f"The image address answered with error {resp.status_code}: {url}")
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_IMAGE_BYTES:
                        raise AdsError(f"The image is bigger than Google's 5 MB limit: {url}")
                return body
    raise AdsError(f"Too many redirects for the image: {url}")


def check_image(data: bytes, kind: str, where: str) -> tuple[str, int, int]:
    """Check an image fits its slot. Returns (mime, width, height)."""
    field, ratio, min_w, min_h, plain = IMAGE_KINDS[kind]
    if len(data) > MAX_IMAGE_BYTES:
        raise AdsError(f"{where} is bigger than Google's 5 MB limit.")
    mime, w, h = image_info(data)
    if not h or abs(w / h - ratio) / ratio > RATIO_TOLERANCE:
        raise AdsError(f"{where} is {w}x{h}, which isn't the right shape for a {plain}.")
    if w < min_w or h < min_h:
        raise AdsError(f"{where} is {w}x{h}, which is too small for a {plain}.")
    return mime, w, h


def youtube_id(value: str) -> str:
    v = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", v):
        return v
    m = _YOUTUBE.search(v)
    if not m:
        raise AdsError(f"'{value}' isn't a YouTube video link or ID.")
    return m.group(1)


def text(value: Any, limit: int, what: str) -> str:
    t = " ".join(str(value or "").split())
    if not t:
        raise AdsError(f"Empty {what}.")
    if len(t) > limit:
        raise AdsError(f"{what.capitalize()} too long ({len(t)}/{limit} characters): '{t}'.")
    return t


def texts(values: list[Any] | None, limit: int, what: str, lo: int, hi: int) -> list[str]:
    out = [text(v, limit, what) for v in values or []]
    if not lo <= len(out) <= hi:
        raise AdsError(f"Give {lo} to {hi} {what}s (got {len(out)}).")
    if len({o.lower() for o in out}) != len(out):
        raise AdsError(f"The {what}s must all be different.")
    return out


def https_url(value: Any, what: str = "final_url") -> str:
    url = str(value or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise AdsError(f"The {what} must start with https://")
    return url


@dataclass
class Made:
    rn: str  # resource name of the asset (temporary for new ones)
    label: str


class Creative:
    """Builds asset operations into a Change and remembers which ones carry downloaded images."""

    def __init__(self, w, acct: Account, ch, first_temp: int = -100):
        self.w, self.acct, self.ch = w, acct, ch
        self.next = first_temp
        self.cache: dict[tuple[str, str], Made] = {}

    def temp(self, kind: str) -> str:
        rn = self.w.rn(self.acct, kind, self.next)
        self.next -= 1
        return rn

    def text_asset(self, value: str) -> str:
        key = ("text", value)
        if key not in self.cache:
            op = self.w.op()
            a = op.asset_operation.create
            a.resource_name = self.temp("assets")
            a.text_asset.text = value
            self.ch.ops.append(op)
            self.cache[key] = Made(a.resource_name, value)
        return self.cache[key].rn

    def image_asset(self, ref: Any, kind: str, existing: dict[str, dict[str, Any]] | None = None) -> Made:
        """ref is a web address or an existing image asset ID."""
        s = str(ref or "").strip()
        if not s:
            raise AdsError("Empty image.")
        key = ("image:" + kind, s)
        if key in self.cache:
            return self.cache[key]
        plain = IMAGE_KINDS[kind][4]
        if re.fullmatch(r"(asset[:/ ]?)?\d+", s, re.I) or s.startswith("customers/"):
            aid = re.sub(r"\D", "", s.rsplit("/", 1)[-1])
            info = (existing or {}).get(aid)
            if info is None:
                raise AdsError(f"Image asset {aid} isn't in this account's asset library.")
            w, h = info.get("width") or 0, info.get("height") or 0
            ratio = IMAGE_KINDS[kind][1]
            if w and h and abs(w / h - ratio) / ratio > RATIO_TOLERANCE:
                raise AdsError(f"Image asset {aid} is {w}x{h}, which isn't the right shape for a {plain}.")
            made = Made(self.w.rn(self.acct, "assets", aid), f"existing image asset {aid} ({w}x{h})")
        else:
            url = https_url(s, "image address")
            data = self.w.image_bytes(url)
            mime, w, h = check_image(data, kind, f"The image at {url}")
            op = self.w.op()
            a = op.asset_operation.create
            a.resource_name = self.temp("assets")
            a.name = f"{kind.replace('_', ' ')} {hashlib.sha256(data).hexdigest()[:10]} ({w}x{h})"
            a.type_ = self.w.t.enums.AssetTypeEnum.IMAGE
            a.image_asset.data = data
            self.ch.ops.append(op)
            self.ch.extra.setdefault("img", []).append([len(self.ch.ops) - 1, url, hashlib.sha256(data).hexdigest()])
            made = Made(a.resource_name, f"new image from {url} ({w}x{h}, {mime.split('/')[1]})")
        self.cache[key] = made
        return made

    def video_asset(self, ref: Any) -> Made:
        vid = youtube_id(ref)
        key = ("video", vid)
        if key not in self.cache:
            op = self.w.op()
            a = op.asset_operation.create
            a.resource_name = self.temp("assets")
            a.name = f"YouTube {vid}"
            a.youtube_video_asset.youtube_video_id = vid
            self.ch.ops.append(op)
            self.cache[key] = Made(a.resource_name, f"YouTube video {vid}")
        return self.cache[key]


def existing_images(w, grt: str, acct: Account, refs: list[Any]) -> dict[str, dict[str, Any]]:
    """Look up image assets given by ID: {asset id: {width, height}}."""
    ids = sorted({re.sub(r"\D", "", str(r).rsplit("/", 1)[-1]) for r in refs
                  if re.fullmatch(r"(asset[:/ ]?)?\d+", str(r).strip(), re.I) or str(r).startswith("customers/")})
    ids = [i for i in ids if i]
    if not ids:
        return {}
    rows = w.rows(grt, acct, "SELECT asset.id, asset.image_asset.full_size.width_pixels, "
                             "asset.image_asset.full_size.height_pixels FROM asset "
                             f"WHERE asset.type = 'IMAGE' AND asset.id IN ({', '.join(ids)})")
    return {str(r["asset.id"]): {"width": r.get("asset.image_asset.full_size.width_pixels"),
                                 "height": r.get("asset.image_asset.full_size.height_pixels")} for r in rows}
