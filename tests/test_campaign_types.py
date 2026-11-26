"""Phase 5B: Performance Max, Display, remarketing lists and audience targeting, with Google faked."""

import hashlib
import struct

import pytest

from app.ads import AdsError
from app.audiences import Audiences, restrictions
from app.display import Display
from app.media import check_image, image_info, youtube_id
from app.pmax import Pmax
from tests.test_writes import ACCT, make

U = "w@z.test"


def png(w, h, salt=b""):
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h)
            + b"\x08\x02\x00\x00\x00" + b"\0\0\0\0" + salt)


def jpeg(w, h):
    return (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9
            + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, h, w) + b"\0" * 12)


IMAGES = {
    "https://e.com/land.png": png(1200, 628),
    "https://e.com/square.png": png(1200, 1200),
    "https://e.com/logo.png": png(512, 512, b"logo"),
    "https://e.com/wide.png": png(1200, 300),
    "https://e.com/tall.jpg": jpeg(960, 1200),
}


def env(rows=()):
    w, ads = make(rows)
    store = dict(IMAGES)
    w.image_bytes = lambda url: store[url]
    au = Audiences(w)
    return w, ads, au, Pmax(w, au), Display(w, au), store


def kinds(ch):
    return [o._pb.WhichOneof("operation") for o in ch.ops]


def creates(ch, kind):
    return [getattr(o, kind).create for o in ch.ops if o._pb.WhichOneof("operation") == kind]


ASSET_GROUP = {
    "name": "Desert safari",
    "final_url": "https://e.com/safari",
    "business_name": "DXB Tours",
    "headlines": ["Desert Safari Dubai", "Book Today", "Free Hotel Pickup"],
    "long_headlines": ["Evening desert safari with BBQ dinner and hotel pickup"],
    "descriptions": ["Dune bashing, camel rides and dinner.", "Trusted by 10,000 guests. Book online in two minutes today."],
    "landscape_images": ["https://e.com/land.png"],
    "square_images": ["https://e.com/square.png"],
    "logos": ["https://e.com/logo.png"],
    "youtube_videos": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
    "search_themes": ["desert safari dubai", "dune bashing"],
}

PMAX_ARGS = {"name": "PMax Safari", "daily_budget": 100, "location_ids": ["2784"], "asset_group": ASSET_GROUP}


# ------------------------------------------------------------------ media


def test_image_info_and_checks():
    assert image_info(png(10, 20)) == ("image/png", 10, 20)
    assert image_info(jpeg(640, 480)) == ("image/jpeg", 640, 480)
    assert image_info(b"GIF89a" + struct.pack("<HH", 300, 300)) == ("image/gif", 300, 300)
    with pytest.raises(AdsError, match="JPEG, PNG or GIF"):
        image_info(b"RIFF....WEBP")
    assert check_image(png(1200, 628), "landscape", "x")[1:] == (1200, 628)
    with pytest.raises(AdsError, match="right shape"):
        check_image(png(1200, 1200), "landscape", "x")
    with pytest.raises(AdsError, match="too small"):
        check_image(png(100, 100), "logo", "x")


def test_youtube_ids():
    assert youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    with pytest.raises(AdsError):
        youtube_id("not a video")


# ------------------------------------------------------------------ Performance Max


