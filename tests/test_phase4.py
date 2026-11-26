"""Phase 4: conversion actions, Google Tag Manager, and one-step conversion tracking. Google is faked."""

import copy

import pytest

from app.ads import AdsError
from app.conversions import Conversions
from app.gtm import ALL_PAGES, GtmGateway, GtmTools, condition, fill
from app.writes import send_to_ids
from tests.test_writes import ACCT, make

U = "w@z.test"
WS = "accounts/1/containers/2/workspaces/3"
SNIPPET = [{"type_": "WEBPAGE", "event_snippet": "gtag('event', 'conversion', {'send_to': 'AW-987654321/AbC-12_x'});"}]


class FakeGtm(GtmGateway):
    """In-memory Tag Manager: one account, one container (GTM-TEST), Default Workspace."""

    def __init__(self, ads):
        super().__init__(ads)
        self.items = {}  # path -> object
        self.built_ins = {"pageUrl", "pagePath", "pageHostname", "event", "referrer"}
        self.calls = []
        self.next_id = 10
        self.live = "accounts/1/containers/2/versions/7"
        self.fail_on = None

    def add(self, coll, obj):
        self.next_id += 1
        ident = str(self.next_id)
        key = {"tags": "tagId", "triggers": "triggerId", "variables": "variableId"}[coll]
        path = f"{WS}/{coll}/{ident}"
        self.items[path] = {**copy.deepcopy(obj), key: ident, "path": path, "fingerprint": "f" + ident}
        return self.items[path]

    def call(self, grt, method, path, params=None, body=None):
        self.calls.append((method, path, params, copy.deepcopy(body)))
        if self.fail_on and self.fail_on in f"{method} {path}":
            raise AdsError("boom")
        if method == "GET":
            if path == "accounts":
                return {"account": [{"path": "accounts/1", "accountId": "1", "name": "Zyppia"}]}
            if path == "accounts/1/containers":
                return {"container": [{"path": "accounts/1/containers/2", "containerId": "2", "name": "client.com",
                                       "publicId": "GTM-TEST", "usageContext": ["web"], "domainName": ["client.com"]}]}
            if path.endswith("/workspaces"):
                return {"workspace": [{"path": WS, "workspaceId": "3", "name": "Default Workspace"}]}
            for coll, key in (("tags", "tag"), ("triggers", "trigger"), ("variables", "variable")):
                if path == f"{WS}/{coll}":
                    return {key: [v for p, v in self.items.items() if p.startswith(f"{WS}/{coll}/")]}
            if path == f"{WS}/built_in_variables":
                return {"builtInVariable": [{"type": t, "name": t} for t in sorted(self.built_ins)]}
            if path == f"{WS}/status":
                return {"workspaceChange": [{"changeStatus": "added", "tag": {"name": v["name"]}}
                                            for p, v in self.items.items() if "/tags/" in p]}
            if path.endswith("versions:live"):
                return {"path": self.live, "containerVersionId": self.live.rsplit("/", 1)[1], "name": "v" + self.live[-1]}
            if path in self.items:
                return copy.deepcopy(self.items[path])
            raise AdsError("Tag Manager couldn't find that")
        if method == "POST":
            if path.endswith("/built_in_variables"):
                self.built_ins |= set(params["type"])
                return {}
            if path.endswith(":create_version"):
                return {"containerVersion": {"path": "accounts/1/containers/2/versions/8"}, "compilerError": False}
            if path.endswith(":publish"):
                self.live = path.removesuffix(":publish")
                return {"containerVersion": {"path": self.live}}
            coll = path.rsplit("/", 1)[1]
            return self.add(coll, body)
        if method == "PUT":
            self.items[path] = {**body, "path": path}
            return self.items[path]
        if method == "DELETE":
            if path.endswith("/built_in_variables"):
                self.built_ins -= set(params["type"])
            else:
                self.items.pop(path)
            return {}
        raise AssertionError(method)


def setup_env(rows=()):
    w, ads = make(rows)
    gtm = FakeGtm(ads)
    w.gtm = gtm
    conv = Conversions(w)
    return w, ads, gtm, conv, GtmTools(w, gtm, conv)


CONV_ROW = {
    "conversion_action.id": 44, "conversion_action.name": "Contact form", "conversion_action.status": "ENABLED",
    "conversion_action.type": "WEBPAGE", "conversion_action.category": "SUBMIT_LEAD_FORM",
    "conversion_action.origin": "WEBSITE", "conversion_action.primary_for_goal": True,
    "conversion_action.counting_type": "MANY_PER_CLICK", "conversion_action.value_settings.default_value": 1.0,
    "conversion_action.click_through_lookback_window_days": 30, "conversion_action.tag_snippets": SNIPPET,
}
TRACKING = ("FROM customer", [{"customer.conversion_tracking_setting.conversion_tracking_id": 987654321,
                               "customer.conversion_tracking_setting.google_ads_conversion_customer":
                                   "customers/3993556480"}])


