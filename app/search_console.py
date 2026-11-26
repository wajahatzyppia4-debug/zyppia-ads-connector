"""Phase 5: Google Search Console (read-only): the real Google searches that bring people to a client's
site, how those change over time, keyword opportunities, URL inspection and sitemaps.

Useful for keyword research while Keyword Planner waits for Basic API access.
Search Console data is 2-3 days behind, so date ranges end 3 days ago by default.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlparse

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import AdsError
from .google_apis import GoogleApis
from .writes import Writer

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DIMENSIONS = ("query", "page", "country", "device", "date", "searchAppearance")
LAG_DAYS = 3


def _enc(site: str) -> str:
    return quote(site, safe="")


def _host(site: str) -> str:
    s = site.removeprefix("sc-domain:")
    return (urlparse(s).hostname or s).lower().removeprefix("www.").strip("/")


def period(days: int, start: str | None, end: str | None) -> tuple[date, date]:
    if start or end:
        try:
            s, e = date.fromisoformat(start or ""), date.fromisoformat(end or "")
        except ValueError:
            raise AdsError("Give both start_date and end_date as YYYY-MM-DD.") from None
        if s > e:
            raise AdsError("start_date is after end_date.")
        return s, e
    e = date.today() - timedelta(days=LAG_DAYS)
    return e - timedelta(days=days - 1), e


def opportunities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Searches with plenty of impressions where the site is on page 1-2 but gets few clicks:
    good candidates for Google Ads keywords or better page titles."""
    out = []
    for r in rows:
        if r.get("impressions", 0) >= 50 and 4 <= r.get("position", 0) <= 20 and r.get("ctr", 0) < 0.03:
            out.append({**r, "why": f"seen {r['impressions']} times at position {r['position']}, only "
                                    f"{r['ctr'] * 100:.1f}% click"})
    return sorted(out, key=lambda r: r["impressions"], reverse=True)[:25]


class SearchConsole:
    def __init__(self, apis: GoogleApis):
        self.apis = apis

    def sites(self, grt: str) -> list[dict[str, Any]]:
        resp = self.apis.call(grt, "gsc", "GET", "webmasters/v3/sites")
        return sorted(({"site": s.get("siteUrl"), "access": s.get("permissionLevel")} for s in resp.get("siteEntry") or []),
                      key=lambda s: s["site"])

    def site(self, grt: str, ref: str) -> str:
        ref = str(ref or "").strip()
        if not ref:
            raise AdsError("Give the website, e.g. example.com (gsc_list_sites shows the ones you can see).")
        sites = [s for s in self.sites(grt) if s["access"] != "siteUnverifiedUser"]
        for s in sites:
            if s["site"] == ref:
                return ref
        want = _host(ref)
        found = [s["site"] for s in sites if _host(s["site"]) == want]
        if found:  # prefer the domain property, it covers http/https and www
            return sorted(found, key=lambda x: (not x.startswith("sc-domain:"), x))[0]
        raise AdsError(f"Your Google login can't see '{ref}' in Search Console. Ask the client to add "
                       "your login as a user (Search Console > Settings > Users and permissions), or check "
                       "gsc_list_sites.")

    def query(self, grt: str, site: str, start: date, end: date, dimensions: list[str],
              filters: list[dict[str, Any]], search_type: str, limit: int) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dimensions,
                                "rowLimit": min(limit, 25000), "type": search_type, "dataState": "final"}
        if filters:
            body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
        resp = self.apis.call(grt, "gsc", "POST", f"webmasters/v3/sites/{_enc(site)}/searchAnalytics/query", body=body)
        rows = []
        for r in resp.get("rows") or []:
            row = dict(zip(dimensions, r.get("keys") or []))
            row.update({"clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
                        "ctr": round(r.get("ctr", 0), 4), "position": round(r.get("position", 0), 1)})
            rows.append(row)
        return rows

    def search_queries(self, grt: str, ref: str, a: dict[str, Any]) -> dict[str, Any]:
        site = self.site(grt, ref)
        dims = a.get("dimensions") or ["query"]
        bad = [d for d in dims if d not in DIMENSIONS]
        if bad:
            raise AdsError(f"Unknown dimension(s) {', '.join(bad)}. Use: {', '.join(DIMENSIONS)}.")
        start, end = period(a.get("days") or 28, a.get("start_date"), a.get("end_date"))
        filters = []
        for dim, key, op in (("query", "query_contains", "contains"), ("query", "query_not_contains", "notContains"),
                             ("page", "page_contains", "contains")):
            if a.get(key):
                filters.append({"dimension": dim, "operator": op, "expression": a[key]})
        if a.get("country"):
            filters.append({"dimension": "country", "operator": "equals", "expression": a["country"].lower()})
        if a.get("device"):
            filters.append({"dimension": "device", "operator": "equals", "expression": a["device"].upper()})
        limit = a.get("max_rows") or 200
        rows = self.query(grt, site, start, end, dims, filters, a.get("search_type") or "web", limit)
        out: dict[str, Any] = {"site": site, "period": f"{start} to {end}", "dimensions": dims}
        if a.get("compare_previous"):
            span = (end - start).days + 1
            pstart, pend = start - timedelta(days=span), start - timedelta(days=1)
            prev = {tuple(r.get(d) for d in dims): r for r in
                    self.query(grt, site, pstart, pend, dims, filters, a.get("search_type") or "web", 25000)}
            for r in rows:
                p = prev.get(tuple(r.get(d) for d in dims))
                r["clicks_before"] = p["clicks"] if p else 0
                r["impressions_before"] = p["impressions"] if p else 0
                r["position_before"] = p["position"] if p else None
                r["clicks_change"] = r["clicks"] - r["clicks_before"]
            out["compared_with"] = f"{pstart} to {pend}"
        totals = {"clicks": sum(r["clicks"] for r in rows), "impressions": sum(r["impressions"] for r in rows)}
        out.update({"row_count": len(rows), "totals_of_rows_shown": totals, "rows": rows[:limit]})
        if dims == ["query"]:
            out["opportunities"] = opportunities(rows)
            out["tip"] = ("These are real Google searches. Ones with many impressions but few clicks (opportunities) "
                          "are good Google Ads keywords; add them with add_keywords after showing the person.")
        if not rows:
            out["note"] = "No data for this period. New or small sites may have none yet."
        return out

    def inspect(self, grt: str, ref: str, url: str) -> dict[str, Any]:
        site = self.site(grt, ref)
        resp = self.apis.call(grt, "gsc", "POST", "v1/urlInspection/index:inspect",
                              body={"inspectionUrl": url, "siteUrl": site, "languageCode": "en-US"})
        r = resp.get("inspectionResult") or {}
        idx = r.get("indexStatusResult") or {}
        mob = r.get("mobileUsabilityResult") or {}
        return {
            "site": site, "url": url, "verdict": idx.get("verdict"), "coverage": idx.get("coverageState"),
            "indexing_allowed": idx.get("indexingState"), "robots_txt": idx.get("robotsTxtState"),
            "page_fetch": idx.get("pageFetchState"), "last_crawl": idx.get("lastCrawlTime"),
            "google_canonical": idx.get("googleCanonical"), "your_canonical": idx.get("userCanonical"),
            "mobile": mob.get("verdict"), "rich_results": (r.get("richResultsResult") or {}).get("verdict"),
            "details_link": r.get("inspectionResultLink"),
        }

    def sitemaps(self, grt: str, ref: str) -> dict[str, Any]:
        site = self.site(grt, ref)
        resp = self.apis.call(grt, "gsc", "GET", f"webmasters/v3/sites/{_enc(site)}/sitemaps")
        items = [{"sitemap": s.get("path"), "last_read": s.get("lastDownloaded"), "errors": int(s.get("errors") or 0),
                  "warnings": int(s.get("warnings") or 0), "pending": s.get("isPending"),
                  "pages": sum(int(c.get("submitted") or 0) for c in s.get("contents") or [])}
                 for s in resp.get("sitemap") or []]
        out: dict[str, Any] = {"site": site, "sitemaps": items}
        if not items:
            out["note"] = ("No sitemap submitted. Adding one (Search Console > Sitemaps) helps Google find every page.")
        return out