def test_pmax_campaign_ops_preview_apply_undo():
    w, ads, _, pm, _, _ = env()
    ch = pm.new_campaign("grt", ACCT, PMAX_ARGS)
    k = kinds(ch)
    assert k[:2] == ["campaign_budget_operation", "campaign_operation"]
    c = creates(ch, "campaign_operation")[0]
    assert c.status.name == "PAUSED" and c.advertising_channel_type.name == "PERFORMANCE_MAX"
    assert c.brand_guidelines_enabled is False and c.maximize_conversions is not None
    assert c.asset_automation_settings[0].asset_automation_type.name == "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"
    assert c.asset_automation_settings[0].asset_automation_status.name == "OPTED_OUT"
    assert creates(ch, "campaign_budget_operation")[0].explicitly_shared is False
    ag = creates(ch, "asset_group_operation")[0]
    assert ag.campaign == c.resource_name and list(ag.final_urls) == ["https://e.com/safari"]
    links = creates(ch, "asset_group_asset_operation")
    fts = sorted(link.field_type.name for link in links)
    assert fts.count("HEADLINE") == 3 and fts.count("DESCRIPTION") == 2 and fts.count("LONG_HEADLINE") == 1
    assert {"BUSINESS_NAME", "MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE", "LOGO", "YOUTUBE_VIDEO"} <= set(fts)
    assert all(link.asset_group == ag.resource_name for link in links)
    themes = [s.search_theme.text for s in creates(ch, "asset_group_signal_operation")]
    assert themes == ["desert safari dubai", "dune bashing"]
    assert len(ch.extra["img"]) == 3  # three downloaded images

    seen = []
    real = ads.mutate

    def spy(grt, acct, operations, validate_only):
        seen.append([bytes(o.asset_operation.create.image_asset.data) for o in operations
                     if o._pb.WhichOneof("operation") == "asset_operation" and o.asset_operation.create.image_asset.data])
        return real(grt, acct, operations, validate_only)

    ads.mutate = spy
    p = w.preview(U, "grt", ch)
    assert ads.calls[-1][0] is True and len(seen[-1]) == 3  # Google checked the real images
    assert len(p["confirm_token"]) < 20_000  # but the token doesn't carry them
    done = w.apply(U, "grt", p["confirm_token"], False)
    assert IMAGES["https://e.com/land.png"] in seen[-1] and len(seen[-1]) == 3  # downloaded again on apply
    up = w.undo_preview(U, "grt", done["undo_token"])
    w.apply(U, "grt", up["confirm_token"], False)
    removed = [getattr(o, o._pb.WhichOneof("operation")).remove for o in ads.calls[-1][1]]
    assert removed == ["customers/3993556480/campaigns/102", "customers/3993556480/campaignBudgets/101"]


def test_pmax_image_changed_after_preview_is_refused():
    w, ads, _, pm, _, store = env()
    p = w.preview(U, "grt", pm.new_campaign("grt", ACCT, PMAX_ARGS))
    store["https://e.com/land.png"] = png(1200, 628, b"different")
    with pytest.raises(AdsError, match="changed since the preview"):
        w.apply(U, "grt", p["confirm_token"], False)
    store["https://e.com/land.png"] = IMAGES["https://e.com/land.png"]
    assert w.apply(U, "grt", p["confirm_token"], False)["status"] == "done"  # token wasn't used up


def test_pmax_asset_rules():
    _, _, _, pm, _, _ = env()
    bad = dict(ASSET_GROUP, headlines=["One", "Two"])
    with pytest.raises(AdsError, match="3 to 15 headlines"):
        pm.new_campaign("grt", ACCT, dict(PMAX_ARGS, asset_group=bad))
    bad = dict(ASSET_GROUP, descriptions=["x" * 70, "y" * 80])
    with pytest.raises(AdsError, match="60 characters"):
        pm.new_campaign("grt", ACCT, dict(PMAX_ARGS, asset_group=bad))
    bad = dict(ASSET_GROUP, logos=[])
    with pytest.raises(AdsError, match="logo"):
        pm.new_campaign("grt", ACCT, dict(PMAX_ARGS, asset_group=bad))
    bad = dict(ASSET_GROUP, square_images=["https://e.com/land.png"])
    with pytest.raises(AdsError, match="right shape"):
        pm.new_campaign("grt", ACCT, dict(PMAX_ARGS, asset_group=bad))
    with pytest.raises(AdsError, match="maximize_conversions or maximize_conversion_value"):
        pm.new_campaign("grt", ACCT, dict(PMAX_ARGS, bidding="manual_cpc"))