# ------------------------------------------------------------------ conversion actions


def test_send_to_parsing():
    assert send_to_ids(SNIPPET) == {"conversion_id": "987654321", "conversion_label": "AbC-12_x"}
    assert send_to_ids([]) is None


def test_list_flags_problems_and_gives_gtm_ids():
    w, _, _, conv, _ = setup_env([("segments.date", []), ("FROM conversion_action", [CONV_ROW]), TRACKING])
    out = conv.list("grt", ACCT, False)
    item = out["conversion_actions"][0]
    assert item["gtm"]["send_to"] == "AW-987654321/AbC-12_x"
    assert any("no conversions" in p for p in item["problems"])
    assert any("one per click" in p for p in item["problems"])
    assert out["conversion_tracking"]["conversion_id"] == "987654321"


def test_create_website_conversion():
    w, ads, _, conv, _ = setup_env()
    ch = conv.create("grt", ACCT, {"name": "Booking", "type": "website", "value": 50, "use_value_from_tag": True})
    c = ch.ops[0].conversion_action_operation.create
    assert c.type_.name == "WEBPAGE" and c.category.name == "SUBMIT_LEAD_FORM"
    assert c.counting_type.name == "ONE_PER_CLICK" and c.primary_for_goal
    assert c.value_settings.default_value == 50 and c.value_settings.default_currency_code == "AED"
    assert c.value_settings.always_use_default_value is False
    p = w.preview(U, "grt", ch)
    assert ads.calls[-1][0] is True and not p["needs_extra_confirmation"]
    done = w.apply(U, "grt", p["confirm_token"], False)
    assert done["changed"] == ["customers/3993556480/conversionActions/105"]
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    assert ads.calls[-1][1][0].conversion_action_operation.remove == "customers/3993556480/conversionActions/105"


def test_create_call_and_import_types():
    w, _, _, conv, _ = setup_env()
    c = conv.create("grt", ACCT, {"name": "Calls", "type": "phone_call_from_ads", "call_length_seconds": 30}) \
        .ops[0].conversion_action_operation.create
    assert c.type_.name == "AD_CALL" and c.phone_call_duration_seconds == 30 and c.category.name == "PHONE_CALL_LEAD"
    ch = conv.create("grt", ACCT, {"name": "CRM sales", "type": "import_offline_clicks", "category": "purchase"})
    c = ch.ops[0].conversion_action_operation.create
    assert c.type_.name == "UPLOAD_CLICKS" and c.counting_type.name == "MANY_PER_CLICK"
    assert any("upload" in x for x in ch.warnings)
    with pytest.raises(AdsError, match="only applies"):
        conv.create("grt", ACCT, {"name": "X", "type": "website", "call_length_seconds": 30})


def test_create_refuses_duplicate_name():
    w, _, _, conv, _ = setup_env([("conversion_action.name = 'Contact form'", [{"conversion_action.id": 44}])])
    with pytest.raises(AdsError, match="already a conversion action"):
        conv.create("grt", ACCT, {"name": "Contact form"})


def test_update_fixes_counting_and_undo_restores():
    w, ads, _, conv, _ = setup_env([("FROM conversion_action", [CONV_ROW])])
    ch = conv.update("grt", ACCT, "44", {"counting_type": "ONE_PER_CLICK", "primary": False, "value": 10})
    op = ch.ops[0].conversion_action_operation
    assert list(op.update_mask.paths) == ["counting_type", "primary_for_goal", "value_settings.default_value"]
    assert op.update.resource_name == "customers/3993556480/conversionActions/44"
    row = ads.types().get_type("GoogleAdsRow")
    row.conversion_action.counting_type = ads.types().enums.ConversionActionCountingTypeEnum.MANY_PER_CLICK
    row.conversion_action.primary_for_goal = True
    row.conversion_action.value_settings.default_value = 1.0
    ads.raw_rows = [row]
    done = w.apply(U, "grt", w.preview(U, "grt", ch)["confirm_token"], False)
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], bool(up["needs_extra_confirmation"]))
    back = ads.calls[-1][1][0].conversion_action_operation.update
    assert back.counting_type.name == "MANY_PER_CLICK" and back.primary_for_goal and back.value_settings.default_value == 1.0
    with pytest.raises(AdsError, match="Say what to change"):
        conv.update("grt", ACCT, "44", {})


