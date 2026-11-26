"""Phase 6: Google Business Profile and the free rank tracker. Google is faked."""

from datetime import date, timedelta

import pytest

from app.ads import AdsError
from app.auth import ADS_SCOPES, GMB_SCOPES
from app.gbp import BusinessProfile, hours_text, parse_hours
from app.google_apis import explain
from app.ranktrack import FREE_MASK, Places, RankTracker, grid_points
from tests.test_phase5 import FakeApis
from tests.test_writes import make

U = "w@z.test"
ACC = "accounts/1"
LOC = {"name": "locations/22", "title": "Marina Plumbing", "storefrontAddress": {"addressLines": ["1 Marina Walk"],
       "locality": "Dubai", "regionCode": "AE"}, "metadata": {"placeId": "ChIJtarget", "newReviewUri": "https://g.page/r/x"},
       "latlng": {"latitude": 25.08, "longitude": 55.14}, "categories": {"primaryCategory": {"name": "categories/gcid:plumber",
                                                                                             "displayName": "Plumber"}}}
LOC2 = {"name": "locations/33", "title": "Marina Plumbing JLT", "metadata": {}}
REVIEWS = {"averageRating": 4.1, "totalReviewCount": 3, "reviews": [
    {"reviewId": "r1", "starRating": "FIVE", "reviewer": {"displayName": "Sara"}, "comment": "Fast and tidy",
     "createTime": "2026-11-20T10:00:00Z"},
    {"reviewId": "r2", "starRating": "TWO", "reviewer": {"displayName": "Omar"}, "comment": "Late",
     "createTime": "2026-10-01T10:00:00Z", "reviewReply": {"comment": "Sorry Omar", "updateTime": "2026-10-02T00:00:00Z"}},
    {"reviewId": "r3", "starRating": "FOUR", "createTime": "2025-01-01T10:00:00Z"},
]}
V4 = "v4/accounts/1/locations/22"


def env(extra=None):
    routes = {
        ("gbp_accounts", "GET", "v1/accounts"): {"accounts": [{"name": ACC, "accountName": "Zyppia"}]},
        ("gbp_info", "GET", "v1/accounts/1/locations"): {"locations": [LOC, LOC2]},
        ("gbp_v4", "GET", f"{V4}/reviews"): REVIEWS,
        ("gbp_v4", "GET", f"{V4}/reviews/r1"): REVIEWS["reviews"][0],
        ("gbp_v4", "GET", f"{V4}/reviews/r2"): REVIEWS["reviews"][1],
    }
    routes.update(extra or {})
    w, ads = make()
    apis = FakeApis(ads, routes)
    w.apis = apis
    return w, apis, BusinessProfile(w, apis)


def test_scope_and_plain_errors():
    assert "https://www.googleapis.com/auth/business.manage" in GMB_SCOPES
    assert "https://www.googleapis.com/auth/business.manage" not in ADS_SCOPES
    msg = explain("gbp_info", 429, "Quota exceeded for quota metric 'Requests' and limit 'Requests per minute'")
    assert "approved" in msg and "support.google.com/business/contact/api_default" in msg
    assert "Manager" in explain("gbp_v4", 403, "The caller does not have permission")


def test_find_location_by_id_name_and_ambiguity():
    _, _, bp = env()
    assert bp.find("grt", "22")[1]["title"] == "Marina Plumbing"
    assert bp.find("grt", "locations/33")[1]["title"] == "Marina Plumbing JLT"
    assert bp.find("grt", "marina plumbing")[1]["name"] == "locations/22"  # exact name wins
    with pytest.raises(AdsError, match="matches 2 profiles"):
        bp.find("grt", "marina")
    with pytest.raises(AdsError, match="doesn't manage"):
        bp.find("grt", "nobody")
    out = bp.accounts_and_locations("grt", None, False)
    assert out["count"] == 2 and out["locations"][0]["place_id"] == "ChIJtarget"


