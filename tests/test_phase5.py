"""Phase 5: Google Analytics, Search Console, Merchant Center and website tag checks. Google is faked."""

import copy

import pytest

from app import research
from app.ads import AdsError
from app.analytics import Analytics, report_rows
from app.auth import GOOGLE_SCOPES
from app.google_apis import GoogleApis, explain, raw_change
from app.merchant import OVERRIDE_SOURCE, Merchant
from app.search_console import SearchConsole, opportunities
from app.sitetags import SiteTags, install_code, scan
from tests.test_phase4 import FakeGtm
from tests.test_writes import make

U = "w@z.test"


class FakeApis(GoogleApis):
    """Routes (api, method, path) to canned answers; records every call; POSTs create items with names."""

    def __init__(self, ads, routes=None):
        super().__init__(ads)
        self.routes = dict(routes or {})
        self.calls = []
        self.n = 0

    def call(self, grt, api, method, path, params=None, body=None):
        self.calls.append((api, method, path, copy.deepcopy(params), copy.deepcopy(body)))
        key = (api, method, path)
        if key in self.routes:
            r = self.routes[key]
            if isinstance(r, Exception):
                raise r
            return copy.deepcopy(r(params, body) if callable(r) else r)
        if method == "POST":
            self.n += 1
            if path.endswith("/properties"):
                return {"name": f"properties/90{self.n}"}
            if path.endswith("/dataStreams"):
                return {"name": f"{path.removeprefix('v1beta/')}/{self.n}", "webStreamData": {"measurementId": "G-NEW123"}}
            if path.endswith("/dataSources"):
                return {"name": f"accounts/55/dataSources/{700 + self.n}"}
            return {"name": f"{path.split('/', 1)[1]}/{self.n}"}
        return {}


def env(routes=None):
    w, ads = make()
    apis = FakeApis(ads, routes)
    w.apis = apis
    return w, ads, apis


def test_new_scopes_are_requested():
    for s in ("analytics.edit", "analytics.readonly", "webmasters.readonly", "auth/content"):
        assert any(s in x for x in GOOGLE_SCOPES)


def test_explain_gives_plain_fixes():
    assert "Disconnect" in explain("ga_admin", 403, "Request had insufficient authentication scopes.")
    msg = explain("merchant", 403, "Merchant API has not been used in project 1 before or it is disabled.")
    assert "merchantapi.googleapis.com" in msg and "update script" in msg
    assert "merchant_register_project" in explain("merchant", 401, "GCP project is not registered with the account")


# ------------------------------------------------------------------ Google Analytics

SUMMARIES = {"accountSummaries": [{"account": "accounts/11", "displayName": "Zyppia clients", "propertySummaries": [
    {"property": "properties/501", "displayName": "DXB Travel", "propertyType": "PROPERTY_TYPE_ORDINARY"}]}]}


def test_ga4_create_property_with_stream_and_undo():
    w, _, apis = env({("ga_admin", "GET", "v1beta/accountSummaries"): SUMMARIES})
    ga = Analytics(w, apis)
    with pytest.raises(AdsError, match="already has a property"):
        ga.create_property("grt", {"account_id": "11", "name": "dxb travel"})
    ch = ga.create_property("grt", {"account_id": "11", "name": "Perth Plumbing", "website_url": "perthplumbing.com.au",
                                    "time_zone": "Australia/Perth", "currency": "aud"})
    p = w.preview(U, "grt", ch)
    assert "no test mode" in p["status"] and not p["needs_extra_confirmation"]
    assert any("web data stream for https://perthplumbing.com.au" in line for line in p["details"])
    done = w.apply(U, "grt", p["confirm_token"], False)
    assert done["status"] == "done"
    create, stream = [c for c in apis.calls if c[1] == "POST"]
    assert create[4]["parent"] == "accounts/11" and create[4]["currencyCode"] == "AUD"
    assert stream[2] == "v1beta/properties/901/dataStreams"  # ${property} filled from the first step
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    assert apis.calls[-1][:3] == ("ga_admin", "DELETE", "v1beta/properties/901")
    with pytest.raises(AdsError, match="already applied"):
        w.apply(U, "grt", p["confirm_token"], False)