def test_pmax_shopping_feed_only_and_root_listing_group():
    _, _, _, pm, _, _ = env()
    ch = pm.new_campaign("grt", ACCT, dict(PMAX_ARGS, merchant_center_id="123-456", feed_label="AE",
                                           bidding="maximize_conversion_value", target_roas=4.0,
                                           asset_group={"name": "All products", "final_url": "https://e.com"}))
    c = creates(ch, "campaign_operation")[0]
    assert c.shopping_setting.merchant_id == 123456 and c.shopping_setting.feed_label == "AE"
    assert c.maximize_conversion_value.target_roas == 4.0
    root = creates(ch, "asset_group_listing_group_filter_operation")[0]
    assert root.type_.name == "UNIT_INCLUDED" and root.listing_source.name == "SHOPPING"
    assert not creates(ch, "asset_group_asset_operation")
    assert any("Shopping ads" in x for x in ch.warnings) and any("linked" in x for x in ch.warnings)


LIST_ROWS = ("FROM user_list WHERE user_list.id IN", [
    {"user_list.id": 77, "user_list.name": "All visitors", "user_list.size_for_display": 50,
     "user_list.size_for_search": 50, "user_list.eligible_for_search": True, "user_list.membership_status": "OPEN"}])


def test_pmax_audience_signal():
    _, _, _, pm, _, _ = env([LIST_ROWS])
    ch = pm.new_campaign("grt", ACCT, dict(PMAX_ARGS, asset_group=dict(
        ASSET_GROUP, audience={"user_list_ids": ["77"], "interest_ids": ["80432"]})))
    aud = creates(ch, "audience_operation")[0]
    ag = creates(ch, "asset_group_operation")[0]
    assert aud.scope.name == "ASSET_GROUP" and aud.asset_group == ag.resource_name
    segs = aud.dimensions[0].audience_segments.segments
    assert segs[0].user_list.user_list.endswith("/userLists/77")
    assert segs[1].user_interest.user_interest_category.endswith("/userInterests/80432")
    signal = [s for s in creates(ch, "asset_group_signal_operation") if s.audience.audience][0]
    assert signal.audience.audience == aud.resource_name
    assert any("100+" in x for x in ch.warnings)  # list is too small to be used yet
    assert kinds(ch).index("audience_operation") > kinds(ch).index("asset_group_operation")


AG_ROW = ("FROM asset_group WHERE asset_group.id = 55", [{
    "asset_group.id": 55, "asset_group.name": "Safari", "asset_group.status": "ENABLED",
    "asset_group.final_urls": ["https://e.com"], "campaign.id": 11, "campaign.name": "PMax", "campaign.status": "ENABLED",
    "campaign.shopping_setting.merchant_id": 999, "campaign.brand_guidelines_enabled": False}])


def test_listing_groups_rebuild_tree():
    _, _, _, pm, _, _ = env([AG_ROW, ("FROM asset_group_listing_group_filter", [
        {"asset_group_listing_group_filter.resource_name": "customers/3993556480/assetGroupListingGroupFilters/55~1",
         "asset_group_listing_group_filter.id": 1, "asset_group_listing_group_filter.parent_listing_group_filter": ""},
        {"asset_group_listing_group_filter.resource_name": "customers/3993556480/assetGroupListingGroupFilters/55~2",
         "asset_group_listing_group_filter.id": 2,
         "asset_group_listing_group_filter.parent_listing_group_filter": "customers/3993556480/assetGroupListingGroupFilters/55~1"},
    ])])
    ch = pm.listing_groups("grt", ACCT, "55", "brand", ["Nike", "Adidas"], "include_only")
    ops = [o.asset_group_listing_group_filter_operation for o in ch.ops]
    assert [o.remove for o in ops[:2]] == ["customers/3993556480/assetGroupListingGroupFilters/55~2",
                                          "customers/3993556480/assetGroupListingGroupFilters/55~1"]  # child first
    root, nike, adidas, rest = (o.create for o in ops[2:])
    assert root.type_.name == "SUBDIVISION" and not root.parent_listing_group_filter
    assert nike.parent_listing_group_filter == root.resource_name and nike.type_.name == "UNIT_INCLUDED"
    assert nike.case_value.product_brand.value == "Nike"
    assert rest.type_.name == "UNIT_EXCLUDED" and "product_brand" in rest.case_value and not rest.case_value.product_brand.value
    assert ch.large
    ch = pm.listing_groups("grt", ACCT, "55", "custom_label_2", ["sale"], "exclude")
    node = ch.ops[3].asset_group_listing_group_filter_operation.create
    assert node.type_.name == "UNIT_EXCLUDED" and node.case_value.product_custom_attribute.index.name == "INDEX2"
    assert ch.ops[4].asset_group_listing_group_filter_operation.create.type_.name == "UNIT_INCLUDED"