def test_remove_needs_exact_name_and_extra_confirmation():
    w, _, _, conv, _ = setup_env([("FROM conversion_action", [CONV_ROW])])
    with pytest.raises(AdsError, match="exact name"):
        conv.remove("grt", ACCT, ["44"], None)
    ch = conv.remove("grt", ACCT, ["44"], ["contact form"])
    assert ch.ops[0].conversion_action_operation.remove.endswith("/conversionActions/44") and ch.large


# ------------------------------------------------------------------ Tag Manager


def test_condition_builder():
    c = condition({"variable": "Click URL", "operator": "not_starts_with", "value": "tel:"})
    assert c["type"] == "startsWith"
    assert {"type": "boolean", "key": "negate", "value": "true"} in c["parameter"]
    assert c["parameter"][0]["value"] == "{{Click URL}}"
    with pytest.raises(AdsError, match="Unknown operator"):
        condition({"variable": "Page URL", "operator": "near", "value": "x"})


def test_fill_leaves_code_alone():
    assert fill({"p": "${version}:publish", "html": "`${x}`"}, {"version": "v/8"}) == {"p": "v/8:publish", "html": "`${x}`"}


def test_container_lookup():
    _, _, gtm, _, _ = setup_env()
    assert gtm.container("grt", "gtm-test")["path"] == "accounts/1/containers/2"
    with pytest.raises(AdsError, match="can't see"):
        gtm.container("grt", "GTM-NOPE")


def test_tag_create_preview_apply_undo():
    w, _, gtm, _, g = setup_env()
    trig = gtm.add("triggers", {"name": "Thank you", "type": "pageview"})
    ch = g.tag("grt", "GTM-TEST", None, "create", {"kind": "google_ads_conversion", "name": "Ads - Lead",
                                                   "conversion_id": "AW-111", "conversion_label": "lab",
                                                   "firing_trigger_ids": [trig["triggerId"]]})
    p = w.preview(U, "grt", ch)
    assert "Tag Manager" in p["account"] and not p["needs_extra_confirmation"]
    assert not any(c[0] == "POST" for c in gtm.calls)  # preview changes nothing
    done = w.apply(U, "grt", p["confirm_token"], False)
    tag = next(v for v in gtm.items.values() if v.get("name") == "Ads - Lead")
    assert tag["type"] == "awct" and {"type": "template", "key": "conversionId", "value": "111"} in tag["parameter"]
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    assert tag["path"] not in gtm.items


def test_tag_needs_trigger_and_ids():
    _, _, _, _, g = setup_env()
    with pytest.raises(AdsError, match="conversion_id and conversion_label"):
        g.tag("grt", "GTM-TEST", None, "create", {"kind": "google_ads_conversion", "name": "x", "firing_trigger_ids": [ALL_PAGES]})
    with pytest.raises(AdsError, match="No trigger with ID"):
        g.tag("grt", "GTM-TEST", None, "create", {"kind": "conversion_linker", "name": "CL", "firing_trigger_ids": ["999"]})
    ch = g.tag("grt", "GTM-TEST", None, "create", {"kind": "conversion_linker", "name": "CL"})
    assert ch.gtm[-1]["b"]["firingTriggerId"] == [ALL_PAGES]


def test_tag_update_and_delete_undo():
    w, _, gtm, _, g = setup_env()
    tag = gtm.add("tags", {"name": "Old", "type": "awct", "firingTriggerId": [ALL_PAGES], "parameter": [
        {"type": "template", "key": "conversionId", "value": "111"}, {"type": "template", "key": "conversionLabel", "value": "a"}]})
    ch = g.tag("grt", "GTM-TEST", None, "update", {"id": tag["tagId"], "conversion_label": "b", "paused": True})
    assert any("conversionLabel" in line for line in ch.lines)
    done = w.apply(U, "grt", w.preview(U, "grt", ch)["confirm_token"], False)
    assert gtm.items[tag["path"]]["paused"] is True
    w.apply(U, "grt", w.undo_preview(U, "grt", done["undo_token"])["confirm_token"], False)
    assert "paused" not in gtm.items[tag["path"]]
    assert {"type": "template", "key": "conversionLabel", "value": "a"} in gtm.items[tag["path"]]["parameter"]

    ch = g.tag("grt", "GTM-TEST", None, "delete", {"id": tag["tagId"]})
    p = w.preview(U, "grt", ch)
    assert p["needs_extra_confirmation"]
    done = w.apply(U, "grt", p["confirm_token"], True)
    assert tag["path"] not in gtm.items
    w.apply(U, "grt", w.undo_preview(U, "grt", done["undo_token"])["confirm_token"], False)
    assert any(v.get("name") == "Old" for v in gtm.items.values())