def test_ga4_key_event_create_and_delete():
    keys = {"keyEvents": [{"name": "properties/501/keyEvents/7", "eventName": "purchase", "countingMethod": "ONCE_PER_EVENT",
                           "custom": False, "deletable": True}]}
    w, _, apis = env({("ga_admin", "GET", "v1beta/properties/501/keyEvents"): keys,
                      ("ga_admin", "GET", "v1beta/accountSummaries"): SUMMARIES})
    ga = Analytics(w, apis)
    with pytest.raises(AdsError, match="already a key event"):
        ga.key_event("grt", "501", "create", {"event_name": "purchase"})
    with pytest.raises(AdsError, match="exactly as the site sends it"):
        ga.key_event("grt", "501", "create", {"event_name": "bad name!"})
    ch = ga.key_event("grt", "DXB Travel", "create", {"event_name": "generate_lead", "default_value": 50, "currency": "aed"})
    p = w.preview(U, "grt", ch)
    done = w.apply(U, "grt", p["confirm_token"], False)
    post = apis.calls[-1]
    assert post[2] == "v1beta/properties/501/keyEvents" and post[4]["defaultValue"]["currencyCode"] == "AED"
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    assert apis.calls[-1][1] == "DELETE" and apis.calls[-1][2].startswith("v1beta/properties/501/keyEvents/")
    # deleting needs extra confirmation; its undo re-creates it
    ch = ga.key_event("grt", "501", "delete", {"event_name": "purchase"})
    p = w.preview(U, "grt", ch)
    assert p["needs_extra_confirmation"]
    done = w.apply(U, "grt", p["confirm_token"], True)
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    assert apis.calls[-1][1] == "POST" and apis.calls[-1][4]["eventName"] == "purchase"


def test_ga4_link_google_ads():
    w, _, apis = env({("ga_admin", "GET", "v1beta/properties/501/googleAdsLinks"): {"googleAdsLinks": []}})
    ga = Analytics(w, apis)
    p = w.preview(U, "grt", ga.link_ads("grt", U, "501", "399-355-6480", "link", True))
    assert "DXB - Travel And Tourism" in p["change"]
    w.apply(U, "grt", p["confirm_token"], False)
    assert apis.calls[-1][4] == {"customerId": "3993556480", "adsPersonalizationEnabled": True}
    apis.routes[("ga_admin", "GET", "v1beta/properties/501/googleAdsLinks")] = {
        "googleAdsLinks": [{"name": "properties/501/googleAdsLinks/3", "customerId": "3993556480"}]}
    with pytest.raises(AdsError, match="already linked"):
        ga.link_ads("grt", U, "501", "399-355-6480", "link", True)


def test_ga4_report_and_live_check():
    resp = {"dimensionHeaders": [{"name": "eventName"}], "metricHeaders": [{"name": "eventCount", "type": "TYPE_INTEGER"}],
            "rows": [{"dimensionValues": [{"value": "page_view"}], "metricValues": [{"value": "12"}]},
                     {"dimensionValues": [{"value": "generate_lead"}], "metricValues": [{"value": "1"}]}]}
    assert report_rows(resp)[0] == {"eventName": "page_view", "eventCount": 12}
    w, _, apis = env({("ga_data", "POST", "v1beta/properties/501:runRealtimeReport"): resp,
                      ("ga_data", "POST", "v1beta/properties/501:runReport"): resp})
    ga = Analytics(w, apis)
    assert ga.check_event("grt", "501", "generate_lead")["arrived"] is True
    miss = ga.check_event("grt", "501", "purchase")
    assert miss["arrived"] is False and "Not yet" in miss["result"]
    out = ga.report("grt", "501", ["eventCount"], ["eventName"], "7daysAgo", "yesterday",
                    [{"field": "eventName", "value": "lead", "match": "contains"}], None, 10)
    body = apis.calls[-1][4]
    assert body["dimensionFilter"]["filter"]["stringFilter"]["matchType"] == "CONTAINS"
    assert body["orderBys"][0]["metric"]["metricName"] == "eventCount" and out["row_count"] == 2
    with pytest.raises(AdsError, match="Dates"):
        ga.report("grt", "501", ["sessions"], [], "last week", "today", None, None, 10)