def test_listing_groups_need_a_feed():
    row = dict(AG_ROW[1][0], **{"campaign.shopping_setting.merchant_id": 0})
    _, _, _, pm, _, _ = env([(AG_ROW[0], [row])])
    with pytest.raises(AdsError, match="no Merchant Center"):
        pm.listing_groups("grt", ACCT, "55", "all", None, "include_only")


AG_ASSETS = ("FROM asset_group_asset", [
    {"asset_group.id": 55, "asset_group_asset.resource_name": f"customers/3993556480/assetGroupAssets/55~{i}~HEADLINE",
     "asset_group_asset.field_type": "HEADLINE", "asset.id": i, "asset.text_asset.text": f"H{i}"} for i in range(1, 4)
])


def test_update_asset_group_add_and_remove():
    _, _, _, pm, _, _ = env([AG_ROW, AG_ASSETS])
    ch = pm.update_asset_group("grt", ACCT, "55", None, None, "PAUSED", {"headlines": ["New one"]}, ["1"])
    assert ch.ops[0].asset_group_operation.update.status.name == "PAUSED"
    link = creates(ch, "asset_group_asset_operation")[0]
    assert link.field_type.name == "HEADLINE" and link.asset_group.endswith("/assetGroups/55")
    assert ch.ops[-1].asset_group_asset_operation.remove.endswith("55~1~HEADLINE")
    with pytest.raises(AdsError, match="fewer than 3"):
        pm.update_asset_group("grt", ACCT, "55", None, None, None, None, ["1"])
    with pytest.raises(AdsError, match="isn't in the asset group|aren't in the asset group"):
        pm.update_asset_group("grt", ACCT, "55", None, None, None, None, ["999"])


def test_signals_replace_audience_and_remove_theme():
    _, _, _, pm, _, _ = env([AG_ROW, LIST_ROWS, ("FROM asset_group_signal", [
        {"asset_group_signal.resource_name": "customers/3993556480/assetGroupSignals/55~1",
         "asset_group_signal.search_theme.text": "old theme"},
        {"asset_group_signal.resource_name": "customers/3993556480/assetGroupSignals/55~2",
         "asset_group_signal.audience.audience": "customers/3993556480/audiences/9"}])])
    ch = pm.signals("grt", ACCT, "55", ["new theme"], ["Old Theme"], {"user_list_ids": ["77"]}, False)
    removes = [o.asset_group_signal_operation.remove for o in ch.ops if o.asset_group_signal_operation.remove]
    assert removes == ["customers/3993556480/assetGroupSignals/55~1", "customers/3993556480/assetGroupSignals/55~2"]
    assert creates(ch, "audience_operation") and ch.large
    with pytest.raises(AdsError, match="isn't a search theme"):
        pm.signals("grt", ACCT, "55", None, ["nope"], None, False)


# ------------------------------------------------------------------ Display and remarketing

AD = {
    "final_url": "https://e.com", "business_name": "DXB Tours", "headlines": ["Desert Safari"],
    "long_headline": "Evening desert safari with dinner", "descriptions": ["Book online today."],
    "landscape_images": ["https://e.com/land.png"], "square_images": ["https://e.com/square.png"],
    "logos": ["https://e.com/logo.png"], "landscape_logos": ["https://e.com/wide.png"],
}


