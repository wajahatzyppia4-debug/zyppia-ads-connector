"""Phase 5: Display campaigns, responsive display ads and remarketing campaigns.

Display ads show as banners on websites, apps, YouTube and Gmail. A
remarketing campaign is a Display campaign that only shows ads to people on
remarketing lists (see audiences.py).

Same preview -> apply_change flow as writes.py; new campaigns are PAUSED.
"""

from __future__ import annotations

import re
import time
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP

from .ads import Account, AdsError
from .audiences import Audiences
from .media import Creative, existing_images, https_url, text, texts
from .writes import CID, MAX_ITEMS, PREVIEW, Bidding, Change, Writer, _check_keyword, _num, money, to_micros

AD_SHAPE = (
    "Responsive display ad: {final_url, business_name (max 25), headlines (1-5, max 30 chars), long_headline "
    "(max 90), descriptions (1-5, max 90), landscape_images (1+, 1.91:1 e.g. 1200x628), square_images (1+, 1:1 "
    "e.g. 1200x1200), logos (optional, square), landscape_logos (optional, 4:1), youtube_videos (optional)}. "
    "Images are public https addresses of JPEG/PNG/GIF files (max 5 MB) or image asset IDs already in the account."
)
TARGETING_SHAPE = (
    "Who sees the ads (all optional): {user_list_ids (remarketing lists), interest_ids, custom_audience_ids "
    "(find_audience_segments), keywords (words about the websites), placements (website or app addresses), "
    "topic_ids (find_audience_segments kind=topics)}."
)


def _placement(url: str) -> str:
    u = str(url or "").strip()
    u = re.sub(r"^https?://", "", u, flags=re.I).rstrip("/")
    if not u or " " in u:
        raise AdsError(f"'{url}' isn't a website address.")
    return u


