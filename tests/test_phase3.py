"""Phase 3: roles, spend limits, undo, extensions, schedules, bid adjustments, health check, website reading."""

import dataclasses

import pytest

from app.ads import AdsError
from app.assets import Extras
from app.health import HealthCheck
from app.research import _PageParser, check_url
from tests.test_writes import ACCT, BUDGET_ROWS, make


def with_settings(w, **kw):
    w.settings = dataclasses.replace(w.settings, **kw)
    return w


def test_read_only_user_cannot_change():
    w, _ = make([BUDGET_ROWS])
    with_settings(w, read_only_emails=["viewer@z.test"])
    with pytest.raises(AdsError, match="read-only"):
        w.preview("viewer@z.test", "grt", w.budget("grt", ACCT, "11", 25))


ALL_CAMPAIGNS = ("WHERE campaign.status != 'REMOVED'", [
    {"campaign.id": 11, "campaign.status": "ENABLED", "campaign_budget.resource_name": "customers/3993556480/campaignBudgets/77",
     "campaign_budget.amount_micros": 20_000_000},
    {"campaign.id": 12, "campaign.status": "PAUSED", "campaign_budget.resource_name": "customers/3993556480/campaignBudgets/78",
     "campaign_budget.amount_micros": 50_000_000},
])


def test_spend_limit_blocks_going_over():
    w, _ = make([BUDGET_ROWS, ALL_CAMPAIGNS, ("FROM campaign WHERE campaign.id IN", [
        {"campaign.id": 12, "campaign.name": "C2", "campaign.status": "PAUSED"}])])
    with_settings(w, spend_limits={"3993556480": 60.0})
    ch = w.budget("grt", ACCT, "11", 30)
    assert any("30.00 of the 60.00" in line for line in ch.lines)
    with pytest.raises(AdsError, match="spending limit"):
        w.budget("grt", ACCT, "11", 61)
    with pytest.raises(AdsError, match="spending limit"):  # enabling C2 adds its 50/day
        w.status_change("grt", ACCT, "campaign", ["12"], "ENABLED")


def test_undo_puts_old_budget_back():
    w, ads = make([BUDGET_ROWS])
    row = ads.types().get_type("GoogleAdsRow")
    row.campaign_budget.amount_micros = 20_000_000
    ads.raw_rows = [row]
    p = w.preview("w@z.test", "grt", w.budget("grt", ACCT, "11", 25))
    done = w.apply("w@z.test", "grt", p["confirm_token"], False)
    assert done["undo_token"]
    up = w.undo_preview("w@z.test", "grt", done["undo_token"])
    w.apply("w@z.test", "grt", up["confirm_token"], up["needs_extra_confirmation"] != [])
    op = ads.calls[-1][1][0].campaign_budget_operation
    assert op.update.amount_micros == 20_000_000 and list(op.update_mask.paths) == ["amount_micros"]
    with pytest.raises(AdsError, match="Only the person"):
        w.undo_preview("other@z.test", "grt", done["undo_token"])


def test_undo_of_new_campaign_removes_campaign_and_budget_only():
    w, ads = make()
    ch = w.new_campaign("grt", ACCT, {"name": "New", "daily_budget": 10, "location_ids": ["2784"], "ad_group_name": "AG",
                                      "keywords": [{"text": "safari"}]})
    p = w.preview("w@z.test", "grt", ch)
    done = w.apply("w@z.test", "grt", p["confirm_token"], False)
    up = w.undo_preview("w@z.test", "grt", done["undo_token"])
    w.apply("w@z.test", "grt", up["confirm_token"], False)
    removed = [getattr(o, o._pb.WhichOneof("operation")).remove for o in ads.calls[-1][1]]
    assert removed == ["customers/3993556480/campaigns/102", "customers/3993556480/campaignBudgets/101"]


def test_removals_report_cannot_undo():
    w, _ = make([("FROM ad_group_ad", [{"ad_group.id": 5, "ad_group.name": "AG", "ad_group_ad.ad.id": 9,
                                         "ad_group_ad.status": "ENABLED"}])])
    p = w.preview("w@z.test", "grt", w.removal("grt", ACCT, "ad", ["5~9"], None))
    done = w.apply("w@z.test", "grt", p["confirm_token"], True)
    assert "removals" in done["cannot_undo"] and "undo_token" not in done


def test_sitelinks_create_assets_and_links():
    w, _ = make([("FROM campaign WHERE campaign.id = 11", [{"campaign.name": "C"}])])
    ch = Extras(w).sitelinks("grt", ACCT, "campaign", "11", [
        {"text": "Desert Safari", "url": "https://e.com/safari", "description1": "Pickup included", "description2": "Book today"},
        {"text": "Dhow Cruise", "url": "https://e.com/dhow"}])
    kinds = [o._pb.WhichOneof("operation") for o in ch.ops]
    assert kinds == ["asset_operation", "campaign_asset_operation"] * 2
    assert ch.ops[1].campaign_asset_operation.create.asset == ch.ops[0].asset_operation.create.resource_name
    assert ch.ops[1].campaign_asset_operation.create.field_type.name == "SITELINK"
    with pytest.raises(AdsError, match="too long"):
        Extras(w).sitelinks("grt", ACCT, "account", None, [{"text": "x" * 26, "url": "https://e.com"}])