def test_display_campaign():
    w, ads, _, _, d, _ = env([LIST_ROWS])
    ch = d.new_campaign("grt", ACCT, {"name": "Display", "daily_budget": 30, "location_ids": ["2784"],
                                      "targeting": {"interest_ids": ["80432"], "placements": ["https://www.timeout.com/"],
                                                    "topic_ids": ["67"], "keywords": ["desert safari"]},
                                      "excluded_placements": ["games.example.com"], "ad": AD})
    c = creates(ch, "campaign_operation")[0]
    assert c.advertising_channel_type.name == "DISPLAY" and c.status.name == "PAUSED"
    assert c.network_settings.target_content_network and not c.network_settings.target_google_search
    g = creates(ch, "ad_group_operation")[0]
    assert g.type_.name == "DISPLAY_STANDARD" and g.optimized_targeting_enabled is False
    assert [(r.targeting_dimension.name, r.bid_only) for r in g.targeting_setting.target_restrictions] == [("AUDIENCE", False)]
    crit = creates(ch, "ad_group_criterion_operation")
    assert crit[0].user_interest.user_interest_category.endswith("/userInterests/80432")
    assert crit[1].keyword.text == "desert safari" and crit[1].keyword.match_type.name == "BROAD"
    assert crit[2].placement.url == "www.timeout.com" and crit[3].topic.topic_constant == "topicConstants/67"
    neg = [x for x in creates(ch, "campaign_criterion_operation") if x.placement.url]
    assert neg[0].negative and neg[0].placement.url == "games.example.com"
    rda = creates(ch, "ad_group_ad_operation")[0].ad.responsive_display_ad
    assert rda.business_name == "DXB Tours" and rda.long_headline.text.startswith("Evening")
    assert len(rda.marketing_images) == 1 and len(rda.square_logo_images) == 1 and len(rda.logo_images) == 1
    p = w.preview(U, "grt", ch)
    assert ads.calls[-1][0] is True and p["confirm_token"]
    with pytest.raises(AdsError, match="ad_group_cpc_bid"):
        d.new_campaign("grt", ACCT, {"name": "D2", "daily_budget": 30, "bidding": "manual_cpc"})


def test_display_ad_needs_images():
    _, _, _, _, d, _ = env([("FROM ad_group WHERE ad_group.id = 5", [
        {"ad_group.name": "AG", "campaign.advertising_channel_type": "DISPLAY"}])])
    with pytest.raises(AdsError, match="landscape image"):
        d.new_ad("grt", ACCT, "5", dict(AD, square_images=[]), "ENABLED")
    assert d.new_ad("grt", ACCT, "5", AD, "PAUSED").ops


def test_remarketing_campaign_targets_lists():
    _, _, _, _, d, _ = env([LIST_ROWS])
    with pytest.raises(AdsError, match="user_list_ids"):
        d.new_campaign("grt", ACCT, {"name": "RM", "daily_budget": 10}, remarketing=True)
    ch = d.new_campaign("grt", ACCT, {"name": "RM", "daily_budget": 10, "user_list_ids": ["77"]}, remarketing=True)
    assert "remarketing" in ch.title
    crit = creates(ch, "ad_group_criterion_operation")[0]
    assert crit.user_list.user_list == "customers/3993556480/userLists/77"
    g = creates(ch, "ad_group_operation")[0]
    assert g.targeting_setting.target_restrictions[0].bid_only is False
    assert any("100+" in x for x in ch.warnings)


