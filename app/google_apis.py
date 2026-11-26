"""Phase 5: one gateway for the other Google APIs (Analytics, Search Console, Merchant Center).

Every call uses the signed-in person's own Google login, like Google Ads and
Tag Manager. These APIs have no "check only" mode, so a change tool builds a
list of steps (small dicts) that is sealed into the same confirm token as other
changes; apply_change runs them in order.

A step:
  a: which API (see APIS), m: HTTP method, p: path (with version), q: query
  params, b: JSON body, d: what it does (shown to the person),
  s/f: save field f of the response as ${s} for later steps,
  u: the step that reverses it (filled in after it runs, so it can use ${s}),
  n: why it can't be undone (when there's no u).
"""

from __future__ import annotations

import re
import time
from typing import Any

from google.auth.transport.requests import AuthorizedSession

from .ads import AdsError, AdsGateway
from .writes import Change, PartialChange

# key: (base URL, product name people know, API name, service to enable)
APIS: dict[str, tuple[str, str, str, str]] = {
    "ga_admin": ("https://analyticsadmin.googleapis.com/", "Google Analytics", "Google Analytics Admin API",
                 "analyticsadmin.googleapis.com"),
    "ga_data": ("https://analyticsdata.googleapis.com/", "Google Analytics", "Google Analytics Data API",
                "analyticsdata.googleapis.com"),
    "gsc": ("https://searchconsole.googleapis.com/", "Search Console", "Google Search Console API",
            "searchconsole.googleapis.com"),
    "merchant": ("https://merchantapi.googleapis.com/", "Merchant Center", "Merchant API", "merchantapi.googleapis.com"),
}
_PATH_RE = re.compile(r"^[A-Za-z0-9_\-.~%:/]+$")
_PLACEHOLDER = re.compile(r"\$\{([a-z_]+)\}")


def check_path(path: str) -> str:
    path = str(path or "").strip().lstrip("/")
    if not path or ".." in path or "//" in path or not _PATH_RE.match(path):
        raise AdsError(f"'{path}' isn't a valid API path (e.g. v1beta/properties/123).")
    return path


def fill(value: Any, ctx: dict[str, Any]) -> Any:
    """Fill ${name} placeholders from ctx (results of earlier steps)."""
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in ctx:
                raise AdsError(f"Internal: no value for ${{{m.group(1)}}}.")
            return str(ctx[m.group(1)])
        return _PLACEHOLDER.sub(sub, value)
    if isinstance(value, list):
        return [fill(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, ctx) for k, v in value.items()}
    return value


