"""Phase 2 change tools: preview -> confirm -> apply, with Google faked."""


import pytest
from cryptography.fernet import Fernet

from app.ads import Account, AdsError, AdsGateway, row_to_dict
from app.config import Settings
from app.sealing import Sealer
from app.writes import Writer, to_micros

ACCT = Account("3993556480", "DXB - Travel And Tourism", "AED", "Asia/Dubai", False, "ENABLED", "3476426639",
               "via manager 347-642-6639")


class FakeAds(AdsGateway):
    def __init__(self, settings, rows):
        super().__init__(settings)
        self.rows_for = rows  # list of (substring, rows)
        self.calls = []
        self.queries = []

    def account(self, user, grt, customer_id):
        return ACCT

    def search(self, grt, acct, query, max_rows):
        self.queries.append(query)
        for needle, rows in self.rows_for:
            if needle in query:
                return rows, False
        return [], False

    def mutate(self, grt, acct, operations, validate_only):
        self.calls.append((validate_only, list(operations)))
        resp = self.types().get_type("MutateGoogleAdsResponse")
        for i, _ in enumerate(operations):
            r = self.types().get_type("MutateOperationResponse")
            r.campaign_result.resource_name = f"customers/{acct.customer_id}/campaigns/{1000 + i}"
            resp.mutate_operation_responses.append(r)
        return resp


def make(rows=()):
    st = Settings("https://x", "a", "b", Fernet.generate_key().decode(), allowed_emails=["w@z.test"])
    ads = FakeAds(st, list(rows))
    return Writer(st, ads, Sealer(st.token_key)), ads


BUDGET_ROWS = ("FROM campaign WHERE campaign.id = 11", [{
    "campaign.name": "Campaign #1", "campaign_budget.resource_name": "customers/3993556480/campaignBudgets/77",
    "campaign_budget.amount_micros": 20_000_000, "campaign_budget.explicitly_shared": False,
    "campaign_budget.reference_count": 1,
}])


def test_money_rounding():
    assert to_micros(12.345) == 12_350_000
    with pytest.raises(AdsError):
        to_micros(0)


def test_budget_increase_needs_extra_confirmation_and_is_one_time():
    w, ads = make([BUDGET_ROWS])
    p = w.preview("w@z.test", "grt", w.budget("grt", ACCT, "11", 40))
    assert ads.calls[-1][0] is True  # validate only
    assert "20.00 -> 40.00 AED" in p["details"][0]
    assert p["needs_extra_confirmation"]
    with pytest.raises(AdsError, match="extra confirmation"):
        w.apply("w@z.test", "grt", p["confirm_token"], False)
    out = w.apply("w@z.test", "grt", p["confirm_token"], True)
    assert out["status"] == "done"
    validate, ops = ads.calls[-1]
    assert validate is False
    assert ops[0].campaign_budget_operation.update.amount_micros == 40_000_000
    assert list(ops[0].campaign_budget_operation.update_mask.paths) == ["amount_micros"]
    with pytest.raises(AdsError, match="already applied"):
        w.apply("w@z.test", "grt", p["confirm_token"], True)


def test_small_budget_change_no_extra_confirmation():
    w, ads = make([BUDGET_ROWS])
    p = w.preview("w@z.test", "grt", w.budget("grt", ACCT, "11", 25))
    assert not p["needs_extra_confirmation"]
    assert w.apply("w@z.test", "grt", p["confirm_token"], False)["status"] == "done"


def test_token_belongs_to_user_and_cannot_be_forged():
    w, _ = make([BUDGET_ROWS])
    p = w.preview("w@z.test", "grt", w.budget("grt", ACCT, "11", 25))
    with pytest.raises(AdsError, match="someone else"):
        w.apply("other@z.test", "grt", p["confirm_token"], True)
    with pytest.raises(AdsError, match="invalid or expired"):
        w.apply("w@z.test", "grt", p["confirm_token"][:-4] + "AAAA", True)


def test_enabling_is_flagged_pausing_is_not():
    rows = [("FROM campaign WHERE", [{"campaign.id": 11, "campaign.name": "C1", "campaign.status": "PAUSED"},
                                     {"campaign.id": 12, "campaign.name": "C2", "campaign.status": "ENABLED"}])]
    w, _ = make(rows)
    ch = w.status_change("grt", ACCT, "campaign", ["11"], "ENABLED")
    assert ch.large and ch.ops[0].campaign_operation.update.status.name == "ENABLED"
    ch = w.status_change("grt", ACCT, "campaign", ["12"], "PAUSED")
    assert not ch.large
    with pytest.raises(AdsError, match="Couldn't find"):
        w.status_change("grt", ACCT, "campaign", ["99"], "PAUSED")


def test_keyword_ids_are_pairs():
    rows = [("FROM ad_group_criterion", [{
        "ad_group.id": 5, "ad_group.name": "AG", "ad_group_criterion.criterion_id": 9,
        "ad_group_criterion.keyword.text": "desert safari", "ad_group_criterion.keyword.match_type": "PHRASE",
        "ad_group_criterion.status": "ENABLED", "ad_group_criterion.negative": False}])]
    w, _ = make(rows)
    ch = w.status_change("grt", ACCT, "keyword", ["5~9"], "PAUSED")
    assert ch.ops[0].ad_group_criterion_operation.update.resource_name == "customers/3993556480/adGroupCriteria/5~9"
    with pytest.raises(AdsError, match="adGroupId~"):
        w.status_change("grt", ACCT, "keyword", ["9"], "PAUSED")


