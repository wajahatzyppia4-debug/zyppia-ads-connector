"""Phase 5: Performance Max campaigns.

A Performance Max (PMax) campaign shows ads on Search, YouTube, Display,
Gmail, Discover and Maps from one set of "assets" (headlines, descriptions,
images, logos, videos). Assets live in asset groups. Signals (search themes
and audiences) are hints for Google about who to look for. With a linked
Merchant Center account, PMax also shows Shopping ads, and listing groups
choose which products each asset group advertises.

Same preview -> apply_change flow as writes.py; new campaigns are PAUSED.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import Account, AdsError
from .audiences import Audiences
from .media import Creative, existing_images, https_url, text, texts
from .writes import CID, MAX_ITEMS, PREVIEW, Change, Writer, _ids_clause, _num, money, to_micros

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

PmaxBidding = Literal["maximize_conversions", "maximize_conversion_value"]

# Asset group requirements: field -> (Google field type, min, max, limit or image kind)
TEXT_FIELDS = {
    "headlines": ("HEADLINE", 3, 15, 30),
    "long_headlines": ("LONG_HEADLINE", 1, 5, 90),
    "descriptions": ("DESCRIPTION", 2, 5, 90),
}
IMAGE_FIELDS = {
    "landscape_images": ("MARKETING_IMAGE", 1, 20, "landscape"),
    "square_images": ("SQUARE_MARKETING_IMAGE", 1, 20, "square"),
    "portrait_images": ("PORTRAIT_MARKETING_IMAGE", 0, 20, "portrait"),
    "logos": ("LOGO", 1, 5, "logo"),
    "landscape_logos": ("LANDSCAPE_LOGO", 0, 5, "landscape_logo"),
}
FIELD_NAMES = {**{v[0]: k for k, v in TEXT_FIELDS.items()}, **{v[0]: k for k, v in IMAGE_FIELDS.items()},
               "BUSINESS_NAME": "business_name", "YOUTUBE_VIDEO": "youtube_videos"}
MAX_IMAGES = 20

LISTING_DIMENSIONS = {
    "brand": "product_brand", "item_id": "product_item_id", "product_type": "product_type",
    **{f"custom_label_{i}": "product_custom_attribute" for i in range(5)},
}

ASSET_GROUP_SHAPE = (
    "{name, final_url, path1, path2 (max 15), business_name (max 25), headlines (3-15, max 30 chars), "
    "long_headlines (1-5, max 90), descriptions (2-5, max 90, at least one max 60), landscape_images (1+, 1.91:1 "
    "e.g. 1200x628), square_images (1+, 1:1 e.g. 1200x1200), portrait_images (optional, 4:5), logos (1-5, square), "
    "landscape_logos (optional, 4:1), youtube_videos (optional, up to 5 links or IDs), search_themes (optional, up "
    "to 25 phrases people search for), audience (optional {user_list_ids, interest_ids, custom_audience_ids})}. "
    "Images are public https addresses of JPEG/PNG/GIF files (max 5 MB) or image asset IDs already in the account."
)


class Pmax:
    def __init__(self, w: Writer, au: Audiences):
        self.w, self.au = w, au

    # ------------------------------------------------------------- asset groups

    def _check_spec(self, spec: dict[str, Any], need_all: bool, brand_at_campaign: bool) -> dict[str, Any]:
        """Clean and check an asset group's assets. need_all: enforce Google's minimums."""
        out: dict[str, Any] = {}
        for f, (_ft, lo, hi, limit) in TEXT_FIELDS.items():
            vals = spec.get(f) or []
            out[f] = texts(vals, limit, f[:-1].replace("_", " "), lo if need_all else 0, hi) if vals or need_all else []
        if out["descriptions"] and need_all and not any(len(d) <= 60 for d in out["descriptions"]):
            raise AdsError("At least one description must be 60 characters or fewer.")
        out["business_name"] = text(spec["business_name"], 25, "business name") if spec.get("business_name") else None
        if need_all and not out["business_name"] and not brand_at_campaign:
            raise AdsError("Give business_name (max 25 characters).")
        for f, (_ft, lo, hi, _kind) in IMAGE_FIELDS.items():
            vals = list(spec.get(f) or [])
            if f == "logos" and brand_at_campaign:
                lo = 0
            if need_all and len(vals) < lo:
                raise AdsError(f"Performance Max needs at least {lo} {f.replace('_', ' ')[:-1]}(s). " + ASSET_GROUP_SHAPE)
            if len(vals) > hi:
                raise AdsError(f"Give at most {hi} {f.replace('_', ' ')}.")
            out[f] = vals
        if sum(len(out[f]) for f in ("landscape_images", "square_images", "portrait_images")) > MAX_IMAGES:
            raise AdsError(f"Give at most {MAX_IMAGES} images in total.")
        out["youtube_videos"] = list(spec.get("youtube_videos") or [])
        if len(out["youtube_videos"]) > 5:
            raise AdsError("Give at most 5 videos.")
        return out

    def asset_links(self, grt: str, acct: Account, ch: Change, cr: Creative, ag_rn: str, clean: dict[str, Any],
                    brand_at_campaign: bool) -> None:
        """Create the assets and link them to the asset group."""
        e = self.w.t.enums
        have = existing_images(self.w, grt, acct, [x for f in IMAGE_FIELDS for x in clean[f]])
        links: list[tuple[str, str]] = []
        for f, (ft, *_rest) in TEXT_FIELDS.items():
            for v in clean[f]:
                links.append((cr.text_asset(v), ft))
            if clean[f]:
                ch.lines.append(f"  {f.replace('_', ' ')}: " + " | ".join(clean[f]))
        if clean.get("business_name"):
            if brand_at_campaign:
                ch.warnings.append("This campaign keeps its business name and logo at campaign level, so the "
                                   "business_name and logos given here were not added.")
            else:
                links.append((cr.text_asset(clean["business_name"]), "BUSINESS_NAME"))
                ch.lines.append(f"  business name: {clean['business_name']}")
        for f, (ft, _lo, _hi, kind) in IMAGE_FIELDS.items():
            if f == "logos" and brand_at_campaign:
                continue
            for ref in clean[f]:
                m = cr.image_asset(ref, kind, have)
                links.append((m.rn, ft))
                ch.lines.append(f"  {f.replace('_', ' ')[:-1]}: {m.label}")
        for v in clean["youtube_videos"]:
            m = cr.video_asset(v)
            links.append((m.rn, "YOUTUBE_VIDEO"))
            ch.lines.append(f"  video: {m.label}")
        for asset_rn, ft in links:
            op = self.w.op()
            link = op.asset_group_asset_operation.create
            link.asset_group = ag_rn
            link.asset = asset_rn
            link.field_type = e.AssetFieldTypeEnum[ft]
            ch.ops.append(op)

    def signal_ops(self, grt: str, acct: Account, ch: Change, cr: Creative, ag_rn: str, ag_name: str,
                   search_themes: list[str] | None, audience: dict[str, Any] | None) -> None:
        e = self.w.t.enums
        themes = [text(t, 80, "search theme") for t in search_themes or []]
        if len(themes) > 25:
            raise AdsError("Give at most 25 search themes per asset group.")
        for t in themes:
            op = self.w.op()
            s = op.asset_group_signal_operation.create
            s.asset_group = ag_rn
            s.search_theme.text = t
            ch.ops.append(op)
        if themes:
            ch.lines.append("  search themes: " + ", ".join(themes))
        if audience:
            unknown = set(audience) - {"user_list_ids", "interest_ids", "custom_audience_ids"}
            if unknown:
                raise AdsError("audience takes user_list_ids, interest_ids and custom_audience_ids.")
            segs = self.au.segments(grt, acct, audience.get("user_list_ids"), audience.get("interest_ids"),
                                    audience.get("custom_audience_ids"), "PERFORMANCE_MAX", ch)
            if segs:
                op = self.w.op()
                a = op.audience_operation.create
                a.resource_name = cr.temp("audiences")
                a.name = f"{ag_name[:150]} signal {int(time.time())}"
                a.scope = e.AudienceScopeEnum.ASSET_GROUP
                a.asset_group = ag_rn
                dim = self.w.t.get_type("AudienceDimension")
                for kind, rn, _label in segs:
                    seg = self.w.t.get_type("AudienceSegment")
                    if kind == "user_list":
                        seg.user_list.user_list = rn
                    elif kind == "user_interest":
                        seg.user_interest.user_interest_category = rn
                    else:
                        seg.custom_audience.custom_audience = rn
                    dim.audience_segments.segments.append(seg)
                a.dimensions.append(dim)
                ch.ops.append(op)
                op2 = self.w.op()
                s = op2.asset_group_signal_operation.create
                s.asset_group = ag_rn
                s.audience.audience = a.resource_name
                ch.ops.append(op2)
                ch.lines.append("  audience signal: " + ", ".join(label for _k, _r, label in segs))

    def listing_root(self, acct: Account, ch: Change, ag_rn: str, temp: int) -> None:
        e = self.w.t.enums
        op = self.w.op()
        f = op.asset_group_listing_group_filter_operation.create
        f.resource_name = self.w.rn(acct, "assetGroupListingGroupFilters", f"{ag_rn.rsplit('/', 1)[-1]}~{temp}")
        f.asset_group = ag_rn
        f.type_ = e.ListingGroupFilterTypeEnum.UNIT_INCLUDED
        f.listing_source = e.ListingGroupFilterListingSourceEnum.SHOPPING
        ch.ops.append(op)
        ch.lines.append("  products: all products from the Merchant Center feed (change with set_pmax_listing_groups)")

    def asset_group_ops(self, grt: str, acct: Account, ch: Change, cr: Creative, campaign_rn: str, ag_rn: str,
                        spec: dict[str, Any], shopping: bool, brand_at_campaign: bool, status: str) -> None:
        e = self.w.t.enums
        name = text(spec.get("name") or "Asset group 1", 255, "asset group name")
        url = https_url(spec.get("final_url"))
        any_assets = any(spec.get(f) for f in [*TEXT_FIELDS, *IMAGE_FIELDS, "youtube_videos", "business_name"])
        need_all = not shopping or any_assets
        clean = self._check_spec(spec, need_all, brand_at_campaign)
        op = self.w.op()
        g = op.asset_group_operation.create
        g.resource_name = ag_rn
        g.name = name
        g.campaign = campaign_rn
        g.final_urls.append(url)
        g.status = e.AssetGroupStatusEnum[status]
        for p in ("path1", "path2"):
            if spec.get(p):
                setattr(g, p, text(spec[p], 15, p))
        ch.ops.append(op)
        ch.lines.append(f"asset group '{name}' ({status}) to {url}")
        if any_assets:
            self.asset_links(grt, acct, ch, cr, ag_rn, clean, brand_at_campaign)
        else:
            ch.lines.append("  no assets: Shopping (product) ads only")
            ch.warnings.append("Without headlines, images and logos this asset group only shows Shopping ads from the "
                               "product feed. Add assets with update_asset_group to also show on YouTube, Display and Search.")
        if shopping:
            self.listing_root(acct, ch, ag_rn, -1)
        self.signal_ops(grt, acct, ch, cr, ag_rn, name, spec.get("search_themes"), spec.get("audience"))
        if not clean["youtube_videos"] and any_assets:
            ch.warnings.append("No video: Google may make an automatic video from the images. Adding your own is better.")

    # ------------------------------------------------------------- campaigns

    def new_campaign(self, grt: str, acct: Account, a: dict[str, Any]) -> Change:
        e = self.w.t.enums
        name = text(a.get("name"), 255, "campaign name")
        existing = self.w.rows(grt, acct, "SELECT campaign.id FROM campaign WHERE campaign.name = '"
                               + name.replace("\\", "\\\\").replace("'", "\\'") + "' AND campaign.status != 'REMOVED'")
        if existing:
            raise AdsError(f"There's already a campaign called '{name}' in {acct.name}. Pick another name.")
        merchant = a.get("merchant_center_id")
        shopping = bool(merchant)
        ch = Change(acct, f"Create Performance Max campaign '{name}' (PAUSED)")
        cr = Creative(self.w, acct, ch)
        budget_rn, camp_rn = self.w.rn(acct, "campaignBudgets", -1), self.w.rn(acct, "campaigns", -2)

        op = self.w.op()
        b = op.campaign_budget_operation.create
        b.resource_name = budget_rn
        b.name = f"{name} budget {int(time.time())}"
        b.amount_micros = to_micros(a["daily_budget"], "daily budget")
        b.delivery_method = e.BudgetDeliveryMethodEnum.STANDARD
        b.explicitly_shared = False  # Performance Max can't use shared budgets
        ch.ops.append(op)
        ch.lines.append(f"daily budget {money(b.amount_micros)} {acct.currency}")

        op = self.w.op()
        c = op.campaign_operation.create
        c.resource_name = camp_rn
        c.name = name
        c.status = e.CampaignStatusEnum.PAUSED
        c.advertising_channel_type = e.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
        c.campaign_budget = budget_rn
        c.brand_guidelines_enabled = False  # business name and logo live in the asset group
        c.geo_target_type_setting.positive_geo_target_type = e.PositiveGeoTargetTypeEnum.PRESENCE
        c.contains_eu_political_advertising = e.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        strategy = a.get("bidding") or "maximize_conversions"
        if strategy not in ("maximize_conversions", "maximize_conversion_value"):
            raise AdsError("Performance Max uses maximize_conversions or maximize_conversion_value bidding.")
        self.w._set_bidding(c, strategy, None, a.get("target_cpa"), a.get("target_roas"))
        expansion = self.w.t.get_type("Campaign").AssetAutomationSetting()
        expansion.asset_automation_type = e.AssetAutomationTypeEnum.FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION
        expansion.asset_automation_status = (e.AssetAutomationStatusEnum.OPTED_IN if a.get("final_url_expansion")
                                             else e.AssetAutomationStatusEnum.OPTED_OUT)
        c.asset_automation_settings.append(expansion)
        if shopping:
            c.shopping_setting.merchant_id = _num(merchant, "Merchant Center ID")
            if a.get("feed_label"):
                c.shopping_setting.feed_label = text(a["feed_label"], 20, "feed label")
        if a.get("end_date"):
            c.end_date_time = f"{date.fromisoformat(a['end_date']).isoformat()} 23:59:59"
        ch.ops.append(op)
        ch.lines.append(f"campaign '{name}', status PAUSED, Performance Max (Search, YouTube, Display, Gmail, Discover, Maps)")
        ch.lines.append("bidding: " + self.w._describe_bidding(acct, strategy, None, a.get("target_cpa"), a.get("target_roas")))
        ch.lines.append("other pages of the site (final URL expansion): " + ("allowed" if a.get("final_url_expansion")
                                                                             else "off, ads go to the given address"))
        if shopping:
            ch.lines.append(f"products from Merchant Center {c.shopping_setting.merchant_id}"
                            + (f" (feed label {a['feed_label']})" if a.get("feed_label") else ""))
            ch.warnings.append("The Merchant Center account must already be linked to this Google Ads account, "
                               "or Google rejects the campaign.")
        if a.get("end_date"):
            ch.lines.append(f"ends {a['end_date']}")
        self.w.geo_lang_ops(ch, camp_rn, a)

        spec = dict(a.get("asset_group") or {})
        spec.setdefault("name", "Asset group 1")
        self.asset_group_ops(grt, acct, ch, cr, camp_rn, self.w.rn(acct, "assetGroups", -3), spec, shopping, False,
                             "ENABLED")
        ch.warnings.append("Performance Max needs working conversion tracking (see list_conversion_actions) and usually "
                           "2 to 6 weeks to learn. Avoid big changes in that time.")
        ch.warnings.append("The campaign is created PAUSED. Nothing spends until you turn it on with set_status.")
        return ch

    def _pmax_campaign(self, grt: str, acct: Account, campaign_id: str) -> dict[str, Any]:
        cid = _num(campaign_id, "campaign ID")
        rows = self.w.rows(grt, acct, "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
                                      "campaign.shopping_setting.merchant_id, campaign.brand_guidelines_enabled "
                                      f"FROM campaign WHERE campaign.id = {cid}")
        if not rows:
            raise AdsError(f"Campaign {cid} not found in {acct.name}.")
        if rows[0].get("campaign.advertising_channel_type") != "PERFORMANCE_MAX":
            raise AdsError(f"'{rows[0]['campaign.name']}' isn't a Performance Max campaign.")
        return rows[0]

    def _asset_group(self, grt: str, acct: Account, asset_group_id: str) -> dict[str, Any]:
        agid = _num(asset_group_id, "asset group ID")
        rows = self.w.rows(grt, acct, "SELECT asset_group.id, asset_group.name, asset_group.status, asset_group.final_urls, "
                                      "campaign.id, campaign.name, campaign.status, campaign.shopping_setting.merchant_id, "
                                      "campaign.brand_guidelines_enabled FROM asset_group "
                                      f"WHERE asset_group.id = {agid}")
        if not rows:
            raise AdsError(f"Asset group {agid} not found in {acct.name}. See list_asset_groups.")
        return rows[0]

    def new_asset_group(self, grt: str, acct: Account, campaign_id: str, spec: dict[str, Any], status: str) -> Change:
        camp = self._pmax_campaign(grt, acct, campaign_id)
        ch = Change(acct, f"Create asset group '{spec.get('name')}' in '{camp['campaign.name']}'")
        cr = Creative(self.w, acct, ch)
        self.asset_group_ops(grt, acct, ch, cr, self.w.rn(acct, "campaigns", camp["campaign.id"]),
                             self.w.rn(acct, "assetGroups", -3), spec, bool(camp.get("campaign.shopping_setting.merchant_id")),
                             bool(camp.get("campaign.brand_guidelines_enabled")), status)
        if status == "ENABLED" and camp.get("campaign.status") == "ENABLED":
            ch.large.append("the new asset group is on, in a campaign that's on, so it can start spending money")
        return ch

    def update_asset_group(self, grt: str, acct: Account, asset_group_id: str, name: str | None, final_url: str | None,
                           status: str | None, add: dict[str, Any] | None, remove_asset_ids: list[str] | None) -> Change:
        e = self.w.t.enums
        ag = self._asset_group(grt, acct, asset_group_id)
        agid = ag["asset_group.id"]
        ag_rn = self.w.rn(acct, "assetGroups", agid)
        ch = Change(acct, f"Change asset group '{ag['asset_group.name']}' (in '{ag['campaign.name']}')")
        op = self.w.op()
        g = op.asset_group_operation.update
        g.resource_name = ag_rn
        paths = op.asset_group_operation.update_mask.paths
        if name:
            g.name = text(name, 255, "asset group name")
            paths.append("name")
            ch.lines.append(f"name: '{ag['asset_group.name']}' -> '{g.name}'")
        if final_url:
            g.final_urls.append(https_url(final_url))
            paths.append("final_urls")
            ch.lines.append(f"final URL: {', '.join(ag.get('asset_group.final_urls') or [])} -> {final_url}")
        if status:
            g.status = e.AssetGroupStatusEnum[status]
            paths.append("status")
            ch.lines.append(f"status: {ag.get('asset_group.status')} -> {status}")
            if status == "ENABLED" and ag.get("asset_group.status") != "ENABLED" and ag.get("campaign.status") == "ENABLED":
                ch.large.append("turning on the asset group can start spending money")
        if paths:
            ch.ops.append(op)
        current = self.w.rows(grt, acct, "SELECT asset_group_asset.resource_name, asset_group_asset.field_type, asset.id, "
                                         "asset.text_asset.text FROM asset_group_asset "
                                         f"WHERE asset_group.id = {agid} AND asset_group_asset.status != 'REMOVED'")
        counts: dict[str, int] = {}
        for r in current:
            counts[r["asset_group_asset.field_type"]] = counts.get(r["asset_group_asset.field_type"], 0) + 1
        if add:
            unknown = set(add) - {*TEXT_FIELDS, *IMAGE_FIELDS, "youtube_videos", "business_name"}
            if unknown:
                raise AdsError(f"Unknown asset fields: {', '.join(sorted(unknown))}. " + ASSET_GROUP_SHAPE)
            clean = self._check_spec(add, False, bool(ag.get("campaign.brand_guidelines_enabled")))
            for f, (ft, _lo, hi, *_r) in {**TEXT_FIELDS, **IMAGE_FIELDS}.items():
                if counts.get(ft, 0) + len(clean[f]) > hi:
                    raise AdsError(f"The asset group already has {counts.get(ft, 0)} {f.replace('_', ' ')}; the most "
                                   f"allowed is {hi}. Remove some first with remove_asset_ids.")
            if clean.get("business_name") and counts.get("BUSINESS_NAME"):
                raise AdsError("The asset group already has a business name. Remove it first with remove_asset_ids.")
            ch.lines.append("add:")
            self.asset_links(grt, acct, ch, Creative(self.w, acct, ch), ag_rn, clean,
                             bool(ag.get("campaign.brand_guidelines_enabled")))
        if remove_asset_ids:
            want = {str(_num(i, "asset ID")) for i in remove_asset_ids}
            found = [r for r in current if str(r["asset.id"]) in want]
            missing = want - {str(r["asset.id"]) for r in found}
            if missing:
                raise AdsError(f"These assets aren't in the asset group: {', '.join(sorted(missing))}. See list_asset_groups.")
            for r in found:
                op = self.w.op()
                op.asset_group_asset_operation.remove = r["asset_group_asset.resource_name"]
                ch.ops.append(op)
                ft = r["asset_group_asset.field_type"]
                counts[ft] -= 1
                ch.lines.append(f"remove {FIELD_NAMES.get(ft, ft.lower()).replace('_', ' ')} asset {r['asset.id']}"
                                + (f" '{r['asset.text_asset.text']}'" if r.get("asset.text_asset.text") else ""))
            for f, (ft, lo, *_r) in {**TEXT_FIELDS, **IMAGE_FIELDS}.items():
                if counts.get(ft, 0) < lo and any(r["asset_group_asset.field_type"] == ft for r in found):
                    added = len((add or {}).get(f) or [])
                    if counts.get(ft, 0) + added < lo:
                        raise AdsError(f"That would leave fewer than {lo} {f.replace('_', ' ')}, which Performance Max "
                                       "needs. Add new ones in the same change.")
            ch.large.append("removing assets can't be undone automatically (add them again if needed)")
        if not ch.ops:
            raise AdsError("Say what to change: name, final_url, status, add or remove_asset_ids.")
        ch.warnings.append("Asset changes reset some of Google's learning for this asset group.")
        return ch

    def signals(self, grt: str, acct: Account, asset_group_id: str, add_search_themes: list[str] | None,
                remove_search_themes: list[str] | None, audience: dict[str, Any] | None, remove_audience: bool) -> Change:
        ag = self._asset_group(grt, acct, asset_group_id)
        agid = ag["asset_group.id"]
        ag_rn = self.w.rn(acct, "assetGroups", agid)
        ch = Change(acct, f"Change signals of asset group '{ag['asset_group.name']}'")
        current = self.w.rows(grt, acct, "SELECT asset_group_signal.resource_name, asset_group_signal.search_theme.text, "
                                         "asset_group_signal.audience.audience FROM asset_group_signal "
                                         f"WHERE asset_group.id = {agid}")
        themes = {(r.get("asset_group_signal.search_theme.text") or "").lower(): r for r in current
                  if r.get("asset_group_signal.search_theme.text")}
        for t in remove_search_themes or []:
            r = themes.get(str(t).strip().lower())
            if not r:
                raise AdsError(f"'{t}' isn't a search theme of this asset group.")
            op = self.w.op()
            op.asset_group_signal_operation.remove = r["asset_group_signal.resource_name"]
            ch.ops.append(op)
            ch.lines.append(f"remove search theme '{t}'")
        new_themes = [t for t in add_search_themes or [] if str(t).strip().lower() not in themes]
        if len(themes) - len(remove_search_themes or []) + len(new_themes) > 25:
            raise AdsError("An asset group can have at most 25 search themes.")
        if audience or remove_audience:
            for r in current:
                if r.get("asset_group_signal.audience.audience"):
                    op = self.w.op()
                    op.asset_group_signal_operation.remove = r["asset_group_signal.resource_name"]
                    ch.ops.append(op)
                    ch.lines.append("remove the current audience signal" + (" (replaced below)" if audience else ""))
        before = len(ch.ops)
        self.signal_ops(grt, acct, ch, Creative(self.w, acct, ch), ag_rn, ag["asset_group.name"], new_themes, audience)
        if not ch.ops:
            raise AdsError("Nothing to change: give search themes or an audience to add or remove.")
        if before:
            ch.large.append("removed signals can't be put back automatically")
        ch.warnings.append("Signals are hints: Performance Max can still show ads to other people likely to convert.")
        return ch

    def listing_groups(self, grt: str, acct: Account, asset_group_id: str, dimension: str, values: list[str] | None,
                       mode: str) -> Change:
        e = self.w.t.enums
        ag = self._asset_group(grt, acct, asset_group_id)
        if not ag.get("campaign.shopping_setting.merchant_id"):
            raise AdsError("This Performance Max campaign has no Merchant Center feed, so it has no products to choose.")
        agid = ag["asset_group.id"]
        ag_rn = self.w.rn(acct, "assetGroups", agid)
        ch = Change(acct, f"Choose products for asset group '{ag['asset_group.name']}'")
        current = self.w.rows(grt, acct, "SELECT asset_group_listing_group_filter.resource_name, "
                                         "asset_group_listing_group_filter.id, "
                                         "asset_group_listing_group_filter.parent_listing_group_filter "
                                         f"FROM asset_group_listing_group_filter WHERE asset_group.id = {agid}")
        parent = {r["asset_group_listing_group_filter.resource_name"]:
                  r.get("asset_group_listing_group_filter.parent_listing_group_filter") for r in current}

        def depth(rn: str) -> int:
            d = 0
            while parent.get(rn):
                rn, d = parent[rn], d + 1
            return d

        for rn in sorted(parent, key=depth, reverse=True):  # children before parents
            op = self.w.op()
            op.asset_group_listing_group_filter_operation.remove = rn
            ch.ops.append(op)
        if current:
            ch.lines.append(f"replace the current product selection ({len(current)} listing group(s))")

        if dimension == "all":
            self.listing_root(acct, ch, ag_rn, -1)
        else:
            if dimension not in LISTING_DIMENSIONS:
                raise AdsError("dimension must be all, brand, item_id, product_type or custom_label_0..custom_label_4.")
            vals = [text(v, 1000, "value") for v in values or []]
            if not vals or len(vals) > MAX_ITEMS:
                raise AdsError(f"Give 1 to {MAX_ITEMS} values.")
            field = LISTING_DIMENSIONS[dimension]
            root = self.w.rn(acct, "assetGroupListingGroupFilters", f"{agid}~-1")
            op = self.w.op()
            f = op.asset_group_listing_group_filter_operation.create
            f.resource_name = root
            f.asset_group = ag_rn
            f.type_ = e.ListingGroupFilterTypeEnum.SUBDIVISION
            f.listing_source = e.ListingGroupFilterListingSourceEnum.SHOPPING
            ch.ops.append(op)
            yes, no = e.ListingGroupFilterTypeEnum.UNIT_INCLUDED, e.ListingGroupFilterTypeEnum.UNIT_EXCLUDED
            for i, v in enumerate([*vals, None]):
                op = self.w.op()
                f = op.asset_group_listing_group_filter_operation.create
                f.resource_name = self.w.rn(acct, "assetGroupListingGroupFilters", f"{agid}~{-2 - i}")
                f.asset_group = ag_rn
                f.parent_listing_group_filter = root
                f.listing_source = e.ListingGroupFilterListingSourceEnum.SHOPPING
                if v is None:  # "everything else"
                    f.type_ = no if mode == "include_only" else yes
                else:
                    f.type_ = yes if mode == "include_only" else no
                dim = getattr(f.case_value, field)
                getattr(f.case_value._pb, field).SetInParent()
                if v is not None:
                    dim.value = v
                if field == "product_type":
                    dim.level = e.ListingGroupFilterProductTypeLevelEnum.LEVEL1
                if field == "product_custom_attribute":
                    dim.index = e.ListingGroupFilterCustomAttributeIndexEnum[f"INDEX{dimension[-1]}"]
                ch.ops.append(op)
            what = dimension.replace("_", " ")
            if mode == "include_only":
                ch.lines.append(f"advertise ONLY products whose {what} is: " + ", ".join(vals))
            else:
                ch.lines.append(f"advertise all products EXCEPT those whose {what} is: " + ", ".join(vals))
            if field == "product_type":
                ch.warnings.append("Product types are matched at the top level (the first part before '>').")
        if current:
            ch.large.append("the old product selection is replaced and can't be put back automatically")
        ch.warnings.append("Values must match the product feed exactly (brand names, item IDs, labels).")
        return ch

    # ------------------------------------------------------------- reading

    def list(self, grt: str, acct: Account, campaign_id: str | None, days: int) -> dict[str, Any]:
        where = "asset_group.status != 'REMOVED'"
        if campaign_id:
            where += f" AND campaign.id = {_num(campaign_id, 'campaign ID')}"
        groups = self.w.rows(grt, acct, "SELECT campaign.id, campaign.name, campaign.status, asset_group.id, asset_group.name, "
                                        "asset_group.status, asset_group.ad_strength, asset_group.primary_status, "
                                        "asset_group.final_urls, campaign.shopping_setting.merchant_id, "
                                        f"campaign.brand_guidelines_enabled FROM asset_group WHERE {where}")
        if not groups:
            return {"account": acct.name, "asset_groups": [],
                    "note": "No Performance Max asset groups. Create a campaign with create_pmax_campaign."}
        ids = _ids_clause(g["asset_group.id"] for g in groups)
        assets = self.w.rows(grt, acct, "SELECT asset_group.id, asset_group_asset.field_type, asset_group_asset.status, "
                                        "asset_group_asset.primary_status, asset.id, asset.type, asset.text_asset.text, "
                                        "asset.youtube_video_asset.youtube_video_id FROM asset_group_asset "
                                        f"WHERE asset_group.id IN {ids} AND asset_group_asset.status != 'REMOVED'", 5000)
        signals = self.w.rows(grt, acct, "SELECT asset_group.id, asset_group_signal.search_theme.text, "
                                         "asset_group_signal.audience.audience FROM asset_group_signal "
                                         f"WHERE asset_group.id IN {ids}", 5000)
        listings = self.w.rows(grt, acct, "SELECT asset_group.id, asset_group_listing_group_filter.id "
                                          f"FROM asset_group_listing_group_filter WHERE asset_group.id IN {ids}", 5000)
        start = (date.today() - timedelta(days=days)).isoformat()
        try:
            perf = self.w.rows(grt, acct, "SELECT asset_group.id, metrics.impressions, metrics.clicks, metrics.cost_micros, "
                                          "metrics.conversions, metrics.conversions_value FROM asset_group "
                                          f"WHERE asset_group.id IN {ids} AND segments.date >= '{start}'")
        except Exception:  # noqa: BLE001 - performance is a nice-to-have here
            perf = []
        by_id: dict[int, dict[str, Any]] = {}
        for g in groups:
            by_id[g["asset_group.id"]] = {
                "campaign": f"{g['campaign.name']} ({g['campaign.id']}, {g.get('campaign.status')})",
                "asset_group_id": str(g["asset_group.id"]), "name": g["asset_group.name"],
                "status": g.get("asset_group.status"), "ad_strength": g.get("asset_group.ad_strength"),
                "serving": g.get("asset_group.primary_status"), "final_urls": g.get("asset_group.final_urls"),
                "shopping_feed": bool(g.get("campaign.shopping_setting.merchant_id")),
                "brand_at_campaign": bool(g.get("campaign.brand_guidelines_enabled")),
                "assets": {}, "search_themes": [], "audience_signals": 0, "listing_groups": 0,
            }
        for r in assets:
            item = by_id.get(r["asset_group.id"])
            if not item:
                continue
            ft = r.get("asset_group_asset.field_type")
            label = r.get("asset.text_asset.text") or r.get("asset.youtube_video_asset.youtube_video_id") or r.get("asset.type")
            item["assets"].setdefault(FIELD_NAMES.get(ft, str(ft).lower()), []).append(
                {"asset_id": str(r["asset.id"]), "value": label, "status": r.get("asset_group_asset.primary_status")})
        for r in signals:
            item = by_id.get(r["asset_group.id"])
            if item and r.get("asset_group_signal.search_theme.text"):
                item["search_themes"].append(r["asset_group_signal.search_theme.text"])
            elif item and r.get("asset_group_signal.audience.audience"):
                item["audience_signals"] += 1
        for r in listings:
            if r["asset_group.id"] in by_id:
                by_id[r["asset_group.id"]]["listing_groups"] += 1
        for r in perf:
            item = by_id.get(r["asset_group.id"])
            if item:
                p = item.setdefault("last_days", {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0.0, "value": 0.0})
                p["impressions"] += r.get("metrics.impressions") or 0
                p["clicks"] += r.get("metrics.clicks") or 0
                p["cost"] = round(p["cost"] + (r.get("metrics.cost") or 0), 2)
                p["conversions"] = round(p["conversions"] + (r.get("metrics.conversions") or 0), 2)
                p["value"] = round(p["value"] + (r.get("metrics.conversions_value") or 0), 2)
        for item in by_id.values():
            gaps = []
            have = {k: len(v) for k, v in item["assets"].items()}
            if item["assets"] or not item["shopping_feed"]:
                for f, (_ft, lo, hi, *_r) in {**TEXT_FIELDS, **IMAGE_FIELDS}.items():
                    if f == "logos" and item["brand_at_campaign"]:
                        continue
                    if have.get(f, 0) < lo:
                        gaps.append(f"needs at least {lo} {f.replace('_', ' ')} (has {have.get(f, 0)})")
                if not have.get("business_name") and not item["brand_at_campaign"]:
                    gaps.append("needs a business name")
                for f, best in (("headlines", 11), ("descriptions", 4), ("landscape_images", 4), ("square_images", 4),
                                ("youtube_videos", 1)):
                    if have.get(f, 0) < best:
                        gaps.append(f"add more {f.replace('_', ' ')} for better ad strength "
                                    f"(has {have.get(f, 0)}, aim for {best}+)")
            if not item["search_themes"] and not item["audience_signals"]:
                gaps.append("no signals: add search themes or an audience with set_pmax_signals")
            item["to_improve"] = gaps
        return {"account": acct.name, "currency": acct.currency, "days": days, "asset_groups": list(by_id.values())}


def register_pmax_tools(mcp: FastMCP, w: Writer, au: Audiences) -> Pmax:
    pm = Pmax(w, au)

    @mcp.tool(annotations=READ_ONLY)
    async def list_asset_groups(
        customer_id: CID,
        campaign_id: Annotated[str | None, Field(description="Only this Performance Max campaign.")] = None,
        days: Annotated[int, Field(ge=1, le=365, description="Days of performance to include.")] = 30,
    ) -> dict[str, Any]:
        """List Performance Max asset groups with ad strength, every asset (with IDs), search themes, audience
        signals, product listing groups, recent performance and what to improve."""
        return await w.run("list_asset_groups", customer_id,
                           lambda user, grt: pm.list(grt, w.ads.account(user, grt, customer_id), campaign_id, days))

    @mcp.tool(annotations=PREVIEW)
    async def create_pmax_campaign(
        customer_id: CID,
        name: str,
        daily_budget: Annotated[float, Field(gt=0, description="Average daily budget in the account's currency.")],
        asset_group: Annotated[dict[str, Any], Field(description="The first asset group: " + ASSET_GROUP_SHAPE)],
        location_ids: Annotated[list[str] | None, Field(description="Location IDs from find_locations.")] = None,
        excluded_location_ids: list[str] | None = None,
        language_ids: Annotated[list[str] | None, Field(description="Default ['1000'] English.")] = None,
        bidding: PmaxBidding = "maximize_conversions",
        target_cpa: Annotated[float | None, Field(description="maximize_conversions only: target cost per conversion.")] = None,
        target_roas: Annotated[float | None, Field(description="maximize_conversion_value only: e.g. 4.0 = 400%.")] = None,
        merchant_center_id: Annotated[str | None, Field(description=(
            "Merchant Center account ID, to also show Shopping ads for its products (must be linked to this account)."))] = None,
        feed_label: Annotated[str | None, Field(description="Only products with this feed label, e.g. 'AU'.")] = None,
        final_url_expansion: Annotated[bool, Field(description=(
            "Let Google send people to other pages of the site and write text from them."))] = False,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    ) -> dict[str, Any]:
        """Preview creating a Performance Max campaign (one campaign showing on Search, YouTube, Display, Gmail,
        Discover and Maps) with budget, bidding, locations, languages and a first asset group of headlines,
        descriptions, images, logos and videos, plus optional search themes and audience signals. With a Merchant
        Center ID it also advertises the products. Always created PAUSED."""
        args = dict(name=name, daily_budget=daily_budget, asset_group=asset_group, location_ids=location_ids,
                    excluded_location_ids=excluded_location_ids, language_ids=language_ids, bidding=bidding,
                    target_cpa=target_cpa, target_roas=target_roas, merchant_center_id=merchant_center_id,
                    feed_label=feed_label, final_url_expansion=final_url_expansion, end_date=end_date)
        return await w.prepared("create_pmax_campaign", customer_id, lambda g, a: pm.new_campaign(g, a, args))

    @mcp.tool(annotations=PREVIEW)
    async def create_asset_group(
        customer_id: CID,
        campaign_id: str,
        asset_group: Annotated[dict[str, Any], Field(description=ASSET_GROUP_SHAPE)],
        status: Literal["ENABLED", "PAUSED"] = "PAUSED",
    ) -> dict[str, Any]:
        """Preview adding an asset group (another set of ads, e.g. for a different service or product range) to a
        Performance Max campaign."""
        return await w.prepared("create_asset_group", customer_id,
                                lambda g, a: pm.new_asset_group(g, a, campaign_id, asset_group, status))

    @mcp.tool(annotations=PREVIEW)
    async def update_asset_group(
        customer_id: CID,
        asset_group_id: str,
        name: str | None = None,
        final_url: str | None = None,
        status: Literal["ENABLED", "PAUSED"] | None = None,
        add: Annotated[dict[str, Any] | None, Field(description=(
            "Assets to add, any of: headlines, long_headlines, descriptions, business_name, landscape_images, "
            "square_images, portrait_images, logos, landscape_logos, youtube_videos."))] = None,
        remove_asset_ids: Annotated[list[str] | None, Field(description="Asset IDs from list_asset_groups to take out.")] = None,
    ) -> dict[str, Any]:
        """Preview renaming, pausing or enabling a Performance Max asset group, changing its final URL, or adding and
        removing its headlines, descriptions, images, logos and videos (e.g. replacing ones rated 'Low')."""
        return await w.prepared("update_asset_group", customer_id, lambda g, a: pm.update_asset_group(
            g, a, asset_group_id, name, final_url, status, add, remove_asset_ids))

    @mcp.tool(annotations=PREVIEW)
    async def set_pmax_signals(
        customer_id: CID,
        asset_group_id: str,
        add_search_themes: Annotated[list[str] | None, Field(description="Phrases people search for, max 25 per group.")] = None,
        remove_search_themes: list[str] | None = None,
        audience: Annotated[dict[str, Any] | None, Field(description=(
            "Replace the audience signal with {user_list_ids, interest_ids, custom_audience_ids}."))] = None,
        remove_audience: bool = False,
    ) -> dict[str, Any]:
        """Preview changing a Performance Max asset group's signals: search themes and the audience signal
        (remarketing lists, interests, custom audiences) that tell Google who to look for."""
        return await w.prepared("set_pmax_signals", customer_id, lambda g, a: pm.signals(
            g, a, asset_group_id, add_search_themes, remove_search_themes, audience, remove_audience))

    @mcp.tool(annotations=PREVIEW)
    async def set_pmax_listing_groups(
        customer_id: CID,
        asset_group_id: str,
        dimension: Annotated[Literal["all", "brand", "item_id", "product_type", "custom_label_0", "custom_label_1",
                                     "custom_label_2", "custom_label_3", "custom_label_4"], Field(description=(
                                         "all = every product in the feed; otherwise choose products by this feed field."))],
        values: Annotated[list[str] | None, Field(description="Values of that field, exactly as in the product feed.")] = None,
        mode: Annotated[Literal["include_only", "exclude"], Field(description=(
            "include_only = advertise only these; exclude = advertise everything except these."))] = "include_only",
    ) -> dict[str, Any]:
        """Preview choosing which Merchant Center products a Performance Max asset group advertises (all products,
        only some brands / item IDs / product types / custom labels, or all except some). Replaces the current choice."""
        return await w.prepared("set_pmax_listing_groups", customer_id,
                                lambda g, a: pm.listing_groups(g, a, asset_group_id, dimension, values, mode))

    return pm


__all__ = ["Pmax", "register_pmax_tools"]