class Display:
    def __init__(self, w: Writer, au: Audiences):
        self.w, self.au = w, au

    # ------------------------------------------------------------- ads

    def rda_ops(self, grt: str, acct: Account, ch: Change, cr: Creative, ad_group_rn: str, ad: dict[str, Any],
                status: str) -> None:
        url = https_url(ad.get("final_url"))
        business = text(ad.get("business_name"), 25, "business name")
        heads = texts(ad.get("headlines"), 30, "headline", 1, 5)
        long_head = text(ad.get("long_headline"), 90, "long headline")
        descs = texts(ad.get("descriptions"), 90, "description", 1, 5)
        land, square = ad.get("landscape_images") or [], ad.get("square_images") or []
        logos, wide_logos = ad.get("logos") or [], ad.get("landscape_logos") or []
        videos = ad.get("youtube_videos") or []
        if not land or not square:
            raise AdsError("A responsive display ad needs at least 1 landscape image (1.91:1) and 1 square image (1:1).")
        if len(land) + len(square) > 15:
            raise AdsError("Give at most 15 landscape and square images together.")
        if len(logos) > 5 or len(wide_logos) > 5 or len(videos) > 5:
            raise AdsError("Give at most 5 logos, 5 wide logos and 5 videos.")
        have = existing_images(self.w, grt, acct, [*land, *square, *logos, *wide_logos])
        made = {
            "marketing_images": [cr.image_asset(x, "landscape", have) for x in land],
            "square_marketing_images": [cr.image_asset(x, "square", have) for x in square],
            "square_logo_images": [cr.image_asset(x, "logo", have) for x in logos],
            "logo_images": [cr.image_asset(x, "landscape_logo", have) for x in wide_logos],
        }
        vids = [cr.video_asset(v) for v in videos]
        e = self.w.t.enums
        op = self.w.op()
        a = op.ad_group_ad_operation.create
        a.ad_group = ad_group_rn
        a.status = e.AdGroupAdStatusEnum[status]
        a.ad.final_urls.append(url)
        rda = a.ad.responsive_display_ad
        rda.business_name = business
        for h in heads:
            t = self.w.t.get_type("AdTextAsset")
            t.text = h
            rda.headlines.append(t)
        rda.long_headline.text = long_head
        for d in descs:
            t = self.w.t.get_type("AdTextAsset")
            t.text = d
            rda.descriptions.append(t)
        for field_name, items in made.items():
            for m in items:
                img = self.w.t.get_type("AdImageAsset")
                img.asset = m.rn
                getattr(rda, field_name).append(img)
        for v in vids:
            va = self.w.t.get_type("AdVideoAsset")
            va.asset = v.rn
            rda.youtube_videos.append(va)
        ch.ops.append(op)
        ch.lines.append(f"responsive display ad ({status}) to {url}: business '{business}', {len(heads)} headlines, "
                        f"long headline, {len(descs)} descriptions")
        for field_name, items in made.items():
            ch.lines += [f"  {field_name.replace('_', ' ')}: {m.label}" for m in items]
        ch.lines += [f"  video: {v.label}" for v in vids]
        if not logos:
            ch.warnings.append("No logo: Google shows the ad without one. A square logo usually helps.")

    def new_ad(self, grt: str, acct: Account, ad_group_id: str, ad: dict[str, Any], status: str) -> Change:
        agid = _num(ad_group_id, "ad group ID")
        rows = self.w.rows(grt, acct, "SELECT ad_group.name, campaign.advertising_channel_type FROM ad_group "
                                      f"WHERE ad_group.id = {agid}")
        if not rows:
            raise AdsError(f"Ad group {agid} not found in {acct.name}.")
        if rows[0].get("campaign.advertising_channel_type") != "DISPLAY":
            raise AdsError("Responsive display ads go in Display campaigns. For Search use create_responsive_search_ad.")
        ch = Change(acct, f"Create a display ad in '{rows[0]['ad_group.name']}'")
        self.rda_ops(grt, acct, ch, Creative(self.w, acct, ch), self.w.rn(acct, "adGroups", agid), ad, status)
        ch.warnings.append("New ads go through Google's policy review (usually within a day) before they show.")
        return ch

    # ------------------------------------------------------------- targeting

    def targeting_ops(self, grt: str, acct: Account, ch: Change, ad_group_rn: str, tg: dict[str, Any] | None,
                      negative: bool = False) -> int:
        """Ad group criteria for audiences, keywords, placements and topics. Returns how many audiences were added."""
        tg = tg or {}
        unknown = set(tg) - {"user_list_ids", "interest_ids", "custom_audience_ids", "keywords", "placements", "topic_ids"}
        if unknown:
            raise AdsError(f"Unknown targeting: {', '.join(sorted(unknown))}. " + TARGETING_SHAPE)
        e = self.w.t.enums
        segs = self.au.segments(grt, acct, tg.get("user_list_ids"), tg.get("interest_ids"),
                                tg.get("custom_audience_ids"), "DISPLAY", ch)
        count = 0

        def criterion():
            op = self.w.op()
            c = op.ad_group_criterion_operation.create
            c.ad_group = ad_group_rn
            c.status = e.AdGroupCriterionStatusEnum.ENABLED
            if negative:
                c.negative = True
            ch.ops.append(op)
            return c

        verb = "exclude" if negative else "show on / to"
        for kind, rn, label in segs:
            self.au.set_segment(criterion(), kind, rn)
            ch.lines.append(f"{verb} {label}")
            count += 1
        for k in tg.get("keywords") or []:
            kw = _check_keyword(k if isinstance(k, str) else k.get("text", ""))
            c = criterion()
            c.keyword.text = kw
            c.keyword.match_type = e.KeywordMatchTypeEnum.BROAD
            ch.lines.append(f"{verb} pages about '{kw}'")
        for p in tg.get("placements") or []:
            c = criterion()
            c.placement.url = _placement(p)
            ch.lines.append(f"{verb} placement {c.placement.url}")
        for t in tg.get("topic_ids") or []:
            c = criterion()
            c.topic.topic_constant = f"topicConstants/{_num(t, 'topic ID')}"
            ch.lines.append(f"{verb} topic {_num(t, 'topic ID')}")
        if len(ch.ops) > MAX_ITEMS * 3:
            raise AdsError("Too much targeting in one change; split it up.")
        return count

    def update_targeting(self, grt: str, acct: Account, ad_group_id: str, add: dict[str, Any] | None,
                         exclude: dict[str, Any] | None, remove_ids: list[str] | None,
                         optimized_targeting: bool | None) -> Change:
        agid = _num(ad_group_id, "ad group ID")
        rows = self.w.rows(grt, acct, "SELECT ad_group.name, campaign.name, campaign.advertising_channel_type, "
                                      f"ad_group.optimized_targeting_enabled FROM ad_group WHERE ad_group.id = {agid}")
        if not rows:
            raise AdsError(f"Ad group {agid} not found in {acct.name}.")
        r = rows[0]
        if r.get("campaign.advertising_channel_type") != "DISPLAY":
            raise AdsError("This tool is for Display ad groups. For Search keywords use add_keywords; for audiences on "
                           "Search use update_audience_targeting.")
        ch = Change(acct, f"Change targeting of display ad group '{r['ad_group.name']}'")
        rn = self.w.rn(acct, "adGroups", agid)
        self.targeting_ops(grt, acct, ch, rn, add)
        self.targeting_ops(grt, acct, ch, rn, exclude, negative=True)
        for cid in remove_ids or []:
            op = self.w.op()
            op.ad_group_criterion_operation.remove = self.w.rn(acct, "adGroupCriteria", f"{agid}~{_num(cid, 'criterion ID')}")
            ch.ops.append(op)
            ch.lines.append(f"stop targeting criterion {_num(cid, 'criterion ID')}")
        if optimized_targeting is not None and optimized_targeting != bool(r.get("ad_group.optimized_targeting_enabled")):
            op = self.w.op()
            g = op.ad_group_operation.update
            g.resource_name = rn
            g.optimized_targeting_enabled = optimized_targeting
            op.ad_group_operation.update_mask.paths.append("optimized_targeting_enabled")
            ch.ops.append(op)
            ch.lines.append("optimized targeting (Google finds similar people): " + ("on" if optimized_targeting else "off"))
        if not ch.ops:
            raise AdsError("Nothing to change. " + TARGETING_SHAPE)
        if remove_ids:
            ch.large.append("removing targeting can't be undone automatically")
        return ch

    # ------------------------------------------------------------- ad groups and campaigns

    def _ad_group(self, grt: str, acct: Account, ch: Change, cr: Creative, campaign_rn: str, ag_rn: str, name: str,
                  cpc_bid: float | None, status: str, targeting: dict[str, Any] | None, audience_mode: str,
                  optimized_targeting: bool, ad: dict[str, Any] | None) -> None:
        e = self.w.t.enums
        name = text(name, 255, "ad group name")
        op = self.w.op()
        g = op.ad_group_operation.create
        g.resource_name = ag_rn
        g.name = name
        g.campaign = campaign_rn
        g.status = e.AdGroupStatusEnum[status]
        g.type_ = e.AdGroupTypeEnum.DISPLAY_STANDARD
        g.optimized_targeting_enabled = optimized_targeting
        line = f"display ad group '{name}' ({status})"
        if cpc_bid:
            g.cpc_bid_micros = to_micros(cpc_bid, "ad group bid")
            line += f", default max CPC {money(g.cpc_bid_micros)} {acct.currency}"
        ch.ops.append(op)
        ch.lines.append(line)
        n = self.targeting_ops(grt, acct, ch, ag_rn, targeting)
        if n:
            tr = self.w.t.get_type("TargetRestriction")
            tr.targeting_dimension = e.TargetingDimensionEnum.AUDIENCE
            tr.bid_only = audience_mode == "observation"
            g.targeting_setting.target_restrictions.append(tr)
            ch.lines.append("audiences: " + ("ONLY show ads to these people" if audience_mode == "targeting"
                                             else "observe only (ads show to everyone)"))
        if not (targeting and any(targeting.values())) and not optimized_targeting:
            ch.warnings.append("No targeting and optimized targeting off: the ads could show on any website. "
                               "Add audiences, topics, placements or keywords.")
        ch.lines.append("optimized targeting (Google finds similar people): " + ("on" if optimized_targeting else "off"))
        if ad:
            self.rda_ops(grt, acct, ch, cr, ag_rn, ad, "ENABLED")

    def new_ad_group(self, grt: str, acct: Account, campaign_id: str, a: dict[str, Any]) -> Change:
        cid = _num(campaign_id, "campaign ID")
        rows = self.w.rows(grt, acct, "SELECT campaign.name, campaign.status, campaign.advertising_channel_type FROM campaign "
                                      f"WHERE campaign.id = {cid}")
        if not rows:
            raise AdsError(f"Campaign {cid} not found in {acct.name}.")
        if rows[0].get("campaign.advertising_channel_type") != "DISPLAY":
            raise AdsError("That isn't a Display campaign. For Search use create_ad_group; for Performance Max "
                           "use create_asset_group.")
        ch = Change(acct, f"Create display ad group '{a.get('name')}' in '{rows[0]['campaign.name']}'")
        self._ad_group(grt, acct, ch, Creative(self.w, acct, ch), self.w.rn(acct, "campaigns", cid),
                       self.w.rn(acct, "adGroups", -3), a.get("name"), a.get("cpc_bid"), a.get("status") or "PAUSED",
                       a.get("targeting"), a.get("audience_mode") or "targeting", bool(a.get("optimized_targeting")),
                       a.get("ad"))
        if (a.get("status") or "PAUSED") == "ENABLED" and rows[0]["campaign.status"] == "ENABLED":
            ch.large.append("the new ad group is on, in a campaign that's on, so it can start spending money")
        return ch

    def new_campaign(self, grt: str, acct: Account, a: dict[str, Any], remarketing: bool = False) -> Change:
        e = self.w.t.enums
        name = text(a.get("name"), 255, "campaign name")
        existing = self.w.rows(grt, acct, "SELECT campaign.id FROM campaign WHERE campaign.name = '"
                               + name.replace("\\", "\\\\").replace("'", "\\'") + "' AND campaign.status != 'REMOVED'")
        if existing:
            raise AdsError(f"There's already a campaign called '{name}' in {acct.name}. Pick another name.")
        targeting = dict(a.get("targeting") or {})
        if remarketing:
            if not a.get("user_list_ids"):
                raise AdsError("A remarketing campaign needs user_list_ids (see list_audiences, or make one with "
                               "create_remarketing_list).")
            targeting["user_list_ids"] = list(a["user_list_ids"])
        kind = "remarketing" if remarketing else "display"
        ch = Change(acct, f"Create {kind} campaign '{name}' (PAUSED)")
        cr = Creative(self.w, acct, ch)
        budget_rn, camp_rn = self.w.rn(acct, "campaignBudgets", -1), self.w.rn(acct, "campaigns", -2)

        op = self.w.op()
        b = op.campaign_budget_operation.create
        b.resource_name = budget_rn
        b.name = f"{name} budget {int(time.time())}"
        b.amount_micros = to_micros(a["daily_budget"], "daily budget")
        b.delivery_method = e.BudgetDeliveryMethodEnum.STANDARD
        b.explicitly_shared = False
        ch.ops.append(op)
        ch.lines.append(f"daily budget {money(b.amount_micros)} {acct.currency}")

        op = self.w.op()
        c = op.campaign_operation.create
        c.resource_name = camp_rn
        c.name = name
        c.status = e.CampaignStatusEnum.PAUSED
        c.advertising_channel_type = e.AdvertisingChannelTypeEnum.DISPLAY
        c.campaign_budget = budget_rn
        c.network_settings.target_google_search = False
        c.network_settings.target_search_network = False
        c.network_settings.target_content_network = True
        c.network_settings.target_partner_search_network = False
        c.geo_target_type_setting.positive_geo_target_type = e.PositiveGeoTargetTypeEnum.PRESENCE
        c.contains_eu_political_advertising = e.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        strategy = a.get("bidding") or "maximize_conversions"
        self.w._set_bidding(c, strategy, a.get("max_cpc_limit"), a.get("target_cpa"), a.get("target_roas"))
        if a.get("end_date"):
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", a["end_date"]):
                raise AdsError("end_date must be YYYY-MM-DD.")
            c.end_date_time = f"{a['end_date']} 23:59:59"
        ch.ops.append(op)
        ch.lines.append(f"campaign '{name}', status PAUSED, Google Display Network (websites, apps, YouTube, Gmail)")
        ch.lines.append("bidding: " + self.w._describe_bidding(acct, strategy, a.get("max_cpc_limit"), a.get("target_cpa"),
                                                               a.get("target_roas")))
        if a.get("end_date"):
            ch.lines.append(f"ends {a['end_date']}")
        self.w.geo_lang_ops(ch, camp_rn, a)
        for p in a.get("excluded_placements") or []:
            op = self.w.op()
            cc = op.campaign_criterion_operation.create
            cc.campaign = camp_rn
            cc.negative = True
            cc.placement.url = _placement(p)
            ch.ops.append(op)
            ch.lines.append(f"never show on {cc.placement.url}")

        self._ad_group(grt, acct, ch, cr, camp_rn, self.w.rn(acct, "adGroups", -3), a.get("ad_group_name") or "Ad group 1",
                       a.get("ad_group_cpc_bid"), "ENABLED", targeting,
                       "targeting" if remarketing else (a.get("audience_mode") or "targeting"),
                       bool(a.get("optimized_targeting")), a.get("ad"))
        if strategy == "manual_cpc" and not a.get("ad_group_cpc_bid"):
            raise AdsError("Manual CPC needs ad_group_cpc_bid (the most to pay per click).")
        if strategy in ("maximize_conversions", "maximize_conversion_value"):
            ch.warnings.append("Conversion bidding needs working conversion tracking (see list_conversion_actions).")
        if not a.get("ad"):
            ch.warnings.append("No ad yet: add one with create_responsive_display_ad before turning the campaign on.")
        if remarketing:
            ch.warnings.append("Remarketing only reaches people already on the lists, so it usually spends less than the budget.")
        ch.warnings.append("The campaign is created PAUSED. Nothing spends until you turn it on with set_status.")
        return ch