def test_callouts_account_level():
    w, _ = make()
    ch = Extras(w).callouts("grt", ACCT, "account", None, ["Free Pickup", "24/7 Support"])
    assert ch.ops[1].customer_asset_operation.create.field_type.name == "CALLOUT"
    assert ch.ops[0].asset_operation.create.callout_asset.callout_text == "Free Pickup"


def test_schedule_weekdays_replaces_existing():
    w, _ = make([("FROM campaign WHERE campaign.id = 11", [{"campaign.name": "C"}]),
                 ("AD_SCHEDULE", [{"campaign_criterion.resource_name": "customers/3993556480/campaignCriteria/11~555"}])])
    ch = Extras(w).schedule("grt", ACCT, "11", [{"days": "WEEKDAYS", "start_hour": 9, "end_hour": 18, "bid_adjustment_pct": 10}], True)
    assert ch.ops[0].campaign_criterion_operation.remove.endswith("11~555")
    creates = [o.campaign_criterion_operation.create for o in ch.ops[1:]]
    assert [c.ad_schedule.day_of_week.name for c in creates] == ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]
    assert creates[0].ad_schedule.start_hour == 9 and creates[0].bid_modifier == pytest.approx(1.1)
    with pytest.raises(AdsError, match="Unknown day"):
        Extras(w).schedule("grt", ACCT, "11", [{"days": ["FUNDAY"]}], False)


def test_device_adjustment_create_and_exclude():
    w, _ = make([("FROM campaign WHERE campaign.id = 11", [{"campaign.name": "C"}])])
    ch = Extras(w).bid_adjustments("grt", ACCT, "11", {"TABLET": -100, "MOBILE": 20}, None)
    t, m = (o.campaign_criterion_operation.create for o in ch.ops)
    assert t.device.type_.name == "TABLET" and t.bid_modifier == 0
    assert m.bid_modifier == pytest.approx(1.2)
    with pytest.raises(AdsError, match="isn't targeted"):
        Extras(w).bid_adjustments("grt", ACCT, "11", None, {"2784": 10})


def test_health_check_finds_problems():
    _, ads = make([
        ("FROM campaign WHERE campaign.status = 'ENABLED'", [{
            "campaign.id": 11, "campaign.name": "Safari", "campaign.advertising_channel_type": "SEARCH",
            "campaign.network_settings.target_content_network": True, "campaign_budget.amount_micros": 20_000_000,
            "metrics.cost_micros": 600_000_000, "metrics.clicks": 120, "metrics.conversions": 6.0,
            "metrics.search_budget_lost_impression_share": 0.55, "metrics.search_rank_lost_impression_share": 0.1}]),
        ("FROM conversion_action", [{"conversion_action.name": "Lead", "conversion_action.primary_for_goal": True}]),
        ("FROM search_term_view", [{"search_term_view.search_term": "free safari", "search_term_view.status": "NONE",
                                    "campaign.id": 11, "campaign.name": "Safari", "metrics.cost_micros": 150_000_000,
                                    "metrics.conversions": 0}]),
        ("FROM ad_group_criterion", []),
        ("FROM ad_group_ad", []),
        ("FROM ad_group WHERE", [{"campaign.name": "Safari", "campaign.advertising_channel_type": "SEARCH",
                                  "ad_group.id": 5, "ad_group.name": "Main"}]),
        ("FROM customer_asset", []),
        ("FROM campaign_asset", []),
    ])
    out = HealthCheck(ads, "grt", ACCT, 30).run()
    areas = {(i["severity"], i["area"]) for i in out["issues"]}
    assert ("high", "budget") in areas and ("high", "ads") in areas and ("medium", "settings") in areas
    neg = next(i for i in out["issues"] if i["area"] == "search terms")
    assert neg["fix_with"]["tool"] == "add_negative_keywords"
    assert neg["fix_with"]["arguments"]["keywords"] == [{"text": "free safari", "match_type": "EXACT"}]
    assert out["summary"]["cost_per_conversion"] == 100.0
    assert 0 <= out["health_score"] < 100 and not out["checks_skipped"]


def test_health_check_no_tracking_is_high():
    _, ads = make([("FROM conversion_action", [])])
    out = HealthCheck(ads, "grt", ACCT, 30).run()
    assert any(i["severity"] == "high" and i["area"] == "tracking" for i in out["issues"])


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/computeMetadata/v1/", "http://metadata.google.internal/", "http://localhost:8080/",
    "http://127.0.0.1/", "http://10.0.0.5/", "file:///etc/passwd", "https://example.com:8443/", "http://[::1]/",
])
def test_website_reader_blocks_private_addresses(url):
    with pytest.raises(AdsError):
        check_url(url)


def test_page_parser():
    p = _PageParser()
    p.feed("<html><head><title>Dubai Safari Tours</title><meta name='description' content='Best desert safari'>"
           "<script>var x=1</script></head><body><nav>Menu</nav><h1>Desert Safari</h1><p>Call +971 50 123 4567 today.</p>"
           "<a href='/dhow'>Dhow</a></body></html>")
    assert p.title == "Dubai Safari Tours" and p.description == "Best desert safari"
    assert p.headings == ["Desert Safari"] and "var x" not in " ".join(p.text) and "Menu" not in " ".join(p.text)
    assert p.links == ["/dhow"]