def test_create_remarketing_list_rules():
    _, _, au, _, _, _ = env()
    ch = au.create_list("grt", ACCT, "Safari visitors", ["/desert-safari", {"match": "starts_with", "value": "https://e.com/tours"}],
                        [{"match": "contains", "value": "/thank-you"}], 60, True, None)
    ul = ch.ops[0].user_list_operation.create
    assert ul.membership_life_span == 60 and ul.membership_status.name == "OPEN"
    flex = ul.rule_based_user_list.flexible_rule_user_list
    assert ul.rule_based_user_list.prepopulation_status.name == "REQUESTED"
    assert flex.inclusive_rule_operator.name == "OR" and len(flex.inclusive_operands) == 2
    item = flex.inclusive_operands[1].rule.rule_item_groups[0].rule_items[0]
    assert item.name == "url__" and item.string_rule_item.operator.name == "STARTS_WITH"
    assert flex.exclusive_operands[0].rule.rule_item_groups[0].rule_items[0].string_rule_item.value == "/thank-you"
    assert any("Google tag" in x for x in ch.warnings)
    with pytest.raises(AdsError, match="1 to 540"):
        au.create_list("grt", ACCT, "X", ["e.com"], None, 600, True, None)


def test_remarketing_list_duplicate_name():
    _, _, au, _, _, _ = env([("user_list.name = 'Visitors'", [{"user_list.id": 1}])])
    with pytest.raises(AdsError, match="already a remarketing list"):
        au.create_list("grt", ACCT, "Visitors", ["e.com"], None, 30, True, None)


def test_audience_observation_on_search_ad_group():
    w, _, au, _, _, _ = env([LIST_ROWS, ("FROM ad_group WHERE ad_group.id = 5", [{
        "ad_group.name": "AG", "campaign.name": "Search", "campaign.advertising_channel_type": "SEARCH",
        "ad_group.targeting_setting.target_restrictions": [{"targeting_dimension": 3, "bid_only": False}]}])])
    ch = au.targeting("grt", ACCT, "ad_group", "5", ["77"], None, None, "observation", 30, None)
    crit = ch.ops[0].ad_group_criterion_operation.create
    assert crit.user_list.user_list.endswith("/userLists/77") and crit.bid_modifier == pytest.approx(1.3)
    upd = ch.ops[1].ad_group_operation
    assert list(upd.update_mask.paths) == ["targeting_setting.target_restrictions"]
    got = [(r.targeting_dimension.name, r.bid_only) for r in upd.update.targeting_setting.target_restrictions]
    assert got == [("AUDIENCE", True)]
    assert any("1,000" in x for x in ch.warnings)  # 50 people is too few for Search
    ch = au.targeting("grt", ACCT, "ad_group", "5", ["77"], None, None, "exclude", None, None)
    assert ch.ops[0].ad_group_criterion_operation.create.negative and len(ch.ops) == 1


def test_audience_targeting_refuses_pmax():
    _, _, au, _, _, _ = env([("FROM campaign WHERE campaign.id = 11", [{
        "campaign.name": "PMax", "campaign.advertising_channel_type": "PERFORMANCE_MAX"}])])
    with pytest.raises(AdsError, match="set_pmax_signals"):
        au.targeting("grt", ACCT, "campaign", "11", ["77"], None, None, "observation", None, None)


def test_restrictions_parsing():
    w, _ = make()
    e = w.t.enums
    assert restrictions([{"targeting_dimension": "AUDIENCE", "bid_only": True}], e) == [("AUDIENCE", True)]
    assert restrictions([{"targeting_dimension": 3}], e) == [("AUDIENCE", False)]


def test_update_display_targeting():
    _, _, _, _, d, _ = env([("FROM ad_group WHERE ad_group.id = 5", [{
        "ad_group.name": "AG", "campaign.name": "D", "campaign.advertising_channel_type": "DISPLAY",
        "ad_group.optimized_targeting_enabled": True}])])
    ch = d.update_targeting("grt", ACCT, "5", {"topic_ids": ["67"]}, {"placements": ["bad.com"]}, ["123"], False)
    k = kinds(ch)
    assert k == ["ad_group_criterion_operation"] * 3 + ["ad_group_operation"]
    assert ch.ops[1].ad_group_criterion_operation.create.negative
    assert ch.ops[2].ad_group_criterion_operation.remove.endswith("/adGroupCriteria/5~123")
    assert ch.ops[3].ad_group_operation.update.optimized_targeting_enabled is False and ch.large