def test_reviews_summary():
    _, _, bp = env()
    out = bp.reviews("grt", "22", only_unanswered=True, max_stars=None, limit=10)
    assert out["average_rating"] == 4.1 and out["reply_rate"] == "33% of the last 3"
    assert [r["review_id"] for r in out["reviews"]] == ["r1", "r3"]


def test_reply_new_and_undo_deletes_it():
    w, apis, bp = env()
    p = w.preview(U, "grt", bp.reply("grt", "22", "r1", "Thanks Sara!", False))
    assert "no test mode" in p["status"] and any("Thanks Sara!" in line for line in p["details"])
    done = w.apply(U, "grt", p["confirm_token"], False)
    assert apis.calls[-1][1:3] == ("PUT", f"{V4}/reviews/r1/reply") and apis.calls[-1][4] == {"comment": "Thanks Sara!"}
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    assert apis.calls[-1][1:3] == ("DELETE", f"{V4}/reviews/r1/reply")


def test_changing_a_reply_undo_puts_old_back():
    w, apis, bp = env()
    p = w.preview(U, "grt", bp.reply("grt", "22", "r2", "Sorry for the wait, Omar.", False))
    done = w.apply(U, "grt", p["confirm_token"], False)
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    assert apis.calls[-1][1] == "PUT" and apis.calls[-1][4] == {"comment": "Sorry Omar"}
    with pytest.raises(AdsError, match="no reply to delete"):
        bp.reply("grt", "22", "r1", None, True)


def test_post_offer_and_undo_deletes_created_post():
    w, apis, bp = env()
    with pytest.raises(AdsError, match="needs a link"):
        bp.post("grt", "22", {"text": "Hi", "button": "BOOK"})
    with pytest.raises(AdsError, match="start and end"):
        bp.post("grt", "22", {"text": "Hi", "type": "OFFER", "title": "10% off"})
    ch = bp.post("grt", "22", {"text": "10% off drain cleaning this week. Call 050 123 4567", "type": "OFFER",
                               "title": "10% off drains", "start": "2026-12-01", "end": "2026-12-07 18:00",
                               "coupon_code": "DRAIN10", "button": "CALL"})
    p = w.preview(U, "grt", ch)
    assert any("phone number" in x for x in p["warnings"])
    done = w.apply(U, "grt", p["confirm_token"], False)
    post = apis.calls[-1]
    assert post[2] == f"{V4}/localPosts"
    body = post[4]
    assert body["topicType"] == "OFFER" and body["offer"] == {"couponCode": "DRAIN10"}
    assert body["event"]["schedule"]["endTime"] == {"hours": 18, "minutes": 0}
    assert body["callToAction"] == {"actionType": "CALL"}
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    assert apis.calls[-1][1:3] == ("DELETE", "v4/accounts/1/locations/22/localPosts/1")


def test_hours_parsing():
    rh = parse_hours({"mon": "09:00-17:00", "Fri": "09:00-12:00, 14:00-18:00", "SAT": "18:00-02:00",
                      "sunday": "closed", "TUESDAY": "24h"})
    p = rh["periods"]
    assert {"openDay": "SATURDAY", "openTime": {"hours": 18, "minutes": 0}, "closeDay": "SUNDAY",
            "closeTime": {"hours": 2, "minutes": 0}} in p
    assert any(x["openDay"] == "TUESDAY" and x["closeTime"]["hours"] == 24 for x in p)
    assert hours_text(rh)["FRIDAY"] == "09:00-12:00, 14:00-18:00" and hours_text(rh)["SUNDAY"] == "closed"
    with pytest.raises(AdsError):
        parse_hours({"MONDAY": "9 to 5"})
    with pytest.raises(AdsError):
        parse_hours({"Funday": "09:00-17:00"})


