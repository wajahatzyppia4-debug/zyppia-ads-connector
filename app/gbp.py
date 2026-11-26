"""Phase 6: Google Business Profile (the business's listing on Google Search and Maps).

Reading: locations, a profile audit with a 0-100 score, reviews (with reply rate),
posts, photos, and insights (views on Search and Maps, calls, website clicks,
directions) plus the monthly search keywords people used to find the profile.

Changing: reply to reviews, create or delete posts, add photos, and edit the
profile (description, website, phone, hours, categories). Like every change here,
the tools only preview; apply_change makes it and undo_change reverses it.

Google must approve the Cloud project for the Business Profile APIs once (free),
and the signed-in Google login must be an Owner or Manager of each profile.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import AdsError
from .google_apis import GoogleApis, raw_change
from .writes import PREVIEW, Change, Writer

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

LIST_MASK = "name,title,storefrontAddress,categories,metadata,latlng,websiteUri,phoneNumbers,openInfo,serviceArea"
FULL_MASK = LIST_MASK + ",regularHours,specialHours,moreHours,profile,labels,serviceItems"
STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
PHOTO_CATEGORIES = ("COVER", "PROFILE", "LOGO", "EXTERIOR", "INTERIOR", "PRODUCT", "AT_WORK", "FOOD_AND_DRINK",
                    "MENU", "COMMON_AREA", "ROOMS", "TEAMS", "ADDITIONAL")
CTA_TYPES = ("BOOK", "ORDER", "SHOP", "LEARN_MORE", "SIGN_UP", "CALL")
# Business Profile Performance API daily metrics -> short names people know
METRICS = {
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH": "views_search_mobile",
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH": "views_search_desktop",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS": "views_maps_mobile",
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": "views_maps_desktop",
    "CALL_CLICKS": "calls",
    "WEBSITE_CLICKS": "website_clicks",
    "BUSINESS_DIRECTION_REQUESTS": "direction_requests",
    "BUSINESS_CONVERSATIONS": "messages",
    "BUSINESS_BOOKINGS": "bookings",
    "BUSINESS_FOOD_ORDERS": "food_orders",
}
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_PHONE_IN_TEXT = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")
CACHE_TTL = 600


def loc_id(ref: str) -> str:
    return str(ref or "").strip().removeprefix("locations/").split("/")[-1]


def address_text(a: dict[str, Any] | None) -> str | None:
    if not a:
        return None
    parts = list(a.get("addressLines") or []) + [a.get("locality"), a.get("administrativeArea"), a.get("postalCode"),
                                                  a.get("regionCode")]
    return ", ".join(p for p in parts if p) or None


def summary(loc: dict[str, Any], account: str) -> dict[str, Any]:
    md = loc.get("metadata") or {}
    cats = loc.get("categories") or {}
    out = {
        "location_id": loc_id(loc.get("name")), "account": account, "name": loc.get("title"),
        "address": address_text(loc.get("storefrontAddress")),
        "service_area_only": bool(loc.get("serviceArea")) and not loc.get("storefrontAddress"),
        "primary_category": (cats.get("primaryCategory") or {}).get("displayName"),
        "website": loc.get("websiteUri"), "phone": (loc.get("phoneNumbers") or {}).get("primaryPhone"),
        "place_id": md.get("placeId"), "maps_url": md.get("mapsUri"), "review_link": md.get("newReviewUri"),
        "lat": (loc.get("latlng") or {}).get("latitude"), "lng": (loc.get("latlng") or {}).get("longitude"),
        "status": (loc.get("openInfo") or {}).get("status"),
    }
    if md.get("hasVoiceOfMerchant") is False:
        out["warning"] = "Not verified or suspended: Google may not show edits or let you post."
    if md.get("hasGoogleUpdated"):
        out["google_updated"] = "Google changed something on this profile. Check it in Business Profile."
    if md.get("hasPendingEdits"):
        out["pending_edits"] = True
    return {k: v for k, v in out.items() if v not in (None, "", False)}


def parse_hours(hours: dict[str, str]) -> dict[str, Any]:
    """{'MONDAY': '09:00-17:00', 'SATURDAY': '10:00-14:00, 15:00-18:00', 'SUNDAY': 'closed'} -> regularHours."""
    periods = []
    for day, text in hours.items():
        d = str(day).strip().upper()
        if d[:3] in {x[:3] for x in DAYS}:
            d = next(x for x in DAYS if x[:3] == d[:3])
        else:
            raise AdsError(f"'{day}' isn't a day of the week.")
        t = str(text or "").strip().lower()
        if t in ("closed", ""):
            continue
        if t in ("24h", "24 hours", "open 24 hours"):
            t = "00:00-24:00"
        for part in t.split(","):
            m = _TIME_RE.match(part.strip())
            if not m:
                raise AdsError(f"Hours for {d} should look like '09:00-17:00' (or 'closed'), not '{text}'.")
            oh, om, ch, cm = map(int, m.groups())
            if oh > 24 or ch > 24 or om > 59 or cm > 59:
                raise AdsError(f"'{part.strip()}' isn't a valid time range.")
            close_day = d
            if (ch, cm) <= (oh, om) and (ch, cm) != (0, 0):  # past midnight, e.g. 18:00-02:00
                close_day = DAYS[(DAYS.index(d) + 1) % 7]
            if (ch, cm) == (0, 0):
                ch = 24
            periods.append({"openDay": d, "openTime": {"hours": oh, "minutes": om},
                            "closeDay": close_day, "closeTime": {"hours": ch, "minutes": cm}})
    return {"periods": periods}


def hours_text(rh: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, list[str]] = {d: [] for d in DAYS}
    for p in (rh or {}).get("periods") or []:
        o, c = p.get("openTime") or {}, p.get("closeTime") or {}
        out.setdefault(p.get("openDay"), []).append(
            f"{o.get('hours', 0):02d}:{o.get('minutes', 0):02d}-{c.get('hours', 0):02d}:{c.get('minutes', 0):02d}")
    return {d: ", ".join(v) or "closed" for d, v in out.items() if d in DAYS}


def review_row(r: dict[str, Any]) -> dict[str, Any]:
    reply = r.get("reviewReply") or {}
    return {k: v for k, v in {
        "review_id": r.get("reviewId") or (r.get("name") or "").split("/")[-1],
        "stars": STARS.get(r.get("starRating")), "by": (r.get("reviewer") or {}).get("displayName"),
        "text": r.get("comment"), "date": (r.get("createTime") or "")[:10],
        "reply": reply.get("comment"), "replied_on": (reply.get("updateTime") or "")[:10] or None,
    }.items() if v not in (None, "")}


def _dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _gdate(d: date, prefix: str) -> dict[str, int]:
    return {f"{prefix}.year": d.year, f"{prefix}.month": d.month, f"{prefix}.day": d.day}


def _event_date(text: str, what: str) -> tuple[dict[str, int], dict[str, int] | None]:
    """'2026-12-24' or '2026-12-24 18:30' -> (Date, TimeOfDay|None)."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?$", str(text or "").strip())
    if not m:
        raise AdsError(f"{what} should look like 2026-12-24 or '2026-12-24 18:30'.")
    y, mo, d, h, mi = m.groups()
    day = {"year": int(y), "month": int(mo), "day": int(d)}
    return day, ({"hours": int(h), "minutes": int(mi)} if h else None)