def _campaign_args(**kw) -> dict[str, Any]:
    return kw


def register_display_tools(mcp: FastMCP, w: Writer, au: Audiences) -> Display:
    d = Display(w, au)

    @mcp.tool(annotations=PREVIEW)
    async def create_display_campaign(
        customer_id: CID,
        name: str,
        daily_budget: Annotated[float, Field(gt=0, description="Average daily budget in the account's currency.")],
        location_ids: Annotated[list[str] | None, Field(description="Location IDs from find_locations.")] = None,
        excluded_location_ids: list[str] | None = None,
        language_ids: Annotated[list[str] | None, Field(description="Default ['1000'] English.")] = None,
        bidding: Bidding = "maximize_conversions",
        max_cpc_limit: float | None = None,
        target_cpa: float | None = None,
        target_roas: float | None = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        ad_group_name: str | None = None,
        ad_group_cpc_bid: Annotated[float | None, Field(description="Max CPC (needed for manual_cpc).")] = None,
        targeting: Annotated[dict[str, Any] | None, Field(description=TARGETING_SHAPE)] = None,
        audience_mode: Annotated[Literal["targeting", "observation"], Field(description=(
            "targeting = only show ads to the audiences given; observation = show to everyone, report on them."))] = "targeting",
        optimized_targeting: Annotated[bool, Field(description="Let Google also find similar people beyond the targeting.")] = False,
        excluded_placements: Annotated[list[str] | None, Field(description="Websites/apps never to show on.")] = None,
        ad: Annotated[dict[str, Any] | None, Field(description=AD_SHAPE)] = None,
    ) -> dict[str, Any]:
        """Preview creating a Display campaign (image ads on websites, apps, YouTube and Gmail) in one step: budget,
        bidding, locations, languages, an ad group with targeting, and optionally a responsive display ad.
        Always created PAUSED."""
        args = _campaign_args(name=name, daily_budget=daily_budget, location_ids=location_ids,
                              excluded_location_ids=excluded_location_ids, language_ids=language_ids, bidding=bidding,
                              max_cpc_limit=max_cpc_limit, target_cpa=target_cpa, target_roas=target_roas,
                              end_date=end_date, ad_group_name=ad_group_name, ad_group_cpc_bid=ad_group_cpc_bid,
                              targeting=targeting, audience_mode=audience_mode, optimized_targeting=optimized_targeting,
                              excluded_placements=excluded_placements, ad=ad)
        return await w.prepared("create_display_campaign", customer_id, lambda g, a: d.new_campaign(g, a, args))

    @mcp.tool(annotations=PREVIEW)
    async def create_remarketing_campaign(
        customer_id: CID,
        name: str,
        daily_budget: Annotated[float, Field(gt=0)],
        user_list_ids: Annotated[list[str], Field(description="Remarketing list IDs (list_audiences / create_remarketing_list).")],
        location_ids: list[str] | None = None,
        language_ids: list[str] | None = None,
        bidding: Bidding = "maximize_conversions",
        max_cpc_limit: float | None = None,
        target_cpa: float | None = None,
        target_roas: float | None = None,
        end_date: str | None = None,
        ad_group_cpc_bid: float | None = None,
        excluded_placements: list[str] | None = None,
        ad: Annotated[dict[str, Any] | None, Field(description=AD_SHAPE)] = None,
    ) -> dict[str, Any]:
        """Preview creating a remarketing campaign: Display ads shown ONLY to people on the given remarketing lists
        (past website visitors). Always created PAUSED."""
        args = _campaign_args(name=name, daily_budget=daily_budget, user_list_ids=user_list_ids, location_ids=location_ids,
                              language_ids=language_ids, bidding=bidding, max_cpc_limit=max_cpc_limit,
                              target_cpa=target_cpa, target_roas=target_roas, end_date=end_date,
                              ad_group_name="Remarketing", ad_group_cpc_bid=ad_group_cpc_bid,
                              excluded_placements=excluded_placements, ad=ad)
        return await w.prepared("create_remarketing_campaign", customer_id,
                                lambda g, a: d.new_campaign(g, a, args, remarketing=True))

    @mcp.tool(annotations=PREVIEW)
    async def create_display_ad_group(
        customer_id: CID,
        campaign_id: str,
        name: str,
        cpc_bid: float | None = None,
        status: Literal["ENABLED", "PAUSED"] = "PAUSED",
        targeting: Annotated[dict[str, Any] | None, Field(description=TARGETING_SHAPE)] = None,
        audience_mode: Literal["targeting", "observation"] = "targeting",
        optimized_targeting: bool = False,
        ad: Annotated[dict[str, Any] | None, Field(description=AD_SHAPE)] = None,
    ) -> dict[str, Any]:
        """Preview adding an ad group with its own targeting (and optionally an ad) to a Display campaign."""
        args = dict(name=name, cpc_bid=cpc_bid, status=status, targeting=targeting, audience_mode=audience_mode,
                    optimized_targeting=optimized_targeting, ad=ad)
        return await w.prepared("create_display_ad_group", customer_id, lambda g, a: d.new_ad_group(g, a, campaign_id, args))

    @mcp.tool(annotations=PREVIEW)
    async def create_responsive_display_ad(
        customer_id: CID,
        ad_group_id: str,
        ad: Annotated[dict[str, Any], Field(description=AD_SHAPE)],
        status: Literal["ENABLED", "PAUSED"] = "ENABLED",
    ) -> dict[str, Any]:
        """Preview creating a responsive display ad (Google builds banners of every size from your images and text)."""
        return await w.prepared("create_responsive_display_ad", customer_id, lambda g, a: d.new_ad(g, a, ad_group_id, ad, status))

    @mcp.tool(annotations=PREVIEW)
    async def update_display_targeting(
        customer_id: CID,
        ad_group_id: str,
        add: Annotated[dict[str, Any] | None, Field(description="Targeting to add. " + TARGETING_SHAPE)] = None,
        exclude: Annotated[dict[str, Any] | None, Field(description="Targeting to exclude, same shape.")] = None,
        remove_criterion_ids: Annotated[list[str] | None, Field(description="Criterion IDs to stop targeting.")] = None,
        optimized_targeting: bool | None = None,
    ) -> dict[str, Any]:
        """Preview changing who a Display ad group shows to: audiences, keywords, placements (websites), topics,
        exclusions and optimized targeting."""
        return await w.prepared("update_display_targeting", customer_id, lambda g, a: d.update_targeting(
            g, a, ad_group_id, add, exclude, remove_criterion_ids, optimized_targeting))

    return d


__all__ = ["Display", "register_display_tools"]