def test_update_profile_with_undo_and_flags():
    detail = {**LOC, "profile": {"description": "Old"}, "phoneNumbers": {"primaryPhone": "+971 4 000 0000"},
              "websiteUri": "https://old.ae"}
    w, apis, bp = env({("gbp_info", "GET", "v1/locations/22"): detail})
    with pytest.raises(AdsError, match="750"):
        bp.update_profile("grt", "22", {"description": "x" * 751})
    with pytest.raises(AdsError, match="links"):
        bp.update_profile("grt", "22", {"description": "see https://x.ae"})
    ch = bp.update_profile("grt", "22", {"description": "Licensed plumbers in Dubai Marina.", "phone": "+971 4 123 4567",
                                         "hours": {"MONDAY": "08:00-20:00"}, "primary_category": "gcid:emergency_plumber"})
    p = w.preview(U, "grt", ch)
    assert len(p["needs_extra_confirmation"]) == 2
    with pytest.raises(AdsError, match="extra confirmation"):
        w.apply(U, "grt", p["confirm_token"], False)
    done = w.apply(U, "grt", p["confirm_token"], True)
    patch = apis.calls[-1]
    assert patch[1:3] == ("PATCH", "v1/locations/22")
    assert patch[3]["updateMask"] == "profile.description,phoneNumbers.primaryPhone,regularHours,categories"
    assert patch[4]["categories"]["primaryCategory"] == {"name": "categories/gcid:emergency_plumber"}
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    back = apis.calls[-1][4]
    assert back["profile"]["description"] == "Old" and back["phoneNumbers"]["primaryPhone"] == "+971 4 000 0000"
    assert back["categories"]["primaryCategory"] == {"name": "categories/gcid:plumber"}


def test_insights_totals_and_comparison():
    def perf(params, body):
        start = params["dailyRange.startDate.day"]
        v = "10" if start == params.get("_never") else ("30" if date.today().day else "0")
        series = [{"dailyMetric": m, "timeSeries": {"datedValues": [{"date": {"year": 2026, "month": 11, "day": 1},
                                                                       "value": v}]}}
                  for m in ("CALL_CLICKS", "BUSINESS_IMPRESSIONS_MOBILE_MAPS")]
        return {"multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": series}]}
    kw = {"searchKeywordsCounts": [{"searchKeyword": "plumber marina", "insightsValue": {"value": "120"}},
                                   {"searchKeyword": "drain", "insightsValue": {"threshold": "15"}}]}
    w, apis, bp = env({("gbp_perf", "GET", "v1/locations/22:fetchMultiDailyMetricsTimeSeries"): perf,
                       ("gbp_perf", "GET", "v1/locations/22/searchkeywords/impressions/monthly"): kw})
    out = bp.insights("grt", "22", 30, None, None, True, 10)
    assert out["totals"]["calls"] == 30 and out["totals"]["views_total"] == 30
    assert out["change_vs_previous"]["calls"] == "+0%"
    assert out["search_keywords"] == [{"keyword": "plumber marina", "views": 120}, {"keyword": "drain", "views": "under 15"}]
    q = [c for c in apis.calls if c[2].endswith("fetchMultiDailyMetricsTimeSeries")][0][3]
    assert "CALL_CLICKS" in q["dailyMetrics"]
    end = date.today() - timedelta(days=3)
    assert (q["dailyRange.endDate.year"], q["dailyRange.endDate.month"], q["dailyRange.endDate.day"]) == \
        (end.year, end.month, end.day)


def test_audit_scores_gaps():
    detail = {**LOC, "profile": {}, "regularHours": {}, "metadata": {**LOC["metadata"], "hasVoiceOfMerchant": True}}
    w, apis, bp = env({("gbp_info", "GET", "v1/locations/22"): detail,
                       ("gbp_v4", "GET", f"{V4}/media"): {"mediaItems": [], "totalMediaItemCount": 0},
                       ("gbp_v4", "GET", f"{V4}/localPosts"): {"localPosts": []}})
    out = bp.audit("grt", "22")
    problems = " ".join(i["problem"] for i in out["issues"])
    for words in ("No business description", "No website", "No opening hours", "Only 0 photos", "never posted",
                  "have no reply", "Average rating 4.1"):
        assert words in problems
    assert 0 <= out["score"] < 50