class BusinessProfile:
    def __init__(self, w: Writer, apis: GoogleApis):
        self.w, self.apis = w, apis
        self._cache: dict[str, tuple[float, list[tuple[str, dict[str, Any]]]]] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- locations

    def all_locations(self, grt: str, refresh: bool = False) -> list[tuple[str, dict[str, Any]]]:
        """[(account resource name, location)] for every profile the login manages (cached 10 minutes)."""
        key = sha256(grt.encode()).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
        if hit and not refresh and hit[0] > time.time():
            return hit[1]
        accounts = self.apis.list_all(grt, "gbp_accounts", "v1/accounts", "accounts", {"pageSize": 20})
        out = []
        for a in accounts:
            locs = self.apis.list_all(grt, "gbp_info", f"v1/{a['name']}/locations", "locations",
                                      {"readMask": LIST_MASK, "pageSize": 100})
            out += [(a["name"], loc) for loc in locs]
        with self._lock:
            self._cache[key] = (time.time() + CACHE_TTL, out)
        return out

    def accounts_and_locations(self, grt: str, search: str | None, refresh: bool) -> dict[str, Any]:
        rows = [summary(loc, acct) for acct, loc in self.all_locations(grt, refresh)]
        if search:
            q = search.lower()
            rows = [r for r in rows if q in (r.get("name") or "").lower() or q in (r.get("address") or "").lower()]
        return {"count": len(rows), "locations": sorted(rows, key=lambda r: (r.get("name") or "").lower()),
                "tip": "Use location_id (or the business name) with the other Business Profile tools."}

    def find(self, grt: str, ref: str) -> tuple[str, dict[str, Any]]:
        ref = str(ref or "").strip()
        if not ref:
            raise AdsError("Say which Business Profile: its location_id or name (gbp_list_locations shows them).")
        items = self.all_locations(grt)
        lid = loc_id(ref)
        hit = [x for x in items if loc_id(x[1].get("name")) == lid]
        if not hit:
            q = ref.lower()
            hit = [x for x in items if (x[1].get("title") or "").lower() == q] or \
                  [x for x in items if q in (x[1].get("title") or "").lower()]
        if not hit:
            raise AdsError(f"Your Google login doesn't manage a Business Profile matching '{ref}'. Ask the client to "
                           "add it as a Manager (Business Profile > People and access), or check gbp_list_locations.")
        if len(hit) > 1:
            raise AdsError(f"'{ref}' matches {len(hit)} profiles: " + "; ".join(
                f"{x[1].get('title')} ({address_text(x[1].get('storefrontAddress')) or 'service area'}, "
                f"location_id {loc_id(x[1].get('name'))})" for x in hit[:10]) + ". Use the location_id.")
        return hit[0]

    def detail(self, grt: str, lid: str) -> dict[str, Any]:
        return self.apis.call(grt, "gbp_info", "GET", f"v1/locations/{lid}", params={"readMask": FULL_MASK})

    def v4(self, acct: str, lid: str) -> str:
        return f"v4/{acct}/locations/{lid}"

    # ---------------------------------------------------------------- reading

    def reviews_raw(self, grt: str, acct: str, lid: str, limit: int = 500) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        token, first = None, {}
        while len(out) < limit:
            q: dict[str, Any] = {"pageSize": 50, "orderBy": "updateTime desc"}
            if token:
                q["pageToken"] = token
            resp = self.apis.call(grt, "gbp_v4", "GET", f"{self.v4(acct, lid)}/reviews", params=q)
            first = first or resp
            out += resp.get("reviews") or []
            token = resp.get("nextPageToken")
            if not token:
                break
        return {"reviews": out[:limit], "average": first.get("averageRating"), "total": first.get("totalReviewCount")}

    def reviews(self, grt: str, ref: str, only_unanswered: bool, max_stars: int | None, limit: int) -> dict[str, Any]:
        acct, loc = self.find(grt, ref)
        lid = loc_id(loc["name"])
        raw = self.reviews_raw(grt, acct, lid, max(limit, 200))
        rows = [review_row(r) for r in raw["reviews"]]
        answered = sum(1 for r in rows if r.get("reply"))
        recent = [r for r in raw["reviews"] if (_dt(r.get("createTime")) or datetime.min.replace(tzinfo=timezone.utc))
                  > datetime.now(timezone.utc) - timedelta(days=90)]
        stars = [r["stars"] for r in rows if r.get("stars")]
        if only_unanswered:
            rows = [r for r in rows if not r.get("reply")]
        if max_stars:
            rows = [r for r in rows if (r.get("stars") or 0) <= max_stars]
        return {
            "business": loc.get("title"), "location_id": lid,
            "average_rating": raw["average"], "total_reviews": raw["total"],
            "reply_rate": f"{answered / len(stars) * 100:.0f}% of the last {len(stars)}" if stars else None,
            "new_in_last_90_days": len(recent),
            "star_split_recent": {s: stars.count(s) for s in (5, 4, 3, 2, 1)},
            "shown": min(len(rows), limit), "reviews": rows[:limit],
            "tip": "To answer one, draft a reply (thank them by name, mention the service, no personal details) and "
                   "preview it with gbp_reply_review.",
        }

    def posts_raw(self, grt: str, acct: str, lid: str) -> list[dict[str, Any]]:
        return self.apis.list_all(grt, "gbp_v4", f"{self.v4(acct, lid)}/localPosts", "localPosts", {"pageSize": 100},
                                  limit=200)

    def posts(self, grt: str, ref: str, limit: int) -> dict[str, Any]:
        acct, loc = self.find(grt, ref)
        raw = self.posts_raw(grt, acct, loc_id(loc["name"]))
        rows = [{k: v for k, v in {
            "post_id": (p.get("name") or "").split("/")[-1], "type": p.get("topicType"), "state": p.get("state"),
            "text": p.get("summary"), "date": (p.get("createTime") or "")[:10],
            "button": (p.get("callToAction") or {}).get("actionType"), "link": (p.get("callToAction") or {}).get("url"),
            "event": (p.get("event") or {}).get("title"), "offer_code": (p.get("offer") or {}).get("couponCode"),
            "url": p.get("searchUrl"),
        }.items() if v} for p in raw]
        last = max((_dt(p.get("createTime")) for p in raw if _dt(p.get("createTime"))), default=None)
        return {"business": loc.get("title"), "count": len(rows), "last_post": last.date().isoformat() if last else None,
                "posts": rows[:limit]}

    def insights(self, grt: str, ref: str, days: int, start: str | None, end: str | None,
                 compare: bool, keywords: int) -> dict[str, Any]:
        acct, loc = self.find(grt, ref)
        lid = loc_id(loc["name"])
        if start or end:
            try:
                s, e = date.fromisoformat(start or ""), date.fromisoformat(end or "")
            except ValueError:
                raise AdsError("Give both start_date and end_date as YYYY-MM-DD.") from None
        else:
            e = date.today() - timedelta(days=3)  # Google's data is a few days behind
            s = e - timedelta(days=days - 1)
        if s > e:
            raise AdsError("start_date is after end_date.")

        def totals(s: date, e: date) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
            q: dict[str, Any] = {"dailyMetrics": list(METRICS)}
            q.update(_gdate(s, "dailyRange.startDate"))
            q.update(_gdate(e, "dailyRange.endDate"))
            resp = self.apis.call(grt, "gbp_perf", "GET", f"v1/locations/{lid}:fetchMultiDailyMetricsTimeSeries", params=q)
            tot = {v: 0 for v in METRICS.values()}
            by_month: dict[str, dict[str, int]] = {}
            for block in resp.get("multiDailyMetricTimeSeries") or []:
                for series in block.get("dailyMetricTimeSeries") or []:
                    name = METRICS.get(series.get("dailyMetric"))
                    if not name:
                        continue
                    for dv in (series.get("timeSeries") or {}).get("datedValues") or []:
                        v = int(dv.get("value") or 0)
                        tot[name] += v
                        d = dv.get("date") or {}
                        mk = f"{d.get('year')}-{d.get('month', 0):02d}"
                        by_month.setdefault(mk, {}).setdefault(name, 0)
                        by_month[mk][name] += v
            tot["views_total"] = sum(tot[k] for k in tot if k.startswith("views_"))
            tot["actions_total"] = tot["calls"] + tot["website_clicks"] + tot["direction_requests"] + tot["messages"] \
                + tot["bookings"] + tot["food_orders"]
            return tot, by_month

        now, by_month = totals(s, e)
        out: dict[str, Any] = {"business": loc.get("title"), "location_id": lid, "period": f"{s} to {e}",
                               "totals": {k: v for k, v in now.items() if v or k in ("views_total", "calls",
                                                                                     "website_clicks",
                                                                                     "direction_requests")}}
        if len(by_month) > 1:
            out["by_month"] = dict(sorted(by_month.items()))
        if compare:
            n = (e - s).days + 1
            ps, pe = s - timedelta(days=n), s - timedelta(days=1)
            prev, _ = totals(ps, pe)
            out["previous_period"] = f"{ps} to {pe}"
            out["change_vs_previous"] = {
                k: (f"{(now[k] - prev[k]) / prev[k] * 100:+.0f}%" if prev[k] else ("new" if now[k] else "0"))
                for k in ("views_total", "views_search_mobile", "views_maps_mobile", "calls", "website_clicks",
                          "direction_requests", "actions_total")}
        if keywords:
            # Keywords come per whole month: use the months the period covers, ending with the last complete one.
            em = e.replace(day=1) if (e + timedelta(days=1)).day == 1 else (e.replace(day=1) - timedelta(days=1)).replace(day=1)
            sm = min(s.replace(day=1), em)
            q = {"monthlyRange.startMonth.year": sm.year, "monthlyRange.startMonth.month": sm.month,
                 "monthlyRange.endMonth.year": em.year, "monthlyRange.endMonth.month": em.month, "pageSize": 100}
            try:
                kws = self.apis.list_all(grt, "gbp_perf", f"v1/locations/{lid}/searchkeywords/impressions/monthly",
                                         "searchKeywordsCounts", q, limit=keywords)
                out["search_keywords"] = [
                    {"keyword": k.get("searchKeyword"),
                     "views": int((k.get("insightsValue") or {}).get("value") or 0) or
                     f"under {(k.get('insightsValue') or {}).get('threshold')}"} for k in kws]
                out["search_keywords_months"] = f"{sm:%Y-%m} to {em:%Y-%m}"
            except AdsError as err:
                out["search_keywords_note"] = str(err)
        out["note"] = ("Views count each person once per day per device and platform. Search keyword counts below "
                       "15 are shown as 'under 15' by Google.")
        return out

    def audit(self, grt: str, ref: str) -> dict[str, Any]:
        """Profile optimisation check: what's missing or weak, ranked, with the tool that fixes it."""
        acct, loc = self.find(grt, ref)
        lid = loc_id(loc["name"])
        d = self.detail(grt, lid)
        issues: list[dict[str, Any]] = []

        def add(points: int, problem: str, fix: str):
            issues.append({"points": points, "problem": problem, "fix": fix})

        desc = ((d.get("profile") or {}).get("description") or "").strip()
        if not desc:
            add(12, "No business description.", "Write 500-750 characters with the main services and areas; "
                                                "gbp_update_profile(description=...).")
        elif len(desc) < 250:
            add(6, f"Description is short ({len(desc)}/750 characters).", "Expand it with services, areas and what "
                                                                          "makes them different (gbp_update_profile).")
        cats = d.get("categories") or {}
        if len(cats.get("additionalCategories") or []) < 2:
            add(8, f"Only {len(cats.get('additionalCategories') or [])} additional categories.",
                "Add the relevant ones (gbp_find_categories, then gbp_update_profile(additional_categories=...)).")
        if not d.get("websiteUri"):
            add(8, "No website.", "gbp_update_profile(website=...).")
        if not (d.get("phoneNumbers") or {}).get("primaryPhone"):
            add(8, "No phone number.", "gbp_update_profile(phone=...).")
        if not (d.get("regularHours") or {}).get("periods"):
            add(8, "No opening hours.", "gbp_update_profile(hours={...}).")
        if not d.get("serviceItems"):
            add(4, "No services listed.", "Add services in Business Profile (Edit profile > Services), or "
                                          "gbp_advanced_change with serviceItems.")
        if (d.get("openInfo") or {}).get("status") not in (None, "OPEN"):
            add(20, f"Profile status is {(d.get('openInfo') or {}).get('status')}.", "Check that's intended.")
        md = d.get("metadata") or {}
        if md.get("hasVoiceOfMerchant") is False:
            add(20, "Not verified, or suspended: changes and posts may not show.", "Verify it in Business Profile.")
        if md.get("hasGoogleUpdated"):
            add(4, "Google changed something on the profile.", "Review the Google update in Business Profile.")

        try:
            media = self.apis.call(grt, "gbp_v4", "GET", f"{self.v4(acct, lid)}/media", params={"pageSize": 100})
            items = media.get("mediaItems") or []
            n = int(media.get("totalMediaItemCount") or len(items))
            cats_seen = {(m.get("locationAssociation") or {}).get("category") for m in items}
            if n < 10:
                add(8, f"Only {n} photos.", "Aim for 10+ real photos (outside, inside, team, work); gbp_add_photo.")
            for c in ("LOGO", "COVER"):
                if c not in cats_seen:
                    add(3, f"No {c.lower()} photo.", f"gbp_add_photo(category='{c}').")
            newest = max((_dt(m.get("createTime")) for m in items if _dt(m.get("createTime"))), default=None)
            if newest and newest < datetime.now(timezone.utc) - timedelta(days=30):
                add(3, "No new photo in the last 30 days.", "Add fresh photos monthly (gbp_add_photo).")
        except AdsError as e:
            n = None
            add(0, f"Couldn't read photos: {e}", "")
        try:
            posts = self.posts_raw(grt, acct, lid)
            last = max((_dt(p.get("createTime")) for p in posts if _dt(p.get("createTime"))), default=None)
            if not last or last < datetime.now(timezone.utc) - timedelta(days=7):
                add(8, "No post in the last 7 days" + (f" (last {last.date()})." if last else " (never posted)."),
                    "Post weekly: an offer, a recent job, a tip (gbp_create_post).")
        except AdsError as e:
            add(0, f"Couldn't read posts: {e}", "")
        rev: dict[str, Any] = {}
        try:
            raw = self.reviews_raw(grt, acct, lid, 100)
            rs = raw["reviews"]
            unanswered = [r for r in rs if not r.get("reviewReply")]
            rev = {"average_rating": raw["average"], "total_reviews": raw["total"],
                   "unanswered_of_last_100": len(unanswered)}
            if unanswered:
                add(min(12, 2 + len(unanswered)), f"{len(unanswered)} of the last {len(rs)} reviews have no reply.",
                    "gbp_reviews(only_unanswered=true), then gbp_reply_review for each.")
            if raw["average"] and float(raw["average"]) < 4.3:
                add(6, f"Average rating {raw['average']}.", "Ask happy customers for reviews with the review link "
                                                            f"{md.get('newReviewUri') or ''}".strip())
            recent = [r for r in rs if (_dt(r.get("createTime")) or datetime.min.replace(tzinfo=timezone.utc))
                      > datetime.now(timezone.utc) - timedelta(days=30)]
            if not recent:
                add(5, "No new reviews in the last 30 days.", "Send the review link after each job: "
                                                             f"{md.get('newReviewUri') or '(see gbp_list_locations)'}")
        except AdsError as e:
            add(0, f"Couldn't read reviews: {e}", "")

        score = max(0, 100 - sum(i["points"] for i in issues))
        return {
            "business": d.get("title"), "location_id": lid, "score": score,
            "profile": {**summary(d, acct), "description": desc or None, "hours": hours_text(d.get("regularHours")),
                        "additional_categories": [c.get("displayName") for c in cats.get("additionalCategories") or []],
                        "photos": n},
            "reviews": rev,
            "issues": sorted([i for i in issues if i["points"]], key=lambda i: -i["points"])
            + [i for i in issues if not i["points"]],
            "next_step": "Fix the biggest issues first. Each fix tool only previews; nothing changes without a yes. "
                         "For local rankings, run rank_grid_scan with the main keywords.",
        }

    def categories(self, grt: str, search: str, region: str, language: str) -> dict[str, Any]:
        resp = self.apis.call(grt, "gbp_info", "GET", "v1/categories", params={
            "regionCode": region.upper(), "languageCode": language, "filter": f"displayName={search}",
            "view": "BASIC", "pageSize": 50})
        return {"categories": [{"id": c.get("name", "").removeprefix("categories/"), "name": c.get("displayName")}
                               for c in resp.get("categories") or []]}

    # ---------------------------------------------------------------- changes

    def _where(self, loc: dict[str, Any]) -> str:
        return f"Business Profile '{loc.get('title')}' ({address_text(loc.get('storefrontAddress')) or 'service area'})"

    def reply(self, grt: str, ref: str, review_id: str, text: str | None, delete: bool) -> Change:
        acct, loc = self.find(grt, ref)
        lid = loc_id(loc["name"])
        rid = str(review_id or "").strip().split("/")[-1]
        if not rid:
            raise AdsError("Give the review_id (gbp_reviews shows them).")
        rpath = f"{self.v4(acct, lid)}/reviews/{rid}"
        r = self.apis.call(grt, "gbp_v4", "GET", rpath)
        row = review_row(r)
        old = row.get("reply")
        who = f"{row.get('stars', '?')}-star review by {row.get('by') or 'a customer'}"
        ch = Change(None, "", where=self._where(loc))
        put_back = ({"a": "gbp_v4", "m": "PUT", "p": f"{rpath}/reply", "b": {"comment": old},
                     "d": f"put the old reply back on the {who}"} if old else None)
        if delete:
            if not old:
                raise AdsError("That review has no reply to delete.")
            ch.title = f"Delete the reply to the {who}"
            ch.lines.append(f"delete the reply: {old!r}")
            ch.gtm.append({"a": "gbp_v4", "m": "DELETE", "p": f"{rpath}/reply", "d": f"delete the reply to the {who}",
                           "u": put_back})
            return ch
        text = str(text or "").strip()
        if not text:
            raise AdsError("Give the reply text.")
        if len(text) > 4000:
            raise AdsError(f"Replies can be at most 4,000 characters (this is {len(text)}).")
        ch.title = ("Change the reply to" if old else "Reply to") + f" the {who}"
        ch.lines.append(f"review: {row.get('text') or '(no text, stars only)'!r}")
        if old:
            ch.lines.append(f"old reply: {old!r}")
        ch.lines.append(f"reply: {text!r}")
        ch.gtm.append({"a": "gbp_v4", "m": "PUT", "p": f"{rpath}/reply", "b": {"comment": text},
                       "d": f"reply to the {who}",
                       "u": put_back or {"a": "gbp_v4", "m": "DELETE", "p": f"{rpath}/reply",
                                         "d": f"delete the reply to the {who}"}})
        ch.warnings.append("Replies are public and show the business name. Google may take a few minutes to show it.")
        if _PHONE_IN_TEXT.search(text) or "@" in text:
            ch.warnings.append("The reply has a phone number or email: fine for the business's own, but never repeat "
                               "the customer's details.")
        return ch

    def post(self, grt: str, ref: str, a: dict[str, Any]) -> Change:
        acct, loc = self.find(grt, ref)
        lid = loc_id(loc["name"])
        text = str(a.get("text") or "").strip()
        kind = (a.get("type") or "STANDARD").upper()
        if kind not in ("STANDARD", "EVENT", "OFFER"):
            raise AdsError("type is STANDARD (update), EVENT or OFFER.")
        if not text and kind == "STANDARD":
            raise AdsError("Give the post text.")
        if len(text) > 1500:
            raise AdsError(f"Posts can be at most 1,500 characters (this is {len(text)}).")
        body: dict[str, Any] = {"languageCode": a.get("language") or "en", "topicType": kind}
        if text:
            body["summary"] = text
        lines = [f"{kind.lower()} post: {text!r}" if text else f"{kind.lower()} post"]
        btn = (a.get("button") or "").upper() or None
        if btn:
            if btn not in CTA_TYPES:
                raise AdsError(f"button is one of {', '.join(CTA_TYPES)}.")
            if btn != "CALL" and not a.get("link"):
                raise AdsError(f"The {btn} button needs a link (https://...).")
            body["callToAction"] = {"actionType": btn, **({"url": a["link"]} if btn != "CALL" else {})}
            lines.append(f"button: {btn}" + (f" -> {a['link']}" if btn != "CALL" else " (calls the profile's phone)"))
        if a.get("image_url"):
            if not str(a["image_url"]).startswith("https://"):
                raise AdsError("image_url must be a public https:// address of a JPG or PNG.")
            body["media"] = [{"mediaFormat": "PHOTO", "sourceUrl": a["image_url"]}]
            lines.append(f"photo: {a['image_url']}")
        if kind in ("EVENT", "OFFER"):
            title = str(a.get("title") or "").strip()
            if not title:
                raise AdsError(f"An {kind.lower()} post needs a title.")
            if len(title) > 58:
                raise AdsError("Event/offer titles can be at most 58 characters.")
            if not a.get("start") or not a.get("end"):
                raise AdsError(f"An {kind.lower()} post needs start and end dates (YYYY-MM-DD).")
            sd, st = _event_date(a["start"], "start")
            ed, et = _event_date(a["end"], "end")
            sched: dict[str, Any] = {"startDate": sd, "endDate": ed}
            if st:
                sched["startTime"] = st
            if et:
                sched["endTime"] = et
            body["event"] = {"title": title, "schedule": sched}
            lines.append(f"{kind.lower()}: {title!r} from {a['start']} to {a['end']}")
        if kind == "OFFER":
            offer = {k2: a[k1] for k1, k2 in (("coupon_code", "couponCode"), ("redeem_url", "redeemOnlineUrl"),
                                              ("terms", "termsConditions")) if a.get(k1)}
            if offer:
                body["offer"] = offer
                lines.append("offer details: " + ", ".join(f"{k}={v!r}" for k, v in offer.items()))
        ch = Change(None, f"New Business Profile post for {loc.get('title')}", where=self._where(loc))
        ch.lines += lines
        ch.gtm.append({"a": "gbp_v4", "m": "POST", "p": f"{self.v4(acct, lid)}/localPosts", "b": body, "s": "post",
                       "f": "name", "d": f"publish the post on {loc.get('title')}",
                       "u": {"a": "gbp_v4", "m": "DELETE", "p": "v4/${post}", "d": f"delete the post on {loc.get('title')}"}})
        if _PHONE_IN_TEXT.search(text):
            ch.warnings.append("Google usually rejects posts with a phone number in the text. Use the CALL button instead.")
        ch.warnings.append("Posts go live right away (after Google's automatic review) and stay visible about 6 months. "
                           "Use the business's own photos; Google rejects stock images with text.")
        return ch

    def delete_post(self, grt: str, ref: str, post_id: str) -> Change:
        acct, loc = self.find(grt, ref)
        pid = str(post_id or "").strip().split("/")[-1]
        path = f"{self.v4(acct, loc_id(loc['name']))}/localPosts/{pid}"
        p = self.apis.call(grt, "gbp_v4", "GET", path)
        ch = Change(None, f"Delete a post on {loc.get('title')}", where=self._where(loc))
        ch.lines.append(f"delete the {(p.get('topicType') or '').lower()} post from {(p.get('createTime') or '')[:10]}: "
                        f"{p.get('summary')!r}")
        ch.gtm.append({"a": "gbp_v4", "m": "DELETE", "p": path, "d": "delete the post",
                       "n": "deleting a post (create it again to put it back)"})
        ch.large.append("deleting a post can't be undone")
        return ch

    def photo(self, grt: str, ref: str, image_url: str, category: str, description: str | None) -> Change:
        acct, loc = self.find(grt, ref)
        cat = (category or "ADDITIONAL").upper()
        if cat not in PHOTO_CATEGORIES:
            raise AdsError(f"category is one of {', '.join(PHOTO_CATEGORIES)}.")
        if not str(image_url or "").startswith("https://"):
            raise AdsError("image_url must be a public https:// address of a JPG or PNG (10 KB to 5 MB, 720x720+).")
        body: dict[str, Any] = {"mediaFormat": "PHOTO", "locationAssociation": {"category": cat}, "sourceUrl": image_url}
        if description:
            body["description"] = description
        ch = Change(None, f"Add a {cat.lower().replace('_', ' ')} photo to {loc.get('title')}", where=self._where(loc))
        ch.lines.append(f"photo {image_url} as {cat}")
        ch.gtm.append({"a": "gbp_v4", "m": "POST", "p": f"{self.v4(acct, loc_id(loc['name']))}/media", "b": body,
                       "s": "media", "f": "name", "d": f"add the photo to {loc.get('title')}",
                       "u": {"a": "gbp_v4", "m": "DELETE", "p": "v4/${media}", "d": "delete the photo"}})
        if cat in ("COVER", "LOGO", "PROFILE"):
            ch.warnings.append(f"This replaces the current {cat.lower()} photo. Google may still choose which photo "
                               "it shows first.")
        return ch

    def update_profile(self, grt: str, ref: str, a: dict[str, Any]) -> Change:
        _, loc = self.find(grt, ref)
        lid = loc_id(loc["name"])
        d = self.detail(grt, lid)
        new: dict[str, Any] = {}
        old: dict[str, Any] = {}
        mask: list[str] = []
        lines: list[str] = []
        large: list[str] = []

        def put(field: str, new_part: dict[str, Any], old_part: dict[str, Any], line: str):
            mask.append(field)
            for k, v in new_part.items():
                if isinstance(v, dict) and isinstance(new.get(k), dict):
                    new[k].update(v)
                else:
                    new[k] = v
            for k, v in old_part.items():
                if isinstance(v, dict) and isinstance(old.get(k), dict):
                    old[k].update(v)
                else:
                    old[k] = v
            lines.append(line)

        if a.get("description") is not None:
            desc = str(a["description"]).strip()
            if len(desc) > 750:
                raise AdsError(f"The description can be at most 750 characters (this is {len(desc)}).")
            if re.search(r"https?://", desc):
                raise AdsError("Google doesn't allow links in the description.")
            cur = (d.get("profile") or {}).get("description")
            put("profile.description", {"profile": {"description": desc}}, {"profile": {"description": cur or ""}},
                f"description: {cur!r} -> {desc!r}")
        if a.get("website") is not None:
            put("websiteUri", {"websiteUri": a["website"]}, {"websiteUri": d.get("websiteUri") or ""},
                f"website: {d.get('websiteUri')!r} -> {a['website']!r}")
        if a.get("phone") is not None:
            cur = (d.get("phoneNumbers") or {}).get("primaryPhone")
            put("phoneNumbers.primaryPhone", {"phoneNumbers": {"primaryPhone": a["phone"]}},
                {"phoneNumbers": {"primaryPhone": cur or ""}}, f"phone: {cur!r} -> {a['phone']!r}")
            large.append("changing the phone number (Google may ask to verify the profile again)")
        if a.get("hours") is not None:
            rh = parse_hours(a["hours"])
            put("regularHours", {"regularHours": rh}, {"regularHours": d.get("regularHours") or {"periods": []}},
                f"hours: {hours_text(d.get('regularHours'))} -> {hours_text(rh)}")
        cats = d.get("categories") or {}
        if a.get("primary_category") or a.get("additional_categories") is not None:
            primary = cats.get("primaryCategory") or {}
            extra = cats.get("additionalCategories") or []
            new_primary = {"name": "categories/" + str(a["primary_category"]).removeprefix("categories/")} \
                if a.get("primary_category") else {"name": primary.get("name")}
            new_extra = [{"name": "categories/" + str(c).removeprefix("categories/")}
                         for c in a["additional_categories"]] if a.get("additional_categories") is not None \
                else [{"name": c.get("name")} for c in extra]
            if len(new_extra) > 9:
                raise AdsError("A profile can have at most 9 additional categories.")
            put("categories", {"categories": {"primaryCategory": new_primary, "additionalCategories": new_extra}},
                {"categories": {"primaryCategory": {"name": primary.get("name")},
                                "additionalCategories": [{"name": c.get("name")} for c in extra]}},
                f"categories: {primary.get('displayName')} + {[c.get('displayName') for c in extra]} -> "
                f"{new_primary['name'].removeprefix('categories/')} + "
                f"{[c['name'].removeprefix('categories/') for c in new_extra]}")
            if a.get("primary_category") and new_primary["name"] != primary.get("name"):
                large.append("changing the primary category (it's the biggest ranking factor and can trigger re-review)")
        if a.get("name") is not None:
            put("title", {"title": a["name"]}, {"title": d.get("title")}, f"business name: {d.get('title')!r} -> "
                                                                          f"{a['name']!r}")
            large.append("changing the business name (must match the real-world name, or the profile can be "
                         "suspended; Google may re-verify)")
        if not mask:
            raise AdsError("Say what to change: description, website, phone, hours, primary_category, "
                           "additional_categories or name.")
        ch = Change(None, f"Update the Business Profile of {d.get('title')}", where=self._where(loc))
        ch.lines += lines
        ch.large += large
        um = ",".join(mask)
        ch.gtm.append({"a": "gbp_info", "m": "PATCH", "p": f"v1/locations/{lid}", "q": {"updateMask": um}, "b": new,
                       "d": f"update {', '.join(mask)}",
                       "u": {"a": "gbp_info", "m": "PATCH", "p": f"v1/locations/{lid}", "q": {"updateMask": um},
                             "b": old, "d": f"put {', '.join(mask)} back"}})
        ch.warnings.append("Google reviews profile edits; they can take from minutes to a few days to show.")
        with self._lock:
            self._cache.clear()
        return ch