def test_list_audiences_flags_small_lists():
    _, _, au, _, _, _ = env([("FROM user_list", [
        {"user_list.id": 1, "user_list.name": "Big", "user_list.size_for_display": 5000, "user_list.size_for_search": 2000,
         "user_list.eligible_for_display": True, "user_list.eligible_for_search": True, "user_list.membership_status": "OPEN"},
        {"user_list.id": 2, "user_list.name": "Small", "user_list.size_for_display": 40, "user_list.size_for_search": 0,
         "user_list.eligible_for_display": True, "user_list.eligible_for_search": True, "user_list.membership_status": "OPEN"}]),
        ("ad_group_criterion.type = 'USER_LIST'", [{"campaign.name": "C", "ad_group.name": "AG",
                                                    "ad_group_criterion.user_list.user_list": "customers/1/userLists/1"}])])
    out = au.list("grt", ACCT, False)
    big, small = out["remarketing_lists"]
    assert big["usable_on_search"] and big["used_in"] == ["'AG' in 'C'"]
    assert not small["usable_on_display"] and small["notes"]


def test_find_interests_filters_locally():
    _, _, au, _, _, _ = env([("FROM user_interest", [
        {"user_interest.user_interest_id": 1, "user_interest.name": "Travel Buffs", "user_interest.taxonomy_type": "AFFINITY"},
        {"user_interest.user_interest_id": 2, "user_interest.name": "Trips to Dubai", "user_interest.taxonomy_type": "IN_MARKET"},
        {"user_interest.user_interest_id": 3, "user_interest.name": "Pet Lovers", "user_interest.taxonomy_type": "AFFINITY"}])])
    out = au.find("grt", ACCT, "dubai", "in_market")
    assert [r["id"] for r in out["results"]] == ["2"] and out["use_as"] == "interest_ids"


def test_list_asset_groups_reports_gaps():
    _, _, _, pm, _, _ = env([("FROM asset_group WHERE asset_group.status", [{
        "campaign.id": 11, "campaign.name": "PMax", "campaign.status": "PAUSED", "asset_group.id": 55,
        "asset_group.name": "Safari", "asset_group.status": "ENABLED", "asset_group.ad_strength": "POOR"}]),
        AG_ASSETS])
    out = pm.list("grt", ACCT, None, 30)
    g = out["asset_groups"][0]
    assert len(g["assets"]["headlines"]) == 3 and g["ad_strength"] == "POOR"
    assert any("descriptions" in x for x in g["to_improve"]) and any("signals" in x for x in g["to_improve"])


def test_undo_skips_children_of_new_asset_group():
    w, _ = make()
    out = w.removes_for_created(["customers/1/assetGroups/5", "customers/1/assetGroupAssets/5~9~HEADLINE",
                                 "customers/1/assetGroupSignals/5~3", "customers/1/assetGroupSignals/6~4"])
    got = [getattr(o, o._pb.WhichOneof("operation")).remove for o in out]
    assert got == ["customers/1/assetGroups/5", "customers/1/assetGroupSignals/6~4"]


async def test_server_registers_phase5_tools():
    from tests.test_flow import make_settings
    from app.server import build_server

    names = {t.name for t in await build_server(make_settings()).list_tools()}
    assert {"create_pmax_campaign", "create_asset_group", "update_asset_group", "set_pmax_signals",
            "set_pmax_listing_groups", "list_asset_groups", "create_display_campaign", "create_remarketing_campaign",
            "create_display_ad_group", "create_responsive_display_ad", "update_display_targeting", "list_audiences",
            "find_audience_segments", "create_remarketing_list", "update_remarketing_list",
            "update_audience_targeting"} <= names


def test_hash_is_stable():
    assert hashlib.sha256(IMAGES["https://e.com/land.png"]).hexdigest() == hashlib.sha256(png(1200, 628)).hexdigest()