# ------------------------------------------------------------------ rank tracker


class FakePlaces(Places):
    """Ranks depend on how far east the searcher is: target is #1 at the west edge and drops eastwards."""

    def __init__(self, settings):
        super().__init__(settings)
        self.key = "k"
        self.masks = []

    def _req(self, method, path, mask, body=None):
        self.masks.append((path, mask))
        if path == "places:searchText" and mask == FREE_MASK:
            lng = body["locationBias"]["circle"]["center"]["longitude"]
            shift = max(0, round((lng - 55.14) * 100))  # 0 at centre/west, grows east
            ids = [f"comp{i}" for i in range(60)]
            ids.insert(min(shift, 59), "ChIJtarget")
            start = int(body.get("pageToken") or 0)
            page = ids[start:start + 20]
            return {"places": [{"id": i} for i in page], **({"nextPageToken": str(start + 20)} if start + 20 < 60 else {})}
        if path.startswith("places/"):
            return {"displayName": {"text": f"Name {path[7:]}"}, "formattedAddress": "Dubai",
                    "location": {"latitude": 25.0, "longitude": 55.0}}
        return {"places": [{"id": "ChIJx", "displayName": {"text": "X"}, "location": {"latitude": 1, "longitude": 2}}]}


def test_grid_geometry():
    pts = grid_points(25.0, 55.0, 3, 1.0)
    assert len(pts) == 9 and pts[4] == (1, 1, 25.0, 55.0)
    north, west = pts[1], pts[3]
    assert north[2] > 25.0 and round((north[2] - 25.0) * 110.574, 3) == 1.0
    assert west[3] < 55.0


def test_rank_scan_uses_only_free_searches_and_reads_grid():
    w, apis, bp = env()
    rt = RankTracker(w.settings, bp)
    fake = FakePlaces(w.settings)
    rt.places = lambda: fake
    out = rt.scan("grt", {"keywords": ["plumber"], "location": "22", "grid_size": 5, "spacing_km": 1.0, "depth": 40,
                          "competitors": 3, "competitor_names": True})
    kw = out["keywords"][0]
    assert out["place_id"] == "ChIJtarget" and out["centre"] == {"lat": 25.08, "lng": 55.14}
    rows = [r.split() for r in kw["grid"]]
    assert rows[2][2] == "1" and rows[2][0] == "1"  # centre and west: top
    assert int(rows[2][4]) > int(rows[2][3]) > 1  # drops going east
    assert kw["top_competitors"][0]["place_id"] == "comp0" and kw["top_competitors"][0]["name"] == "Name comp0"
    searches = [m for p, m in fake.masks if p == "places:searchText"]
    assert searches and all(m == FREE_MASK for m in searches)
    assert out["api_calls"]["pro_lookups"] == 3  # only the competitor names
    assert out["api_calls"]["free_id_searches"] == len(searches)


def test_rank_scan_input_checks():
    w, _, bp = env()
    rt = RankTracker(w.settings, bp)
    rt.places = lambda: FakePlaces(w.settings)
    base = {"keywords": ["plumber"], "grid_size": 5, "spacing_km": 1.0, "depth": 20, "competitors": 0}
    with pytest.raises(AdsError, match="odd"):
        rt.scan("grt", {**base, "grid_size": 4, "place_id": "x"})
    with pytest.raises(AdsError, match="Say which business"):
        rt.scan("grt", base)
    with pytest.raises(AdsError, match="At most 5"):
        rt.scan("grt", {**base, "keywords": list("abcdef"), "place_id": "x"})
    out = rt.scan("grt", {**base, "place_id": "places/ChIJtarget", "center": "25.08,55.14"})
    assert out["keywords"][0]["centre_rank"] == "1"


def test_places_without_key_explains_setup():
    w, _, _ = env()
    with pytest.raises(AdsError, match="PLACES_API_KEY"):
        Places(w.settings).ranked_ids("plumber", 1, 2, 500, 20, None, None)