Location = Annotated[str, Field(description="The Business Profile: location_id or business name (gbp_list_locations).")]


def register_gbp_tools(mcp: FastMCP, w: Writer, apis: GoogleApis) -> BusinessProfile:
    bp = BusinessProfile(w, apis)

    def change(tool: str, build):
        return w.run(tool, None, lambda user, grt: w.preview(user, grt, build(grt)))

    @mcp.tool(annotations=READ_ONLY)
    async def gbp_list_locations(
        search: Annotated[str | None, Field(description="Only profiles whose name or address contains this.")] = None,
        refresh: Annotated[bool, Field(description="Re-read from Google instead of the 10-minute cache.")] = False,
    ) -> dict[str, Any]:
        """List every Google Business Profile your Google login manages: name, address, category, phone, website,
        place ID, Maps link, review link and problems (not verified, Google updates)."""
        return await w.run("gbp_list_locations", None, lambda user, grt: bp.accounts_and_locations(grt, search, refresh))

    @mcp.tool(annotations=READ_ONLY)
    async def gbp_profile_audit(location: Location) -> dict[str, Any]:
        """Check a Business Profile for what's missing or weak (description, categories, hours, photos, posts,
        reviews and replies, verification) and give a 0-100 score with fixes ranked by impact."""
        return await w.run("gbp_profile_audit", None, lambda user, grt: bp.audit(grt, location))

    @mcp.tool(annotations=READ_ONLY)
    async def gbp_reviews(
        location: Location,
        only_unanswered: Annotated[bool, Field(description="Only reviews with no reply yet.")] = False,
        max_stars: Annotated[int | None, Field(ge=1, le=5, description="Only reviews with this many stars or fewer.")] = None,
        max_items: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        """A profile's Google reviews, newest first, with average rating, total, reply rate and star split."""
        return await w.run("gbp_reviews", None, lambda user, grt: bp.reviews(grt, location, only_unanswered,
                                                                            max_stars, max_items))

    @mcp.tool(annotations=READ_ONLY)
    async def gbp_posts(location: Location, max_items: Annotated[int, Field(ge=1, le=200)] = 30) -> dict[str, Any]:
        """A profile's posts (updates, offers, events) with dates, buttons and status."""
        return await w.run("gbp_posts", None, lambda user, grt: bp.posts(grt, location, max_items))

    @mcp.tool(annotations=READ_ONLY)
    async def gbp_insights(
        location: Location,
        days: Annotated[int, Field(ge=7, le=540, description="Last N days (ending 3 days ago).")] = 30,
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD (instead of days)")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        compare_previous: Annotated[bool, Field(description="Also compare with the period before.")] = True,
        search_keywords: Annotated[int, Field(ge=0, le=500, description="How many search keywords to include (0 = none).")] = 50,
    ) -> dict[str, Any]:
        """How a profile performs: views on Google Search and Maps (mobile/desktop), calls, website clicks,
        direction requests, messages and bookings, versus the period before, plus the searches people used to
        find it (monthly)."""
        return await w.run("gbp_insights", None, lambda user, grt: bp.insights(
            grt, location, days, start_date, end_date, compare_previous, search_keywords))

    @mcp.tool(annotations=READ_ONLY)
    async def gbp_find_categories(
        search: Annotated[str, Field(description="Part of a category name, e.g. 'plumb' or 'dentist'.")],
        region_code: Annotated[str, Field(description="Country, e.g. US, AE, GB, AU.")] = "US",
        language: str = "en",
    ) -> dict[str, Any]:
        """Find Business Profile category IDs (e.g. gcid:plumber) to use with gbp_update_profile."""
        return await w.run("gbp_find_categories", None, lambda user, grt: bp.categories(grt, search, region_code, language))

    @mcp.tool(annotations=PREVIEW)
    async def gbp_reply_review(
        location: Location,
        review_id: Annotated[str, Field(description="From gbp_reviews.")],
        reply: Annotated[str | None, Field(description="The reply text (replaces any earlier reply).")] = None,
        delete: Annotated[bool, Field(description="true = delete the existing reply instead.")] = False,
    ) -> dict[str, Any]:
        """Preview replying to a Google review (or changing / deleting the reply). Undo puts the old reply back."""
        return await change("gbp_reply_review", lambda grt: bp.reply(grt, location, review_id, reply, delete))

    @mcp.tool(annotations=PREVIEW)
    async def gbp_create_post(
        location: Location,
        text: Annotated[str | None, Field(description="Post text, up to 1,500 characters (no phone numbers).")] = None,
        type: Annotated[Literal["STANDARD", "EVENT", "OFFER"], Field(description="STANDARD = update.")] = "STANDARD",
        button: Annotated[str | None, Field(description="BOOK, ORDER, SHOP, LEARN_MORE, SIGN_UP or CALL.")] = None,
        link: Annotated[str | None, Field(description="Where the button goes (not needed for CALL).")] = None,
        image_url: Annotated[str | None, Field(description="Public https:// JPG/PNG of the business's own photo.")] = None,
        title: Annotated[str | None, Field(description="Event or offer title (max 58 characters).")] = None,
        start: Annotated[str | None, Field(description="Event/offer start: YYYY-MM-DD or 'YYYY-MM-DD HH:MM'.")] = None,
        end: Annotated[str | None, Field(description="Event/offer end.")] = None,
        coupon_code: str | None = None,
        redeem_url: str | None = None,
        terms: str | None = None,
        language: str = "en",
    ) -> dict[str, Any]:
        """Preview a new Business Profile post (update, offer or event) with optional photo and button.
        Undo deletes the post."""
        a = dict(text=text, type=type, button=button, link=link, image_url=image_url, title=title, start=start, end=end,
                 coupon_code=coupon_code, redeem_url=redeem_url, terms=terms, language=language)
        return await change("gbp_create_post", lambda grt: bp.post(grt, location, a))

    @mcp.tool(annotations=PREVIEW)
    async def gbp_delete_post(location: Location, post_id: Annotated[str, Field(description="From gbp_posts.")]) -> dict[str, Any]:
        """Preview deleting a Business Profile post (can't be undone; extra confirmation)."""
        return await change("gbp_delete_post", lambda grt: bp.delete_post(grt, location, post_id))

    @mcp.tool(annotations=PREVIEW)
    async def gbp_add_photo(
        location: Location,
        image_url: Annotated[str, Field(description="Public https:// JPG/PNG, 720x720 or bigger.")],
        category: Annotated[str, Field(description=", ".join(PHOTO_CATEGORIES))] = "ADDITIONAL",
        description: str | None = None,
    ) -> dict[str, Any]:
        """Preview adding a photo (cover, logo, exterior, interior, team, at work, product...). Undo deletes it."""
        return await change("gbp_add_photo", lambda grt: bp.photo(grt, location, image_url, category, description))

    @mcp.tool(annotations=PREVIEW)
    async def gbp_update_profile(
        location: Location,
        description: Annotated[str | None, Field(description="Up to 750 characters, no links.")] = None,
        website: str | None = None,
        phone: Annotated[str | None, Field(description="Primary phone, e.g. +971 4 123 4567.")] = None,
        hours: Annotated[dict[str, str] | None, Field(description=(
            "Opening hours for the whole week, e.g. {MONDAY: '09:00-18:00', SATURDAY: '10:00-14:00', SUNDAY: 'closed'}. "
            "Days left out are closed. Split shifts: '09:00-12:00, 13:00-18:00'. '24h' = open all day."))] = None,
        primary_category: Annotated[str | None, Field(description="Category ID from gbp_find_categories, e.g. gcid:plumber.")] = None,
        additional_categories: Annotated[list[str] | None, Field(description="The full list of additional category IDs (replaces the current list).")] = None,
        name: Annotated[str | None, Field(description="Business name. Only to match the real-world name.")] = None,
    ) -> dict[str, Any]:
        """Preview editing a Business Profile: description, website, phone, opening hours, categories or name.
        Undo puts the old values back."""
        a = dict(description=description, website=website, phone=phone, hours=hours, primary_category=primary_category,
                 additional_categories=additional_categories, name=name)
        return await change("gbp_update_profile", lambda grt: bp.update_profile(grt, location, a))

    @mcp.tool(annotations=PREVIEW)
    async def gbp_advanced_change(
        calls: Annotated[list[dict[str, Any]], Field(description=(
            "Raw Business Profile API calls: [{api: 'gbp_info'|'gbp_v4', method: POST|PATCH|PUT|DELETE, path: "
            "'v1/locations/123', params: {updateMask: 'serviceItems'}, body: {...}}]. gbp_info = Business Information "
            "API v1 (locations, attributes, services, special hours); gbp_v4 = reviews, posts, media."))],
        description: Annotated[str, Field(description="Plain-language summary of what this does, shown to the person.")],
    ) -> dict[str, Any]:
        """Preview any other Business Profile change with raw API calls (special hours, attributes, services,
        service area...). Always needs extra confirmation."""
        def build(grt):
            if not calls or len(calls) > 20:
                raise AdsError("Give 1 to 20 calls.")
            ch = None
            for c in calls:  # one at a time, so the order is kept even when the APIs differ
                api = c.get("api") or "gbp_info"
                if api not in ("gbp_info", "gbp_v4"):
                    raise AdsError("api is gbp_info or gbp_v4.")
                part = raw_change(api, "Business Profile", [c], description)
                if ch is None:
                    ch = part
                else:
                    ch.gtm += part.gtm
                    ch.lines += part.lines
            return ch
        return await change("gbp_advanced_change", build)

    return bp
