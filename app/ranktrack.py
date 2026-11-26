"""Phase 6: free local rank tracking (geo-grid), with Google's own Places API (New).

How it stays free: every grid search asks Text Search for place IDs only
(field mask `places.id,nextPageToken`). Google bills that as "Text Search
Essentials (IDs Only)", which its pricing page lists with an unlimited free cap.
Business names are only fetched for a handful of top competitors and for
rank_find_business, with Place Details / Text Search Pro fields, which have a
free monthly cap (5,000 each). Nothing here uses Enterprise fields.

What it measures: from each point of a grid around the business, the order
Google's Places search returns businesses for the keyword with the search
biased to that point. That's Google's local ranking for a searcher there; it's
close to Google Maps, though the Maps app can differ a little (personalisation,
ads, zoom level).
"""

from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any

import requests
from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import AdsError
from .config import Settings
from .writes import Writer

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
BASE = "https://places.googleapis.com/v1/"
FREE_MASK = "places.id,nextPageToken"  # Text Search Essentials (IDs Only): unlimited free
NAME_MASK = "places.id,places.displayName,places.formattedAddress,places.location,places.primaryTypeDisplayName"
DETAIL_MASK = "id,displayName,formattedAddress"
MAX_KEYWORDS = 5
WORKERS = 6
_LATLNG = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

SETUP = ("The rank tracker needs a free Google Maps Platform API key. In the Zyppia Ads Connector Cloud project: "
         "1) enable Places API (New) at https://console.cloud.google.com/apis/library/places.googleapis.com, "
         "2) create an API key at https://console.cloud.google.com/apis/credentials restricted to 'Places API (New)', "
         "3) store it as the secret zyppia-places-api-key and expose it to Cloud Run as PLACES_API_KEY "
         "(docs/DEPLOY.md, Phase 6, has copy-paste steps).")


def place_id(ref: str | None) -> str | None:
    if not ref:
        return None
    return str(ref).strip().removeprefix("places/") or None


def grid_points(lat: float, lng: float, size: int, spacing_km: float) -> list[tuple[int, int, float, float]]:
    """size x size points centred on (lat, lng), spacing_km apart. Row 0 is the north edge, column 0 the west."""
    half = size // 2
    dlat = spacing_km / 110.574
    dlng = spacing_km / (111.320 * max(math.cos(math.radians(lat)), 0.01))
    return [(r, c, round(lat + (half - r) * dlat, 6), round(lng + (c - half) * dlng, 6))
            for r in range(size) for c in range(size)]


def rank_text(rank: int | None, depth: int) -> str:
    return str(rank) if rank else f"{depth}+"