def test_trigger_turns_on_built_in_click_variables_and_blocks_delete_in_use():
    w, _, gtm, _, g = setup_env()
    ch = g.trigger("grt", "GTM-TEST", None, "create", {"kind": "link_click", "name": "Phone taps", "conditions": [
        {"variable": "Click URL", "operator": "starts_with", "value": "tel:"}]})
    assert ch.gtm[0]["k"] == "built_in" and ch.gtm[0]["q"] == {"type": ["clickUrl"]}
    w.apply(U, "grt", w.preview(U, "grt", ch)["confirm_token"], False)
    assert "clickUrl" in gtm.built_ins
    trig = next(v for v in gtm.items.values() if v.get("name") == "Phone taps")
    assert trig["type"] == "linkClick" and trig["filter"][0]["type"] == "startsWith"
    gtm.add("tags", {"name": "uses it", "type": "gclidw", "firingTriggerId": [trig["triggerId"]]})
    with pytest.raises(AdsError, match="used by tag"):
        g.trigger("grt", "GTM-TEST", None, "delete", {"id": trig["triggerId"]})


def test_custom_event_trigger_and_variable():
    _, _, _, _, g = setup_env()
    ch = g.trigger("grt", "GTM-TEST", None, "create", {"kind": "custom_event", "name": "Lead", "event_name": "generate_lead"})
    f = ch.gtm[-1]["b"]["customEventFilter"][0]
    assert f["parameter"][0]["value"] == "{{_event}}" and f["parameter"][1]["value"] == "generate_lead"
    ch = g.variable("grt", "GTM-TEST", None, "create", {"kind": "data_layer", "name": "DLV - value", "value": "ecommerce.value"})
    params = {p["key"]: p["value"] for p in ch.gtm[-1]["b"]["parameter"]}
    assert ch.gtm[-1]["b"]["type"] == "v" and params["name"] == "ecommerce.value" and params["dataLayerVersion"] == "2"
    ch = g.variable("grt", "GTM-TEST", None, "create", {"kind": "custom_javascript", "name": "JS", "value": "function(){return 1}"})
    assert ch.large


def test_publish_needs_extra_confirmation_and_undo_republishes_old_version():
    w, _, gtm, _, g = setup_env()
    gtm.add("tags", {"name": "New tag", "type": "gclidw", "firingTriggerId": [ALL_PAGES]})
    ch = g.publish("grt", "GTM-TEST", None, "v8", None, True)
    p = w.preview(U, "grt", ch)
    assert p["needs_extra_confirmation"] and any("New tag" in d for d in p["details"])
    with pytest.raises(AdsError, match="extra confirmation"):
        w.apply(U, "grt", p["confirm_token"], False)
    done = w.apply(U, "grt", p["confirm_token"], True)
    assert gtm.live == "accounts/1/containers/2/versions/8"
    up = w.undo_preview(U, "grt", done["undo_token"])
    assert up["needs_extra_confirmation"]
    w.apply(U, "grt", up["confirm_token"], True)
    assert gtm.live == "accounts/1/containers/2/versions/7"


def test_raw_gtm_calls_are_checked_and_flagged():
    _, _, _, _, g = setup_env()
    ch = g.raw("grt", [{"method": "POST", "path": "accounts/1/containers/2/workspaces", "body": {"name": "Test"}}], "New ws")
    assert ch.large and ch.gtm[0]["k"] == "raw"
    with pytest.raises(AdsError, match="path must"):
        g.raw("grt", [{"method": "POST", "path": "../../evil"}], "x")
    with pytest.raises(AdsError, match="method"):
        g.raw("grt", [{"method": "GET", "path": "accounts/1"}], "x")


def test_workspace_contents_reports_problems():
    _, _, gtm, _, g = setup_env()
    gtm.add("tags", {"name": "Ads", "type": "awct", "firingTriggerId": [], "parameter": []})
    out = g.contents("grt", "GTM-TEST", None)
    assert out["container"]["public_id"] == "GTM-TEST" and out["tags"][0]["name"] == "Ads"
    text = " ".join(out["problems"])
    assert "Conversion Linker" in text and "never runs" in text and "missing its conversion ID" in text


# ------------------------------------------------------------------ end to end