def test_removing_campaign_needs_exact_name():
    rows = [("FROM campaign WHERE", [{"campaign.id": 11, "campaign.name": "Campaign #1", "campaign.status": "PAUSED"}])]
    w, _ = make(rows)
    with pytest.raises(AdsError, match="exact name"):
        w.removal("grt", ACCT, "campaign", ["11"], None)
    ch = w.removal("grt", ACCT, "campaign", ["11"], ["campaign #1"])
    assert ch.ops[0].campaign_operation.remove == "customers/3993556480/campaigns/11" and ch.large


def test_create_campaign_is_paused_and_atomic():
    w, ads = make()
    ch = w.new_campaign("grt", ACCT, {
        "name": "Desert Safari - Search", "daily_budget": 50, "location_ids": ["2784"], "bidding": "maximize_clicks",
        "max_cpc_limit": 3, "ad_group_name": "Desert safari",
        "keywords": [{"text": "desert safari dubai", "match_type": "EXACT"}, {"text": "dune bashing"}],
        "ad": {"final_url": "https://example.com/safari",
               "headlines": ["Dubai Desert Safari", "Book Online Today", "Free Hotel Pickup"],
               "descriptions": ["Dune bashing, BBQ dinner and shows.", "Best price guaranteed. Book now."]},
    })
    kinds = [o._pb.WhichOneof("operation") for o in ch.ops]
    assert kinds == ["campaign_budget_operation", "campaign_operation", "campaign_criterion_operation",
                     "campaign_criterion_operation", "ad_group_operation", "ad_group_criterion_operation",
                     "ad_group_criterion_operation", "ad_group_ad_operation"]
    camp = ch.ops[1].campaign_operation.create
    assert camp.status.name == "PAUSED"
    assert camp.campaign_budget == ch.ops[0].campaign_budget_operation.create.resource_name
    assert camp.contains_eu_political_advertising.name == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
    assert camp.target_spend.cpc_bid_ceiling_micros == 3_000_000
    assert not camp.network_settings.target_content_network
    assert ch.ops[5].ad_group_criterion_operation.create.ad_group == ch.ops[4].ad_group_operation.create.resource_name
    assert ch.ops[6].ad_group_criterion_operation.create.keyword.match_type.name == "PHRASE"
    p = w.preview("w@z.test", "grt", ch)
    assert not p["needs_extra_confirmation"]
    w.apply("w@z.test", "grt", p["confirm_token"], False)
    assert len(ads.calls[-1][1]) == 8 and ads.calls[-1][0] is False


def test_ad_text_is_checked_locally():
    w, _ = make([("FROM ad_group WHERE", [{"ad_group.name": "AG"}])])
    with pytest.raises(AdsError, match="headline too long"):
        w.new_ad("grt", ACCT, "5", {"final_url": "https://e.com", "headlines": ["x" * 31, "b", "c"],
                                     "descriptions": ["d1", "d2"]}, "ENABLED")
    with pytest.raises(AdsError, match="3 to 15 headlines"):
        w.new_ad("grt", ACCT, "5", {"final_url": "https://e.com", "headlines": ["a"], "descriptions": ["d1", "d2"]}, "ENABLED")


def test_bidding_switch_sets_mask():
    w, _ = make([("FROM campaign WHERE campaign.id = 11", [{"campaign.name": "C", "campaign.bidding_strategy_type": "MANUAL_CPC"}])])
    ch = w.bidding("grt", ACCT, "11", "maximize_conversions", None, 25, None)
    op = ch.ops[0].campaign_operation
    assert list(op.update_mask.paths) == ["maximize_conversions"]
    assert op.update.maximize_conversions.target_cpa_micros == 25_000_000


def test_negative_keywords_campaign_level():
    w, _ = make([("FROM campaign WHERE campaign.id = 11", [{"campaign.name": "C"}])])
    ch = w.negatives("grt", ACCT, "campaign", "11", [{"text": "free", "match_type": "BROAD"}])
    c = ch.ops[0].campaign_criterion_operation.create
    assert c.negative and c.keyword.text == "free" and c.campaign.endswith("/campaigns/11")


def test_targeting_remove_uses_existing_criterion():
    rows = [("FROM campaign WHERE campaign.id = 11", [{"campaign.name": "C"}]),
            ("FROM campaign_criterion", [{"campaign_criterion.resource_name": "customers/3993556480/campaignCriteria/11~2784",
                                          "campaign_criterion.criterion_id": 2784, "campaign_criterion.type": "LOCATION",
                                          "campaign_criterion.negative": False}])]
    w, _ = make(rows)
    ch = w.targeting("grt", ACCT, "11", None, ["2784"], None, None, None)
    assert ch.ops[0].campaign_criterion_operation.remove.endswith("11~2784")
    assert ch.large  # removes every location
    ch = w.targeting("grt", ACCT, "11", ["2586"], None, None, None, None)
    assert ch.ops[0].campaign_criterion_operation.create.location.geo_target_constant == "geoTargetConstants/2586"


def test_raw_operations_from_json():
    w, _ = make()
    ch = w.raw(ACCT, [{"campaignOperation": {"update": {"resourceName": "customers/3993556480/campaigns/11", "name": "New"},
                                             "updateMask": "name"}}], "Rename")
    assert ch.ops[0].campaign_operation.update.name == "New" and ch.large
    with pytest.raises(AdsError, match="valid MutateOperation"):
        w.raw(ACCT, [{"nope": 1}], "x")


def test_avg_cpc_converted_from_micros():
    class M:
        average_cpc = 4_968_636
        cost_per_conversion = 0

    class Row:
        metrics = M()

    d = row_to_dict(Row(), ["metrics.average_cpc", "metrics.cost_per_conversion"])
    assert d == {"metrics.average_cpc": 4.97, "metrics.cost_per_conversion": 0.0}