def dig(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        obj = (obj or {}).get(part) if isinstance(obj, dict) else None
    return obj


def explain(api: str, status: int, msg: str) -> str:
    _, product, api_name, service = APIS[api]
    low = msg.lower()
    if status in (401, 403) and ("scope" in low or "insufficient" in low):
        return (f"Your sign-in doesn't include {product} yet. In Claude, go to Settings > Connectors > Zyppia Google "
                f"Ads, click Disconnect, then Connect again and tick every box (including {product}).")
    if status == 403 and ("has not been used" in low or "is disabled" in low or "not been enabled" in low
                          or "service_disabled" in low):
        return (f"The {api_name} is turned off in the Google Cloud project. Turn it on at "
                f"https://console.cloud.google.com/apis/library/{service} (click Enable), or run the update "
                "script (scripts/update.sh), which turns on every API the connector needs. Then try again.")
    if api == "merchant" and ("not registered" in low or "developer registration" in low or "registergcp" in low):
        return ("Merchant Center needs this connector's Google Cloud project registered once. Run "
                "merchant_register_project with any Merchant Center account ID, apply it, then try again.")
    if status == 403:
        return f"{product} refused: {msg} (your Google login may not have access, or needs Edit/Admin rights there)."
    if status == 404:
        return f"{product} couldn't find that: {msg}"
    if status == 429:
        return f"{product} says too many requests right now. Wait a minute and try again."
    return f"{product} error ({status}): {msg}"


class GoogleApis:
    """Calls Analytics, Search Console and Merchant Center with the signed-in person's own Google login."""

    def __init__(self, ads: AdsGateway):
        self.ads = ads

    def call(self, grt: str, api: str, method: str, path: str, params: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None) -> dict[str, Any]:
        if api not in APIS:
            raise AdsError(f"Unknown API '{api}'.")
        url = APIS[api][0] + check_path(path)
        session = AuthorizedSession(self.ads._credentials(grt))
        r = None
        for attempt in range(4):
            r = session.request(method, url, params=params, json=body, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * 2 ** attempt)
                continue
            break
        if r.status_code >= 400:
            try:
                msg = (r.json().get("error") or {}).get("message") or r.text[:300]
            except ValueError:
                msg = r.text[:300]
            raise AdsError(explain(api, r.status_code, msg))
        return r.json() if r.content else {}

    def list_all(self, grt: str, api: str, path: str, key: str, params: dict[str, Any] | None = None,
                 limit: int = 5000) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token = None
        while True:
            q = dict(params or {})
            if token:
                q["pageToken"] = token
            resp = self.call(grt, api, "GET", path, params=q or None)
            out += resp.get(key) or []
            token = resp.get("nextPageToken")
            if not token or len(out) >= limit:
                return out[:limit]

    # ---- running a sealed change

    def execute(self, grt: str, steps: list[dict[str, Any]], ctx: dict[str, Any]) -> tuple[list[str], list[dict], set[str]]:
        results: list[str] = []
        undo: list[dict[str, Any]] = []
        notes: set[str] = set()
        for i, raw in enumerate(steps):
            product = APIS.get(raw.get("a"), ("", "Google"))[1]
            try:
                st = fill({k: v for k, v in raw.items() if k != "u"}, ctx)
                resp = self.call(grt, st["a"], st["m"], st["p"], params=st.get("q"), body=st.get("b"))
            except AdsError as e:
                raise PartialChange(f"{product} step {i + 1} ({raw.get('d')}) failed: {e}",
                                    results, list(reversed(undo)), notes) from None
            if st.get("s"):
                ctx[st["s"]] = dig(resp, st["f"])
            results.append(f"{product}: {st['d']}" + (f" ({resp['name']})" if isinstance(resp.get("name"), str) else ""))
            if raw.get("u"):
                try:
                    undo.append(fill(raw["u"], ctx))
                except AdsError:
                    notes.add(f"{product} step '{st['d']}'")
            else:
                notes.add(raw.get("n") or f"{product} step '{st['d']}'")
        return results, list(reversed(undo)), notes


def raw_change(api: str, product: str, calls: list[dict[str, Any]], description: str) -> Change:
    """Raw API calls for anything the dedicated tools don't cover. Always needs extra confirmation."""
    if not calls:
        raise AdsError("Give at least one call.")
    if len(calls) > 20:
        raise AdsError("At most 20 calls at once.")
    ch = Change(None, description.strip() or f"Advanced {product} change", where=product)
    for c in calls:
        m = str(c.get("method") or "").upper()
        if m not in ("POST", "PATCH", "PUT", "DELETE"):
            raise AdsError("Each call needs method POST, PATCH, PUT or DELETE (reads have their own tools).")
        p = check_path(c.get("path"))
        step = {"a": api, "m": m, "p": p, "d": f"{m} {p}", "n": f"advanced {product} call {m} {p}"}
        if c.get("body") is not None:
            step["b"] = c["body"]
        if c.get("params"):
            step["q"] = c["params"]
        ch.gtm.append(step)
        ch.lines.append(f"{m} {p}" + (f" with {c['body']}" if c.get("body") is not None else ""))
    ch.large.append(f"advanced {product} API calls aren't checked by the connector and can't be undone automatically")
    return ch