# ------------------------------------------------------------------ Search Console

SITES = {"siteEntry": [{"siteUrl": "https://www.client.com/", "permissionLevel": "siteOwner"},
                       {"siteUrl": "sc-domain:client.com", "permissionLevel": "siteFullUser"},
                       {"siteUrl": "https://other.com/", "permissionLevel": "siteUnverifiedUser"}]}


def test_gsc_site_matching_prefers_domain_property():
    w, _, apis = env({("gsc", "GET", "webmasters/v3/sites"): SITES})
    sc = SearchConsole(apis)
    assert sc.site("grt", "client.com") == "sc-domain:client.com"
    assert sc.site("grt", "https://www.client.com/") == "https://www.client.com/"
    with pytest.raises(AdsError, match="can't see"):
        sc.site("grt", "other.com")


def test_gsc_queries_compare_and_opportunities():
    path = "webmasters/v3/sites/sc-domain%3Aclient.com/searchAnalytics/query"

    def answer(params, body):
        if body["startDate"] > "2026-01-01" and body["rowLimit"] == 200:
            return {"rows": [{"keys": ["emergency plumber perth"], "clicks": 3, "impressions": 900, "ctr": 0.0033,
                              "position": 7.2},
                             {"keys": ["client plumbing"], "clicks": 50, "impressions": 80, "ctr": 0.625, "position": 1.0}]}
        return {"rows": [{"keys": ["client plumbing"], "clicks": 40, "impressions": 70, "ctr": 0.57, "position": 1.1}]}

    w, _, apis = env({("gsc", "GET", "webmasters/v3/sites"): SITES, ("gsc", "POST", path): answer})
    out = SearchConsole(apis).search_queries("grt", "client.com", {"days": 28, "compare_previous": True,
                                                                   "query_not_contains": "client"})
    assert out["rows"][1]["clicks_change"] == 10 and out["rows"][0]["clicks_before"] == 0
    assert [o["query"] for o in out["opportunities"]] == ["emergency plumber perth"]
    body = [c for c in apis.calls if c[2] == path][0][4]
    assert body["dimensionFilterGroups"][0]["filters"][0] == {"dimension": "query", "operator": "notContains",
                                                              "expression": "client"}
    assert opportunities([{"impressions": 10, "position": 8, "ctr": 0}]) == []


# ------------------------------------------------------------------ Merchant Center

PRODUCT = {"name": "accounts/55/products/en~AU~SKU1", "offerId": "SKU1", "contentLanguage": "en", "feedLabel": "AU",
           "dataSource": "accounts/55/dataSources/1",
           "productAttributes": {"title": "Red Kettle", "price": {"amountMicros": "49950000", "currencyCode": "AUD"},
                                 "availability": "IN_STOCK"},
           "productStatus": {"destinationStatuses": [{"reportingContext": "SHOPPING_ADS", "disapprovedCountries": ["AU"]}],
                             "itemLevelIssues": [{"description": "Missing GTIN", "severity": "DISAPPROVED",
                                                  "resolution": "merchant_action"}]}}
FEED = {"name": "accounts/55/dataSources/1", "displayName": "Shopify feed",
        "primaryProductDataSource": {"defaultRule": {"takeFromDataSources": [{"self": True}]}}}


def merchant_env(sources):
    return env({("merchant", "GET", "products/v1/accounts/55/products"): {"products": [PRODUCT]},
                ("merchant", "GET", "products/v1/accounts/55/products/en~AU~SKU1"): PRODUCT,
                ("merchant", "GET", "datasources/v1/accounts/55/dataSources"): {"dataSources": sources}})