def test_setup_new_conversion_end_to_end_and_undo():
    w, ads, gtm, conv, g = setup_env([TRACKING])
    ch = g.setup("grt", U, "3993556480", {
        "container": "GTM-TEST", "conversion_name": "Contact form",
        "trigger": {"kind": "page_view", "conditions": [{"variable": "Page URL", "operator": "contains", "value": "/thank-you"}]},
        "value_variable": "{{DLV - value}}"})
    assert len(ch.ops) == 1 and ch.extra == {"label": True}
    kinds = [s["d"] for s in ch.gtm]
    assert kinds[0].startswith("create trigger") and "Conversion Linker" in kinds[1] and "AW-987654321" in kinds[2]
    p = w.preview(U, "grt", ch)
    assert p["tag_manager"].startswith("Tag Manager GTM-TEST") and not p["needs_extra_confirmation"]
    assert any("DLV - value" in x for x in p["warnings"])  # the variable doesn't exist yet
    assert ads.calls[-1][0] is True and not any(c[0] == "POST" for c in gtm.calls)

    ads.rows_for.insert(0, ("conversion_action.resource_name", [{"conversion_action.tag_snippets": SNIPPET}]))
    done = w.apply(U, "grt", p["confirm_token"], False)
    assert done["status"] == "done"
    tag = next(v for v in gtm.items.values() if v.get("type") == "awct")
    params = {x["key"]: x["value"] for x in tag["parameter"]}
    assert params == {"conversionId": "987654321", "conversionLabel": "AbC-12_x", "conversionValue": "{{DLV - value}}"}
    trig = next(v for v in gtm.items.values() if v.get("type") == "pageview")
    assert tag["firingTriggerId"] == [trig["triggerId"]]
    assert len(gtm.items) == 4  # trigger, linker, google tag, conversion tag

    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    assert gtm.items == {}
    assert ads.calls[-1][1][0].conversion_action_operation.remove.endswith("/conversionActions/105")


def test_setup_existing_conversion_with_publish():
    w, ads, gtm, conv, g = setup_env([("FROM conversion_action", [CONV_ROW]), TRACKING])
    gtm.add("tags", {"name": "CL", "type": "gclidw", "firingTriggerId": [ALL_PAGES]})
    gtm.add("tags", {"name": "GT", "type": "googtag", "firingTriggerId": [ALL_PAGES],
                     "parameter": [{"type": "template", "key": "tagId", "value": "AW-987654321"}]})
    ch = g.setup("grt", U, "3993556480", {"container": "GTM-TEST", "conversion_action_id": "44",
                                          "trigger": {"kind": "custom_event", "event_name": "generate_lead"},
                                          "publish": True})
    assert not ch.ops and [s["k"] for s in ch.gtm] == ["trigger", "tag", "version", "publish"]
    assert ch.large
    done = w.apply(U, "grt", w.preview(U, "grt", ch)["confirm_token"], True)
    assert done["status"] == "done" and gtm.live.endswith("/versions/8")


def test_setup_needs_trigger_conditions():
    _, _, _, _, g = setup_env([TRACKING])
    with pytest.raises(AdsError, match="every page"):
        g.setup("grt", U, "3993556480", {"container": "GTM-TEST", "conversion_name": "X", "trigger": {"kind": "page_view"}})


def test_gtm_failure_after_ads_part_reports_partly_done_with_undo():
    w, ads, gtm, conv, g = setup_env([TRACKING])
    ch = g.setup("grt", U, "3993556480", {"container": "GTM-TEST", "conversion_name": "Form",
                                          "trigger": {"kind": "custom_event", "event_name": "lead"}})
    p = w.preview(U, "grt", ch)
    ads.rows_for.insert(0, ("conversion_action.resource_name", [{"conversion_action.tag_snippets": SNIPPET}]))
    gtm.fail_on = "POST " + WS + "/tags"
    done = w.apply(U, "grt", p["confirm_token"], False)
    assert done["status"] == "partly done" and "boom" in done["error"]
    up = w.undo_preview(U, "grt", done["undo_token"])
    gtm.fail_on = None
    w.apply(U, "grt", up["confirm_token"], False)
    assert gtm.items == {}  # the trigger that was made is deleted again
    assert ads.calls[-1][1][0].conversion_action_operation.remove.endswith("/conversionActions/105")


def test_gtm_only_failure_before_anything_releases_token():
    w, _, gtm, _, g = setup_env()
    ch = g.tag("grt", "GTM-TEST", None, "create", {"kind": "conversion_linker", "name": "CL"})
    p = w.preview(U, "grt", ch)
    gtm.fail_on = "POST"
    with pytest.raises(AdsError, match="boom"):
        w.apply(U, "grt", p["confirm_token"], False)
    gtm.fail_on = None
    assert w.apply(U, "grt", p["confirm_token"], False)["status"] == "done"  # can retry