class Places:
    """Places API (New) with a server API key (no Google login involved)."""

    def __init__(self, settings: Settings):
        self.key = settings.places_api_key
        self.http = requests.Session()
        self.calls = {"free_id_searches": 0, "pro_lookups": 0}

    def _req(self, method: str, path: str, mask: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.key:
            raise AdsError(SETUP)
        headers = {"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": mask}
        r = None
        for attempt in range(5):
            r = self.http.request(method, BASE + path, headers=headers, json=body, timeout=30)
            if (r.status_code == 429 or r.status_code >= 500) and attempt < 4:
                time.sleep(1.5 * 2 ** attempt)
                continue
            break
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or {}
                msg, status = err.get("message") or r.text[:300], err.get("status") or ""
            except ValueError:
                msg, status = r.text[:300], ""
            low = msg.lower()
            if "api key not valid" in low or "api_key_invalid" in low:
                raise AdsError("The Places API key isn't valid. " + SETUP)
            if "has not been used" in low or "is disabled" in low or "service_disabled" in low:
                raise AdsError("Places API (New) is turned off in the Cloud project. Enable it at "
                               "https://console.cloud.google.com/apis/library/places.googleapis.com and try again.")
            if "billing" in low:
                raise AdsError("Google Maps Platform needs a billing account linked to the Cloud project, even for the "
                               "free calls this uses (the IDs-only searches are free without limit).")
            if r.status_code == 429:
                raise AdsError("Places API says too many requests. Wait a minute, or scan a smaller grid.")
            raise AdsError(f"Places API error ({r.status_code} {status}): {msg}")
        return r.json() if r.content else {}

    def ranked_ids(self, query: str, lat: float, lng: float, radius_m: float, depth: int,
                   language: str | None, region: str | None) -> list[str]:
        """Place IDs in Google's order for this query searched from (lat, lng). Free: IDs-only field mask."""
        body: dict[str, Any] = {"textQuery": query, "pageSize": 20, "rankPreference": "RELEVANCE",
                                "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                                            "radius": float(radius_m)}}}
        if language:
            body["languageCode"] = language
        if region:
            body["regionCode"] = region
        out: list[str] = []
        while len(out) < depth:
            resp = self._req("POST", "places:searchText", FREE_MASK, body)
            self.calls["free_id_searches"] += 1
            out += [p["id"] for p in resp.get("places") or [] if p.get("id")]
            token = resp.get("nextPageToken")
            if not token:
                break
            body = {**body, "pageToken": token}
        return out[:depth]

    def find(self, query: str, lat: float | None, lng: float | None, language: str | None,
             region: str | None, limit: int = 5) -> list[dict[str, Any]]:
        """Businesses matching a name/address, with names (Text Search Pro: 5,000 free a month)."""
        body: dict[str, Any] = {"textQuery": query, "pageSize": limit}
        if lat is not None and lng is not None:
            body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": 20000.0}}
        if language:
            body["languageCode"] = language
        if region:
            body["regionCode"] = region
        resp = self._req("POST", "places:searchText", NAME_MASK, body)
        self.calls["pro_lookups"] += 1
        return [{"place_id": p.get("id"), "name": (p.get("displayName") or {}).get("text"),
                 "address": p.get("formattedAddress"), "category": (p.get("primaryTypeDisplayName") or {}).get("text"),
                 "lat": (p.get("location") or {}).get("latitude"), "lng": (p.get("location") or {}).get("longitude")}
                for p in resp.get("places") or []]

    def name_of(self, pid: str) -> dict[str, Any]:
        p = self._req("GET", f"places/{pid}", DETAIL_MASK)
        self.calls["pro_lookups"] += 1
        return {"name": (p.get("displayName") or {}).get("text"), "address": p.get("formattedAddress")}


def scan_keyword(places: Places, target: str, query: str, pts: list[tuple[int, int, float, float]], depth: int,
                 radius_m: float, language: str | None, region: str | None) -> dict[str, Any]:
    def one(pt):
        r, c, lat, lng = pt
        try:
            ids = places.ranked_ids(query, lat, lng, radius_m, depth, language, region)
            return pt, ids, None
        except AdsError as e:
            return pt, [], str(e)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(one, pts))
    errors = [e for _, _, e in results if e]
    if errors and len(errors) == len(results):
        raise AdsError(errors[0])
    size = int(math.isqrt(len(pts)))
    grid: list[list[int | None]] = [[None] * size for _ in range(size)]
    points = []
    comp_ranks: dict[str, list[int]] = defaultdict(list)
    for (r, c, lat, lng), ids, err in results:
        rank = ids.index(target) + 1 if target in ids else None
        grid[r][c] = rank
        points.append({"row": r, "col": c, "lat": lat, "lng": lng, "rank": rank, **({"error": err} if err else {})})
        for i, pid in enumerate(ids):
            if pid != target:
                comp_ranks[pid].append(i + 1)
    ok = [p for p in points if "error" not in p]
    found = [p["rank"] for p in ok if p["rank"]]
    n = len(ok) or 1
    summary = {
        "keyword": query,
        "found_at": f"{len(found)} of {len(ok)} points (top {depth})",
        "top3_share": f"{sum(1 for x in found if x <= 3) / n * 100:.0f}%",  # share of local voice
        "top10_share": f"{sum(1 for x in found if x <= 10) / n * 100:.0f}%",
        "average_rank_where_found": round(sum(found) / len(found), 1) if found else None,
        "average_rank_all_points": round(sum((p["rank"] or depth + 1) for p in ok) / n, 1),
        "centre_rank": rank_text(grid[size // 2][size // 2], depth),
        "grid": [" ".join(f"{rank_text(x, depth):>3}" for x in row) for row in grid],
    }
    if errors:
        summary["points_failed"] = f"{len(errors)} (e.g. {errors[0]})"
    competitors = sorted(comp_ranks.items(), key=lambda kv: (-sum(1 for x in kv[1] if x <= 3),
                                                             sum(kv[1]) / len(kv[1])))
    return {"summary": summary, "points": points,
            "competitors": [{"place_id": pid, "top3_points": sum(1 for x in rs if x <= 3), "seen_at_points": len(rs),
                             "average_rank": round(sum(rs) / len(rs), 1)} for pid, rs in competitors]}


class RankTracker:
    def __init__(self, settings: Settings, gbp=None):
        self.settings, self.gbp = settings, gbp

    def places(self) -> Places:
        return Places(self.settings)

    def target(self, grt: str, places: Places, location: str | None, pid: str | None, center: str | None,
               language: str | None, region: str | None) -> tuple[str, str | None, float, float]:
        name = None
        lat = lng = None
        if location:
            if self.gbp is None:
                raise AdsError("Business Profile isn't set up on this server; give place_id and center instead.")
            _, loc = self.gbp.find(grt, location)
            md = loc.get("metadata") or {}
            pid = pid or md.get("placeId")
            name = loc.get("title")
            ll = loc.get("latlng") or {}
            lat, lng = ll.get("latitude"), ll.get("longitude")
            if not pid:
                raise AdsError(f"Google hasn't given '{name}' a place ID yet (new or unverified profile), so it can't "
                               "be ranked. Use rank_find_business to look it up on Maps.")
        pid = place_id(pid)
        if not pid:
            raise AdsError("Say which business to track: location (a Business Profile you manage) or place_id "
                           "(rank_find_business finds any business's place ID).")
        if center:
            m = _LATLNG.match(center)
            if m:
                lat, lng = float(m.group(1)), float(m.group(2))
            else:
                hits = places.find(center, None, None, language, region, 1)
                if not hits:
                    raise AdsError(f"Couldn't find '{center}' on the map. Give the centre as 'lat,lng'.")
                lat, lng = hits[0]["lat"], hits[0]["lng"]
        if lat is None or lng is None:
            info = places._req("GET", f"places/{pid}", "id,displayName,location")
            places.calls["pro_lookups"] += 1
            name = name or (info.get("displayName") or {}).get("text")
            lat, lng = (info.get("location") or {}).get("latitude"), (info.get("location") or {}).get("longitude")
            if lat is None:
                raise AdsError("This business has no map pin (service-area business). Give center as 'lat,lng' or a "
                               "suburb/city name.")
        return pid, name, float(lat), float(lng)

    def scan(self, grt: str, a: dict[str, Any]) -> dict[str, Any]:
        keywords = [k.strip() for k in a["keywords"] if k and k.strip()]
        if not keywords:
            raise AdsError("Give at least one keyword, e.g. 'plumber' or 'emergency plumber dubai marina'.")
        if len(keywords) > MAX_KEYWORDS:
            raise AdsError(f"At most {MAX_KEYWORDS} keywords per scan (split into several scans).")
        size = a["grid_size"]
        if size % 2 == 0:
            raise AdsError("grid_size must be odd (3, 5, 7, 9...) so the business sits in the middle.")
        places = self.places()
        pid, name, lat, lng = self.target(grt, places, a.get("location"), a.get("place_id"), a.get("center"),
                                          a.get("language"), a.get("region"))
        depth = a["depth"]
        spacing = a["spacing_km"]
        radius = a.get("search_radius_m") or max(200.0, min(spacing * 500.0, 5000.0))
        pts = grid_points(lat, lng, size, spacing)
        out: dict[str, Any] = {
            "business": name, "place_id": pid, "centre": {"lat": lat, "lng": lng},
            "grid": f"{size}x{size} points, {spacing} km apart ({spacing * (size - 1):g} km across)",
            "how_to_read": ("Each grid row is north (first) to south (last), west to east. Numbers are the business's "
                            f"position in Google's local results for a searcher at that point; '{depth}+' means not in "
                            f"the top {depth}. top3_share is the share of points where it's in the local 3-pack."),
            "keywords": [],
        }
        all_comp: dict[str, dict[str, Any]] = {}
        for kw in keywords:
            res = scan_keyword(places, pid, kw, pts, depth, radius, a.get("language"), a.get("region"))
            entry = {**res["summary"]}
            if a.get("include_points"):
                entry["points"] = res["points"]
            top = res["competitors"][: a["competitors"]]
            entry["top_competitors"] = top
            for c in top:
                all_comp.setdefault(c["place_id"], {})
            out["keywords"].append(entry)
        if a["competitors"] and a.get("competitor_names", True):
            names = {}
            for cid in list(all_comp)[:15]:  # a few Pro lookups per scan at most (5,000 free a month)
                try:
                    names[cid] = places.name_of(cid)
                except AdsError:
                    pass
            for entry in out["keywords"]:
                for c in entry["top_competitors"]:
                    c.update(names.get(c["place_id"], {}))
        out["api_calls"] = {**places.calls, "cost": "free: IDs-only searches have no cap; name lookups use the "
                                                    "5,000/month free Pro allowance"}
        out["tip"] = ("Show the grid as a heatmap (green 1-3, yellow 4-10, red 11+). Re-run the same scan (same "
                      "keywords, grid_size, spacing_km) every 2-4 weeks to see movement. For the profile itself, run "
                      "gbp_profile_audit.")
        return out


def register_rank_tools(mcp: FastMCP, w: Writer, settings: Settings, gbp=None) -> RankTracker:
    rt = RankTracker(settings, gbp)

    @mcp.tool(annotations=READ_ONLY)
    async def rank_grid_scan(
        keywords: Annotated[list[str], Field(description="1-5 searches, e.g. ['plumber', 'emergency plumber'].")],
        location: Annotated[str | None, Field(description="A Business Profile you manage (location_id or name).")] = None,
        place_id: Annotated[str | None, Field(description="Or any business's Google place ID (rank_find_business).")] = None,
        center: Annotated[str | None, Field(description="Grid centre as 'lat,lng' or a place name. Default: the business.")] = None,
        grid_size: Annotated[int, Field(ge=3, le=9, description="Points per side, odd: 5 = 25 points, 7 = 49, 9 = 81.")] = 7,
        spacing_km: Annotated[float, Field(ge=0.1, le=10, description="Distance between points. Dense city: 0.5-1; suburbs: 1-2; regional: 3-5.")] = 1.0,
        depth: Annotated[int, Field(description="How deep to look: 20, 40 or 60 results.")] = 20,
        competitors: Annotated[int, Field(ge=0, le=10, description="How many top competitors to list per keyword.")] = 5,
        competitor_names: Annotated[bool, Field(description="Look up competitor names (uses a few free Pro lookups).")] = True,
        language: Annotated[str | None, Field(description="e.g. en, ar.")] = None,
        region: Annotated[str | None, Field(description="Country code, e.g. AE, US, GB.")] = None,
        search_radius_m: Annotated[float | None, Field(ge=50, le=50000, description="How tightly each search is tied to its point. Default: half the spacing.")] = None,
        include_points: Annotated[bool, Field(description="Also return every point's coordinates and rank (for a map).")] = False,
    ) -> dict[str, Any]:
        """Free local rank tracking: a geo-grid heatmap of where a business ranks in Google's local results for
        each keyword, from every point of a grid around it, with share of the 3-pack, average rank and the
        competitors that beat it. Uses Google's Places API with free IDs-only searches."""
        if depth not in (20, 40, 60):
            raise ValueError("depth is 20, 40 or 60.")
        a = dict(keywords=keywords, location=location, place_id=place_id, center=center, grid_size=grid_size,
                 spacing_km=spacing_km, depth=depth, competitors=competitors, competitor_names=competitor_names,
                 language=language, region=region, search_radius_m=search_radius_m, include_points=include_points)
        return await w.run("rank_grid_scan", None, lambda user, grt: rt.scan(grt, a))

    @mcp.tool(annotations=READ_ONLY)
    async def rank_find_business(
        query: Annotated[str, Field(description="Business name and area, e.g. 'Al Mansoori Plumbing Dubai Marina'.")],
        near: Annotated[str | None, Field(description="Optional 'lat,lng' to search near.")] = None,
        region: Annotated[str | None, Field(description="Country code, e.g. AE.")] = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Find any business on Google Maps (a client, a prospect or a competitor) and get its place ID, address,
        category and map pin, for rank_grid_scan."""
        lat = lng = None
        if near:
            m = _LATLNG.match(near)
            if not m:
                raise ValueError("near is 'lat,lng'.")
            lat, lng = float(m.group(1)), float(m.group(2))

        def go(user, grt):
            p = rt.places()
            return {"results": p.find(query, lat, lng, language, region, 5)}
        return await w.run("rank_find_business", None, go)

    return rt