def test_merchant_products_summary():
    w, _, apis = merchant_env([FEED])
    out = Merchant(w, apis).products("grt", "55", None, True, 10)
    assert out["top_problems"][0] == {"problem": "Missing GTIN (DISAPPROVED)", "products": 1, "examples": ["SKU1"]}
    assert out["products"][0]["price"] == "49.95 AUD"
    assert out["status_counts"] == {"SHOPPING_ADS: disapproved": 1}


def test_merchant_override_first_time_then_undo():
    w, _, apis = merchant_env([FEED])
    mc = Merchant(w, apis)
    ch = mc.override("grt", "55", "SKU1", {"title": "Red Electric Kettle 1.7L", "price": "44.95", "availability": "out of stock"},
                     False)
    p = w.preview(U, "grt", ch)
    assert p["needs_extra_confirmation"]  # price change
    assert "title: 'Red Kettle' -> 'Red Electric Kettle 1.7L'" in p["details"]
    done = w.apply(U, "grt", p["confirm_token"], True)
    assert done["status"] == "done"
    src, rule, ins = [c for c in apis.calls if c[1] in ("POST", "PATCH")]
    assert src[4]["displayName"] == OVERRIDE_SOURCE
    new_src = "accounts/55/dataSources/701"
    assert rule[4]["primaryProductDataSource"]["defaultRule"]["takeFromDataSources"][0] == {"supplementalDataSourceName": new_src}
    assert ins[3] == {"dataSource": new_src}
    attrs = ins[4]["productAttributes"]
    assert attrs["price"] == {"amountMicros": "44950000", "currencyCode": "AUD"} and attrs["availability"] == "OUT_OF_STOCK"
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    undo = [c for c in apis.calls[-3:]]
    assert [c[1] for c in undo] == ["DELETE", "PATCH", "DELETE"]  # reversed order
    assert undo[0][2] == "products/v1/accounts/55/productInputs/en~AU~SKU1" and undo[0][3] == {"dataSource": new_src}
    assert undo[1][4]["primaryProductDataSource"]["defaultRule"]["takeFromDataSources"] == [{"self": True}]
    assert undo[2][2] == f"datasources/v1/{new_src}"


def test_merchant_override_when_source_exists_and_bad_input():
    ours = {"name": "accounts/55/dataSources/9", "displayName": OVERRIDE_SOURCE, "supplementalProductDataSource": {}}
    feed = copy.deepcopy(FEED)
    feed["primaryProductDataSource"]["defaultRule"]["takeFromDataSources"].insert(0, {"supplementalDataSourceName": ours["name"]})
    w, _, apis = merchant_env([feed, ours])
    mc = Merchant(w, apis)
    ch = mc.override("grt", "55", "SKU1", {"pause_ads": True}, False)
    assert len(ch.gtm) == 1 and ch.gtm[0]["b"]["productAttributes"] == {"pause": "ads"} and not ch.large
    with pytest.raises(AdsError, match="availability must be"):
        mc.override("grt", "55", "SKU1", {"availability": "soon"}, False)
    with pytest.raises(AdsError, match="No product"):
        mc.override("grt", "55", "NOPE", {"title": "x"}, False)
    clear = mc.override("grt", "55", "SKU1", {}, True)
    assert clear.gtm[0]["m"] == "DELETE"


def test_raw_change_is_large_and_checked():
    ch = raw_change("merchant", "Merchant Center", [{"method": "POST", "path": "accounts/v1/accounts/55/x", "body": {}}], "x")
    assert ch.large and ch.gtm[0]["a"] == "merchant"
    with pytest.raises(AdsError):
        raw_change("merchant", "Merchant Center", [{"method": "GET", "path": "a"}], "x")
    with pytest.raises(AdsError):
        raw_change("merchant", "Merchant Center", [{"method": "POST", "path": "../../evil"}], "x")


