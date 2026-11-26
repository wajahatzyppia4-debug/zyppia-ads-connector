"""Phase 5: Google Analytics 4 (properties, web streams, key events, link to Google Ads, reports,
and a live check that a test conversion arrived).

Reads use the GA4 Admin API (setup) and Data API (reports, realtime). Changes are
previewed and sealed like everything else, then made by apply_change; undo
deletes what was created (a deleted property sits in GA4's trash for 35 days).
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import AdsError
from .google_apis import GoogleApis, raw_change
from .writes import CID, PREVIEW, Change, Writer

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
CACHE_SECONDS = 600
ADMIN = "v1beta/"
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|today|yesterday|\d{1,4}daysAgo)$")
_EVENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
IMPORT_STEPS = (
    "To count GA4 key events as Google Ads conversions: in Google Ads go to Goals > Conversions > Summary > "
    "+ New conversion action > Import > Google Analytics 4 properties > Web, tick the key events and click Import. "
    "Then list_conversion_actions shows them."
)


def _num(value: str, kind: str) -> Any:
    try:
        return int(value) if kind == "TYPE_INTEGER" else round(float(value), 4)
    except (TypeError, ValueError):
        return value


def report_rows(resp: dict[str, Any]) -> list[dict[str, Any]]:
    dims = [h["name"] for h in resp.get("dimensionHeaders") or []]
    mets = [(h["name"], h.get("type")) for h in resp.get("metricHeaders") or []]
    out = []
    for r in resp.get("rows") or []:
        row = {d: v.get("value") for d, v in zip(dims, r.get("dimensionValues") or [])}
        row.update({m: _num(v.get("value"), t) for (m, t), v in zip(mets, r.get("metricValues") or [])})
        out.append(row)
    return out


def dimension_filter(filters: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """[{field: 'eventName', value: 'generate_lead', match: exact|contains|begins_with|ends_with}] -> GA4 filter."""
    if not filters:
        return None
    exprs = []
    for f in filters:
        if not f.get("field") or f.get("value") is None:
            raise AdsError("Each filter needs a field (a dimension, e.g. pagePath) and a value.")
        match = {"exact": "EXACT", "contains": "CONTAINS", "begins_with": "BEGINS_WITH", "ends_with": "ENDS_WITH",
                 "regex": "FULL_REGEXP"}.get(str(f.get("match") or "exact").lower())
        if not match:
            raise AdsError("A filter's match is one of: exact, contains, begins_with, ends_with, regex.")
        e = {"filter": {"fieldName": f["field"], "stringFilter": {"matchType": match, "value": str(f["value"])}}}
        if f.get("exclude"):
            e = {"notExpression": e}
        exprs.append(e)
    return exprs[0] if len(exprs) == 1 else {"andGroup": {"expressions": exprs}}


class Analytics:
    def __init__(self, w: Writer, apis: GoogleApis):
        self.w, self.apis = w, apis
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    # ---- reading

    def accounts(self, grt: str, refresh: bool = False) -> list[dict[str, Any]]:
        key = hashlib.sha256(grt.encode()).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
        if hit and not refresh and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        out = []
        for s in self.apis.list_all(grt, "ga_admin", ADMIN + "accountSummaries", "accountSummaries", {"pageSize": 200}):
            out.append({
                "account": s.get("displayName"), "account_id": (s.get("account") or "").split("/")[-1],
                "properties": [{"name": p.get("displayName"), "property_id": (p.get("property") or "").split("/")[-1],
                                "type": p.get("propertyType")} for p in s.get("propertySummaries") or []],
            })
        with self._lock:
            self._cache[key] = (time.time(), out)
        return out

    def property_id(self, grt: str, ref: str) -> str:
        ref = str(ref or "").strip()
        digits = re.sub(r"^properties/", "", ref)
        if digits.isdigit():
            return digits
        if not ref:
            raise AdsError("Give the GA4 property ID (a number, ga4_list_properties shows them) or its exact name.")
        for refresh in (False, True):
            found = [p for a in self.accounts(grt, refresh) for p in a["properties"] if p["name"].lower() == ref.lower()]
            if len(found) == 1:
                return found[0]["property_id"]
            if len(found) > 1:
                raise AdsError(f"More than one GA4 property is called '{ref}'. Use its ID: "
                               + ", ".join(p["property_id"] for p in found))
        raise AdsError(f"Your Google login can't see a GA4 property '{ref}'. ga4_list_properties shows the ones it can.")

    def streams(self, grt: str, pid: str) -> list[dict[str, Any]]:
        return self.apis.list_all(grt, "ga_admin", f"{ADMIN}properties/{pid}/dataStreams", "dataStreams")

    def key_events(self, grt: str, pid: str) -> list[dict[str, Any]]:
        return self.apis.list_all(grt, "ga_admin", f"{ADMIN}properties/{pid}/keyEvents", "keyEvents")

    def ads_links(self, grt: str, pid: str) -> list[dict[str, Any]]:
        return self.apis.list_all(grt, "ga_admin", f"{ADMIN}properties/{pid}/googleAdsLinks", "googleAdsLinks")

    def details(self, grt: str, ref: str) -> dict[str, Any]:
        pid = self.property_id(grt, ref)
        prop = self.apis.call(grt, "ga_admin", "GET", f"{ADMIN}properties/{pid}")
        streams, keys, links = self.streams(grt, pid), self.key_events(grt, pid), self.ads_links(grt, pid)
        web = [s for s in streams if s.get("type") == "WEB_DATA_STREAM"]
        problems = []
        if not streams:
            problems.append("No data stream, so no data is collected. Add a web stream (ga4_create_property makes one, "
                            "or use ga4_advanced_change).")
        if not [k for k in keys if k.get("custom")]:
            problems.append("No key events of your own (like generate_lead or purchase), so GA4 has nothing to send "
                            "to Google Ads as conversions. Add one with ga4_key_event.")
        if not links:
            problems.append("Not linked to any Google Ads account. Link it with ga4_link_google_ads.")
        return {
            "property_id": pid, "name": prop.get("displayName"), "time_zone": prop.get("timeZone"),
            "currency": prop.get("currencyCode"), "industry": prop.get("industryCategory"),
            "created": prop.get("createTime"),
            "web_streams": [{"name": s.get("displayName"), "measurement_id": (s.get("webStreamData") or {}).get("measurementId"),
                             "website": (s.get("webStreamData") or {}).get("defaultUri"), "stream": s.get("name")}
                            for s in web],
            "other_streams": [f"{s.get('displayName')} ({s.get('type')})" for s in streams if s not in web],
            "key_events": [{"event": k.get("eventName"), "counting": k.get("countingMethod"), "custom": bool(k.get("custom")),
                            "id": k.get("name")} for k in keys],
            "google_ads_links": [{"customer_id": l.get("customerId"), "personalized_ads": l.get("adsPersonalizationEnabled"),
                                  "id": l.get("name")} for l in links],
            "problems": problems,
            "tip": "The measurement ID (G-...) is what goes on the website. tag_install_code makes the code for it.",
        }

    def report(self, grt: str, ref: str, metrics: list[str], dimensions: list[str], start: str, end: str,
               filters: list[dict[str, Any]] | None, order_by: str | None, limit: int) -> dict[str, Any]:
        pid = self.property_id(grt, ref)
        for d in (start, end):
            if not _DATE_RE.match(d):
                raise AdsError("Dates are YYYY-MM-DD, today, yesterday or NdaysAgo (e.g. 28daysAgo).")
        body: dict[str, Any] = {
            "dateRanges": [{"startDate": start, "endDate": end}],
            "metrics": [{"name": m} for m in metrics],
            "dimensions": [{"name": d} for d in dimensions],
            "limit": limit,
        }
        f = dimension_filter(filters)
        if f:
            body["dimensionFilter"] = f
        sort = order_by or metrics[0]
        if sort in metrics:
            body["orderBys"] = [{"metric": {"metricName": sort}, "desc": True}]
        elif sort in dimensions:
            body["orderBys"] = [{"dimension": {"dimensionName": sort}}]
        resp = self.apis.call(grt, "ga_data", "POST", f"v1beta/properties/{pid}:runReport", body=body)
        rows = report_rows(resp)
        return {"property_id": pid, "period": f"{start} to {end}", "row_count": resp.get("rowCount", len(rows)),
                "rows": rows}

    def check_event(self, grt: str, ref: str, event_name: str | None) -> dict[str, Any]:
        pid = self.property_id(grt, ref)
        body = {"dimensions": [{"name": "eventName"}], "metrics": [{"name": "eventCount"}], "limit": 100}
        resp = self.apis.call(grt, "ga_data", "POST", f"v1beta/properties/{pid}:runRealtimeReport", body=body)
        events = {r["eventName"]: r["eventCount"] for r in report_rows(resp)}
        out: dict[str, Any] = {"property_id": pid, "window": "the last 30 minutes", "events": events}
        if event_name:
            n = events.get(event_name, 0)
            out["event"] = event_name
            out["arrived"] = bool(n)
            out["count"] = n
            if n:
                out["result"] = f"Yes: GA4 received '{event_name}' {n} time(s) in the last 30 minutes."
            elif events:
                out["result"] = (f"Not yet: GA4 is getting data from the site, but no '{event_name}' in the last 30 "
                                 "minutes. Check the event name matches exactly, then do the test again.")
            else:
                out["result"] = ("Nothing at all in the last 30 minutes, so the GA4 tag may not be on the site. "
                                 "Run check_site_tags on the website.")
        if not events:
            out["note"] = "No events in the last 30 minutes."
        out["also"] = ("Google Ads shows imported conversions a few hours later; list_conversion_actions shows their "
                       "status.")
        return out

    # ---- changes

    def create_property(self, grt: str, a: dict[str, Any]) -> Change:
        acct_id = re.sub(r"^accounts/", "", str(a.get("account_id") or "").strip())
        if not acct_id.isdigit():
            raise AdsError("Give the GA4 account ID (a number, ga4_list_properties shows them).")
        name = " ".join(str(a.get("name") or "").split())
        if not name:
            raise AdsError("Give the new property a name, e.g. the client's business name.")
        url = str(a.get("website_url") or "").strip()
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        accts = self.accounts(grt)
        acct = next((x for x in accts if x["account_id"] == acct_id), None)
        if acct and any(p["name"].lower() == name.lower() for p in acct["properties"]):
            raise AdsError(f"GA4 account '{acct['account']}' already has a property called '{name}'. "
                           "Use that one (ga4_property), or pick another name.")
        tz = a.get("time_zone") or "Australia/Perth"
        cur = str(a.get("currency") or "AUD").upper()
        body = {"parent": f"accounts/{acct_id}", "displayName": name, "timeZone": tz, "currencyCode": cur}
        if a.get("industry"):
            body["industryCategory"] = str(a["industry"]).upper()
        where = f"Google Analytics account {acct['account'] if acct else acct_id}"
        ch = Change(None, f"Create GA4 property '{name}'", where=where)
        ch.lines.append(f"new GA4 property '{name}' (time zone {tz}, currency {cur})")
        ch.gtm.append({"a": "ga_admin", "m": "POST", "p": ADMIN + "properties", "b": body, "s": "property", "f": "name",
                       "d": f"create GA4 property '{name}'",
                       "u": {"a": "ga_admin", "m": "DELETE", "p": ADMIN + "${property}",
                             "d": f"delete GA4 property '{name}' (it stays in GA4's trash for 35 days)"}})
        if url:
            ch.lines.append(f"new web data stream for {url} (this gives the G-... measurement ID)")
            ch.gtm.append({"a": "ga_admin", "m": "POST", "p": ADMIN + "${property}/dataStreams", "s": "measurement_id",
                           "f": "webStreamData.measurementId", "d": f"create web stream for {url}",
                           "n": "the web stream (undo deletes the whole property instead)",
                           "b": {"type": "WEB_DATA_STREAM", "displayName": a.get("stream_name") or name,
                                 "webStreamData": {"defaultUri": url}}})
        else:
            ch.warnings.append("No website_url, so no web stream: the property won't collect data until it has one.")
        ch.warnings.append("After applying, ga4_property shows the G-... measurement ID. Put it on the site with Tag "
                           "Manager (gtm_tag kind google_tag) or give the developer tag_install_code.")
        return ch

    def key_event(self, grt: str, ref: str, action: str, a: dict[str, Any]) -> Change:
        pid = self.property_id(grt, ref)
        keys = self.key_events(grt, pid)
        ev = str(a.get("event_name") or "").strip()
        if not _EVENT_RE.match(ev):
            raise AdsError("Give the event name exactly as the site sends it, e.g. generate_lead, purchase, "
                           "form_submit (letters, numbers and _, up to 40).")
        cur = next((k for k in keys if k.get("eventName") == ev), None)
        where = f"Google Analytics property {pid}"
        if action == "create":
            if cur:
                raise AdsError(f"'{ev}' is already a key event in this property.")
            body: dict[str, Any] = {"eventName": ev, "countingMethod": a.get("counting_method") or "ONCE_PER_EVENT"}
            if a.get("default_value") is not None:
                body["defaultValue"] = {"numericValue": float(a["default_value"]),
                                        "currencyCode": str(a.get("currency") or "AUD").upper()}
            ch = Change(None, f"Mark '{ev}' as a key event in GA4", where=where)
            ch.lines.append(f"key event '{ev}', counted {body['countingMethod'].replace('_', ' ').lower()}"
                            + (f", default value {body['defaultValue']['numericValue']} {body['defaultValue']['currencyCode']}"
                               if "defaultValue" in body else ""))
            ch.gtm.append({"a": "ga_admin", "m": "POST", "p": f"{ADMIN}properties/{pid}/keyEvents", "b": body,
                           "s": "key_event", "f": "name", "d": f"mark '{ev}' as a key event",
                           "u": {"a": "ga_admin", "m": "DELETE", "p": ADMIN + "${key_event}",
                                 "d": f"stop counting '{ev}' as a key event"}})
            ch.warnings.append("GA4 only counts it if the website actually sends an event with exactly this name. "
                               "Test with ga4_check_event.")
            ch.warnings.append(IMPORT_STEPS)
        else:
            if not cur:
                raise AdsError(f"'{ev}' isn't a key event in this property.")
            if cur.get("deletable") is False:
                raise AdsError(f"GA4 doesn't allow removing the key event '{ev}'.")
            ch = Change(None, f"Stop counting '{ev}' as a key event in GA4", where=where)
            ch.lines.append(f"remove key event '{ev}' (the event itself is still collected)")
            body = {k: cur[k] for k in ("eventName", "countingMethod", "defaultValue") if k in cur}
            ch.gtm.append({"a": "ga_admin", "m": "DELETE", "p": ADMIN + cur["name"], "d": f"remove key event '{ev}'",
                           "u": {"a": "ga_admin", "m": "POST", "p": f"{ADMIN}properties/{pid}/keyEvents", "b": body,
                                 "d": f"mark '{ev}' as a key event again"}})
            ch.large.append("Google Ads conversions imported from this key event stop recording")
        return ch

    def link_ads(self, grt: str, user: str, ref: str, customer_id: str, action: str, personalization: bool) -> Change:
        pid = self.property_id(grt, ref)
        acct = self.w.ads.account(user, grt, customer_id)
        links = self.ads_links(grt, pid)
        cur = next((l for l in links if re.sub(r"\D", "", str(l.get("customerId"))) == acct.customer_id), None)
        where = f"Google Analytics property {pid}"
        label = f"{acct.name} ({acct.customer_id[:3]}-{acct.customer_id[3:6]}-{acct.customer_id[6:]})"
        if action == "link":
            if cur:
                raise AdsError(f"GA4 property {pid} is already linked to {label}.")
            ch = Change(None, f"Link GA4 property {pid} to Google Ads {label}", where=where)
            ch.lines.append(f"link to Google Ads account {label}, personalized ads {'on' if personalization else 'off'}")
            ch.gtm.append({"a": "ga_admin", "m": "POST", "p": f"{ADMIN}properties/{pid}/googleAdsLinks",
                           "b": {"customerId": acct.customer_id, "adsPersonalizationEnabled": personalization},
                           "s": "ads_link", "f": "name", "d": f"link GA4 to Google Ads {label}",
                           "u": {"a": "ga_admin", "m": "DELETE", "p": ADMIN + "${ads_link}",
                                 "d": f"unlink GA4 from Google Ads {label}"}})
            ch.warnings.append("Your login needs Editor access on the GA4 property and admin access on the Google Ads "
                               "account, or Google refuses the link.")
            ch.warnings.append(IMPORT_STEPS)
        else:
            if not cur:
                raise AdsError(f"GA4 property {pid} isn't linked to {label}.")
            ch = Change(None, f"Unlink GA4 property {pid} from Google Ads {label}", where=where)
            ch.lines.append(f"remove the link to {label}")
            ch.gtm.append({"a": "ga_admin", "m": "DELETE", "p": ADMIN + cur["name"], "d": f"unlink from {label}",
                           "u": {"a": "ga_admin", "m": "POST", "p": f"{ADMIN}properties/{pid}/googleAdsLinks",
                                 "b": {"customerId": acct.customer_id,
                                       "adsPersonalizationEnabled": cur.get("adsPersonalizationEnabled", True)},
                                 "d": f"link GA4 to Google Ads {label} again"}})
            ch.large.append("conversions imported from GA4 and GA4 audiences stop flowing to this Google Ads account")
        return ch


Property = Annotated[str, Field(description="GA4 property: its ID (a number from ga4_list_properties) or exact name.")]


def register_analytics_tools(mcp: FastMCP, w: Writer, apis: GoogleApis) -> Analytics:
    ga = Analytics(w, apis)

    def change(tool: str, build):
        return w.run(tool, None, lambda user, grt: w.preview(user, grt, build(grt, user)))

    @mcp.tool(annotations=READ_ONLY)
    async def ga4_list_properties(
        refresh: Annotated[bool, Field(description="Re-read from Google instead of the 10-minute cache.")] = False,
    ) -> dict[str, Any]:
        """List every Google Analytics 4 account and property your Google login can access, with their IDs.
        Start here for any Google Analytics work."""
        def go(user, grt):
            items = ga.accounts(grt, refresh)
            return {"accounts": items, "property_count": sum(len(a["properties"]) for a in items)}
        return await w.run("ga4_list_properties", None, go)

    @mcp.tool(annotations=READ_ONLY)
    async def ga4_property(property: Property) -> dict[str, Any]:
        """One GA4 property's setup: time zone, currency, web streams with their G-... measurement IDs, key events
        (conversions), Google Ads links, and what's missing."""
        return await w.run("ga4_property", None, lambda user, grt: ga.details(grt, property))

    @mcp.tool(annotations=READ_ONLY)
    async def ga4_report(
        property: Property,
        metrics: Annotated[list[str], Field(description=(
            "GA4 metrics, e.g. sessions, totalUsers, newUsers, engagedSessions, engagementRate, keyEvents, "
            "eventCount, screenPageViews, totalRevenue, purchaseRevenue, conversions."))] = ["sessions", "totalUsers", "keyEvents"],
        dimensions: Annotated[list[str], Field(description=(
            "GA4 dimensions, e.g. date, sessionDefaultChannelGroup, sessionSource, sessionMedium, sessionCampaignName, "
            "landingPage, pagePath, eventName, country, city, deviceCategory. Empty = totals."))] = ["sessionDefaultChannelGroup"],
        start_date: Annotated[str, Field(description="YYYY-MM-DD, or NdaysAgo / yesterday / today.")] = "28daysAgo",
        end_date: str = "yesterday",
        filters: Annotated[list[dict[str, Any]] | None, Field(description=(
            "All must match: [{field: 'eventName', value: 'generate_lead', match: exact|contains|begins_with|"
            "ends_with|regex, exclude: false}]."))] = None,
        order_by: Annotated[str | None, Field(description="A metric (sorted high to low) or dimension. Default: first metric.")] = None,
        limit: Annotated[int, Field(ge=1, le=10000)] = 100,
    ) -> dict[str, Any]:
        """Google Analytics 4 report: any metrics by any dimensions for a date range (traffic by channel, landing
        pages, key events by source, Google Ads campaigns' on-site behaviour, and so on)."""
        return await w.run("ga4_report", None, lambda user, grt: ga.report(
            grt, property, metrics, dimensions, start_date, end_date, filters, order_by, limit))

    @mcp.tool(annotations=READ_ONLY)
    async def ga4_check_event(
        property: Property,
        event_name: Annotated[str | None, Field(description="The event to look for, e.g. generate_lead. Empty = list all.")] = None,
    ) -> dict[str, Any]:
        """Live check: which events GA4 received in the last 30 minutes. After a test form fill or purchase on the
        site, use this to confirm the conversion event actually arrived."""
        return await w.run("ga4_check_event", None, lambda user, grt: ga.check_event(grt, property, event_name))

    @mcp.tool(annotations=PREVIEW)
    async def ga4_create_property(
        account_id: Annotated[str, Field(description="GA4 account ID to create it in (ga4_list_properties).")],
        name: Annotated[str, Field(description="Property name, e.g. the client's business name.")],
        website_url: Annotated[str | None, Field(description="The site, https://... Also creates its web data stream.")] = None,
        time_zone: Annotated[str, Field(description="IANA time zone, e.g. Australia/Perth, Asia/Dubai, Asia/Karachi.")] = "Australia/Perth",
        currency: Annotated[str, Field(description="Currency code, e.g. AUD, AED, PKR.")] = "AUD",
        industry: Annotated[str | None, Field(description="Optional GA4 industry, e.g. TRAVEL, HEALTHCARE, HOME_AND_GARDEN.")] = None,
        stream_name: str | None = None,
    ) -> dict[str, Any]:
        """Preview creating a GA4 property and its web data stream (which gives the G-... measurement ID).
        Undo deletes the property."""
        a = dict(account_id=account_id, name=name, website_url=website_url, time_zone=time_zone, currency=currency,
                 industry=industry, stream_name=stream_name)
        return await change("ga4_create_property", lambda grt, user: ga.create_property(grt, a))

    @mcp.tool(annotations=PREVIEW)
    async def ga4_key_event(
        property: Property,
        event_name: Annotated[str, Field(description="Event name exactly as the site sends it, e.g. generate_lead, purchase.")],
        action: Literal["create", "delete"] = "create",
        counting_method: Literal["ONCE_PER_EVENT", "ONCE_PER_SESSION"] = "ONCE_PER_EVENT",
        default_value: Annotated[float | None, Field(description="Optional value per key event.")] = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        """Preview marking an event as a key event (GA4's name for a conversion), or removing one. Key events can
        then be imported into Google Ads as conversions."""
        a = dict(event_name=event_name, counting_method=counting_method, default_value=default_value, currency=currency)
        return await change("ga4_key_event", lambda grt, user: ga.key_event(grt, property, action, a))

    @mcp.tool(annotations=PREVIEW)
    async def ga4_link_google_ads(
        property: Property,
        customer_id: CID,
        action: Literal["link", "unlink"] = "link",
        personalized_ads: Annotated[bool, Field(description="Let GA4 audiences be used for remarketing.")] = True,
    ) -> dict[str, Any]:
        """Preview linking a GA4 property to a Google Ads account (or removing the link), so GA4 key events can be
        imported as conversions and GA4 audiences used in campaigns."""
        return await change("ga4_link_google_ads", lambda grt, user: ga.link_ads(
            grt, user, property, customer_id, action, personalized_ads))

    @mcp.tool(annotations=PREVIEW)
    async def ga4_advanced_change(
        calls: Annotated[list[dict[str, Any]], Field(description=(
            "GA4 Admin API calls: [{method: POST|PATCH|DELETE, path: 'v1beta/properties/123/customDimensions', "
            "body: {...}, params: {updateMask: '...'}}]. E.g. custom dimensions, data retention, extra streams."))],
        description: Annotated[str, Field(description="Plain-language summary of what this does, shown to the person.")],
    ) -> dict[str, Any]:
        """Preview any other Google Analytics setting change with raw Admin API calls. Use the dedicated ga4_ tools
        when one fits. Always needs extra confirmation."""
        return await change("ga4_advanced_change", lambda grt, user: raw_change("ga_admin", "Google Analytics",
                                                                                calls, description))

    return ga