Site = Annotated[str, Field(description="The website, e.g. example.com, https://www.example.com/ or sc-domain:example.com.")]


def register_search_console_tools(mcp: FastMCP, w: Writer, apis: GoogleApis) -> SearchConsole:
    sc = SearchConsole(apis)

    @mcp.tool(annotations=READ_ONLY)
    async def gsc_list_sites() -> dict[str, Any]:
        """List every website your Google login can see in Search Console, with your access level."""
        return await w.run("gsc_list_sites", None, lambda user, grt: {"sites": sc.sites(grt)})

    @mcp.tool(annotations=READ_ONLY)
    async def gsc_search_queries(
        site: Site,
        days: Annotated[int, Field(ge=1, le=480, description="How many days back (ending 3 days ago).")] = 28,
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD (instead of days).")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        dimensions: Annotated[list[Literal["query", "page", "country", "device", "date", "searchAppearance"]] | None,
                              Field(description="What each row is. Default: query (the search words).")] = None,
        query_contains: Annotated[str | None, Field(description="Only searches containing this text.")] = None,
        query_not_contains: Annotated[str | None, Field(description="Leave out searches containing this, e.g. the brand.")] = None,
        page_contains: Annotated[str | None, Field(description="Only pages whose URL contains this.")] = None,
        country: Annotated[str | None, Field(description="3-letter country code, e.g. aus, are, pak.")] = None,
        device: Literal["DESKTOP", "MOBILE", "TABLET"] | None = None,
        search_type: Literal["web", "image", "video", "news", "discover", "googleNews"] = "web",
        compare_previous: Annotated[bool, Field(description="Add the period before for comparison.")] = False,
        max_rows: Annotated[int, Field(ge=1, le=25000)] = 200,
    ) -> dict[str, Any]:
        """The real Google searches that bring people to a client's website (from Search Console): clicks,
        impressions, click rate and average position per search, page, country, device or day. Also lists keyword
        opportunities. Good for Google Ads keyword research."""
        a = dict(days=days, start_date=start_date, end_date=end_date, dimensions=dimensions,
                 query_contains=query_contains, query_not_contains=query_not_contains, page_contains=page_contains,
                 country=country, device=device, search_type=search_type, compare_previous=compare_previous,
                 max_rows=max_rows)
        return await w.run("gsc_search_queries", None, lambda user, grt: sc.search_queries(grt, site, a))

    @mcp.tool(annotations=READ_ONLY)
    async def gsc_inspect_url(
        site: Site,
        url: Annotated[str, Field(description="The full page address to check, e.g. a landing page.")],
    ) -> dict[str, Any]:
        """Check one page in Search Console: is it on Google, when it was last crawled, its canonical, mobile
        friendliness. Useful before sending ad traffic to a landing page."""
        return await w.run("gsc_inspect_url", None, lambda user, grt: sc.inspect(grt, site, url))

    @mcp.tool(annotations=READ_ONLY)
    async def gsc_sitemaps(site: Site) -> dict[str, Any]:
        """The sitemaps submitted for a website in Search Console, with errors and page counts."""
        return await w.run("gsc_sitemaps", None, lambda user, grt: sc.sitemaps(grt, site))

    return sc