def test_partial_failure_reports_and_undoes_what_was_made():
    w, _, apis = env({("ga_admin", "GET", "v1beta/accountSummaries"): SUMMARIES})
    p = w.preview(U, "grt", Analytics(w, apis).create_property(
        "grt", {"account_id": "11", "name": "New", "website_url": "https://new.com"}))
    apis.routes[("ga_admin", "POST", "v1beta/properties/901/dataStreams")] = AdsError("boom")
    done = w.apply(U, "grt", p["confirm_token"], False)
    assert done["status"] == "partly done" and "boom" in done["error"] and done["undo_token"]


# ------------------------------------------------------------------ website tags

PAGE = """<html><head><meta name="generator" content="WordPress 6.6">
<script>(function(w,d,s,l,i){w[l]=w[l]||[];})(window,document,'script','dataLayer','GTM-TEST');</script>
<script async src="https://www.googletagmanager.com/gtag/js?id=AW-987654321"></script>
<script>gtag('config', 'AW-987654321'); gtag('config', 'G-ABC1234'); ga('create', 'UA-1234567-1');</script>
</head><body><noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-TEST"></iframe></noscript>
""" + ("<p>text</p>" * 2000) + "</body></html>"


def test_scan_finds_tags():
    s = scan(PAGE)
    assert s["tag_manager"] == {"GTM-TEST": 1} and s["google_tag_config"] == {"AW-987654321": 1, "G-ABC1234": 1}
    assert s["old_universal_analytics"] == ["UA-1234567-1"] and s["platform"].startswith("WordPress")
    double = scan(PAGE + '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-TEST"></script>')
    assert double["tag_manager"] == {"GTM-TEST": 2}


def test_check_site_tags_with_tag_manager_cross_check(monkeypatch):
    w, ads = make()
    gtm = FakeGtm(ads)
    w.gtm = gtm
    gtm.items["accounts/1/containers/2/versions/7"] = {
        "containerVersionId": "7", "path": "accounts/1/containers/2/versions/7",
        "tag": [{"type": "googtag", "parameter": [{"key": "tagId", "value": "AW-987654321"}]}]}
    orig = gtm.call
    gtm.call = lambda grt, m, p, params=None, body=None: (copy.deepcopy(gtm.items["accounts/1/containers/2/versions/7"])
                                                          if p.endswith("versions:live") else orig(grt, m, p, params, body))
    monkeypatch.setattr(research, "fetch_html", lambda url, timeout=10.0: (url, PAGE))
    out = SiteTags(w).check("grt", "https://client.com", None, ["GTM-TEST", "G-ABC1234", "AW-111111111"])
    text = " ".join(out["problems"])
    assert "AW-987654321 is in the page code AND in Tag Manager GTM-TEST" in text
    assert "Universal Analytics" in text and "AW-111111111 isn't on the site" in text
    assert out["expected_found"] == ["G-ABC1234", "GTM-TEST"] and out["verdict"] == "Problems found"


def test_install_code():
    out = install_code(["GTM-ABC123", "g-xyz12345"], "shopify")
    assert "GTM-ABC123" in out["part_1_head"] and "ns.html?id=GTM-ABC123" in out["part_2_body"]
    assert "G-XYZ12345" in out["advice"] and "theme.liquid" in out["where_to_paste"]
    direct = install_code(["987654321", "G-XYZ12345"], "other")
    assert "gtag/js?id=AW-987654321" in direct["part_1_head"] and "gtag('config', 'G-XYZ12345')" in direct["part_1_head"]
    with pytest.raises(AdsError):
        install_code(["hello"], "other")


def test_server_registers_phase5_tools():
    import asyncio

    from app.server import build_server
    from tests.test_writes import make as _make

    w, _ = _make()
    names = {t.name for t in asyncio.run(build_server(w.settings).list_tools())}
    for n in ("ga4_list_properties", "ga4_check_event", "ga4_link_google_ads", "gsc_search_queries", "merchant_products",
              "merchant_product_override", "check_site_tags", "tag_install_code"):
        assert n in names
