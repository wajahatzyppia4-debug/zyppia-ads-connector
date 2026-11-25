"""Phase 3 change tools: extensions (assets), ad schedules and bid adjustments.

They use the same preview -> apply_change flow as writes.py.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP

from .ads import Account, AdsError
from .writes import CID, MAX_ITEMS, PREVIEW, Change, Writer, _num

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
DAY_GROUPS = {"ALL": DAYS, "WEEKDAYS": DAYS[:5], "WEEKEND": DAYS[5:]}
DEVICE_IDS = {"DESKTOP": 30000, "MOBILE": 30001, "TABLET": 30002}  # Google's fixed device criterion IDs
SNIPPET_HEADERS = [
    "Amenities", "Brands", "Courses", "Degree programs", "Destinations", "Featured hotels", "Insurance coverage",
    "Models", "Neighborhoods", "Service catalog", "Shows", "Styles", "Types",
]

Level = Annotated[Literal["account", "campaign"], Field(description="Whole account, or one campaign (give campaign_id).")]


def _limit(text: str, n: int, what: str) -> str:
    t = " ".join(str(text or "").split())
    if not t:
        raise AdsError(f"Empty {what}.")
    if len(t) > n:
        raise AdsError(f"{what.capitalize()} too long ({len(t)}/{n} characters): '{t}'.")
    return t


def _modifier(pct: float, label: str, allow_exclude: bool = False) -> float:
    """Bid adjustment percent (+20 = bid 20% more, -100 = never show) -> Google's bid modifier."""
    if allow_exclude and pct == -100:
        return 0.0
    if not -90 <= pct <= 900:
        raise AdsError(f"The bid adjustment for {label} must be between -90% and +900%"
                       + (" (or -100% to stop showing there)" if allow_exclude else "") + ".")
    return round(1 + pct / 100, 2)


class Extras:
    def __init__(self, w: Writer):
        self.w = w

    def _campaign(self, grt: str, acct: Account, campaign_id: str) -> tuple[int, str]:
        cid = _num(campaign_id, "campaign ID")
        rows = self.w.rows(grt, acct, f"SELECT campaign.name FROM campaign WHERE campaign.id = {cid}")
        if not rows:
            raise AdsError(f"Campaign {cid} not found in {acct.name}.")
        return cid, rows[0]["campaign.name"]

    def _attach(self, grt: str, acct: Account, ch: Change, level: str, campaign_id: str | None,
                build_asset, field_type: str, count: int) -> None:
        """Create `count` assets with build_asset(asset, i) and link them to the account or a campaign."""
        e = self.w.t.enums
        if level == "campaign":
            if not campaign_id:
                raise AdsError("Give campaign_id, or use level 'account'.")
            cid, cname = self._campaign(grt, acct, campaign_id)
            where = f"campaign '{cname}'"
        else:
            where = "the whole account (every campaign)"
        for i in range(count):
            temp = self.w.rn(acct, "assets", -(i + 1))
            op = self.w.op()
            asset = op.asset_operation.create
            asset.resource_name = temp
            build_asset(asset, i)
            ch.ops.append(op)
            link = self.w.op()
            if level == "campaign":
                ca = link.campaign_asset_operation.create
                ca.campaign = self.w.rn(acct, "campaigns", cid)
            else:
                ca = link.customer_asset_operation.create
            ca.asset = temp
            ca.field_type = e.AssetFieldTypeEnum[field_type]
            ch.ops.append(link)
        ch.lines.append(f"added to {where}")

    def sitelinks(self, grt, acct, level, campaign_id, sitelinks: list[dict[str, Any]]) -> Change:
        if not 1 <= len(sitelinks) <= 20:
            raise AdsError("Give 1 to 20 sitelinks.")
        clean = []
        for s in sitelinks:
            url = str(s.get("url") or s.get("final_url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                raise AdsError(f"Sitelink '{s.get('text')}' needs a url starting with https://")
            d1, d2 = s.get("description1"), s.get("description2")
            if bool(d1) != bool(d2):
                raise AdsError("Sitelink descriptions come in pairs: give both description1 and description2, or neither.")
            clean.append({"text": _limit(s.get("text"), 25, "sitelink text"), "url": url,
                          "d1": _limit(d1, 35, "sitelink description") if d1 else None,
                          "d2": _limit(d2, 35, "sitelink description") if d2 else None})
        ch = Change(acct, f"Add {len(clean)} sitelink(s)")

        def build(asset, i):
            c = clean[i]
            asset.final_urls.append(c["url"])
            asset.sitelink_asset.link_text = c["text"]
            if c["d1"]:
                asset.sitelink_asset.description1 = c["d1"]
                asset.sitelink_asset.description2 = c["d2"]

        self._attach(grt, acct, ch, level, campaign_id, build, "SITELINK", len(clean))
        ch.lines[:0] = [f"sitelink '{c['text']}' -> {c['url']}" for c in clean]
        if len(clean) < 4:
            ch.warnings.append("Google usually shows sitelinks only when there are at least 2, and 4 or more works best.")
        return ch

    def callouts(self, grt, acct, level, campaign_id, texts: list[str]) -> Change:
        if not 1 <= len(texts) <= 20:
            raise AdsError("Give 1 to 20 callouts.")
        clean = [_limit(t, 25, "callout") for t in texts]
        ch = Change(acct, f"Add {len(clean)} callout(s)")
        self._attach(grt, acct, ch, level, campaign_id,
                     lambda asset, i: setattr(asset.callout_asset, "callout_text", clean[i]), "CALLOUT", len(clean))
        ch.lines[:0] = [f"callout '{c}'" for c in clean]
        return ch

    def call(self, grt, acct, level, campaign_id, phone_number: str, country_code: str) -> Change:
        phone = re.sub(r"[^0-9+() -]", "", phone_number or "").strip()
        if len(re.sub(r"\D", "", phone)) < 6:
            raise AdsError("Give a real phone number.")
        cc = (country_code or "").upper()
        if not re.fullmatch(r"[A-Z]{2}", cc):
            raise AdsError("country_code must be 2 letters, e.g. AE, AU, PK.")
        ch = Change(acct, "Add a call button")

        def build(asset, i):
            asset.call_asset.phone_number = phone
            asset.call_asset.country_code = cc

        self._attach(grt, acct, ch, level, campaign_id, build, "CALL", 1)
        ch.lines.insert(0, f"call {phone} ({cc})")
        ch.warnings.append("Google checks that the number belongs to the business; unverified numbers may not show.")
        return ch

    def snippet(self, grt, acct, level, campaign_id, header: str, values: list[str]) -> Change:
        match = [h for h in SNIPPET_HEADERS if h.lower() == (header or "").strip().lower()]
        if not match:
            raise AdsError("header must be one of: " + ", ".join(SNIPPET_HEADERS) + ".")
        if not 3 <= len(values) <= 10:
            raise AdsError("Give 3 to 10 values.")
        clean = [_limit(v, 25, "snippet value") for v in values]
        ch = Change(acct, f"Add a structured snippet ({match[0]})")

        def build(asset, i):
            asset.structured_snippet_asset.header = match[0]
            asset.structured_snippet_asset.values.extend(clean)

        self._attach(grt, acct, ch, level, campaign_id, build, "STRUCTURED_SNIPPET", 1)
        ch.lines.insert(0, f"{match[0]}: {', '.join(clean)}")
        return ch

    def schedule(self, grt, acct, campaign_id, slots: list[dict[str, Any]], replace_existing: bool) -> Change:
        e = self.w.t.enums
        cid, cname = self._campaign(grt, acct, campaign_id)
        if not slots or len(slots) > 42:
            raise AdsError("Give 1 to 42 time slots (Google allows 6 per day).")
        ch = Change(acct, f"Set the ad schedule of '{cname}'")
        if replace_existing:
            for r in self.w.rows(grt, acct, "SELECT campaign_criterion.resource_name FROM campaign_criterion "
                                            f"WHERE campaign.id = {cid} AND campaign_criterion.type = 'AD_SCHEDULE'"):
                op = self.w.op()
                op.campaign_criterion_operation.remove = r["campaign_criterion.resource_name"]
                ch.ops.append(op)
            if ch.ops:
                ch.lines.append(f"replace the {len(ch.ops)} existing time slot(s)")
        for s in slots:
            raw_days = s.get("days") or "ALL"
            raw_days = [raw_days] if isinstance(raw_days, str) else raw_days
            days: list[str] = []
            for d in raw_days:
                d = str(d).upper()
                days += DAY_GROUPS.get(d, [d])
            bad = [d for d in days if d not in DAYS]
            if bad:
                raise AdsError(f"Unknown day(s): {', '.join(bad)}. Use MONDAY..SUNDAY, WEEKDAYS, WEEKEND or ALL.")
            start, end = int(s.get("start_hour", 0)), int(s.get("end_hour", 24))
            if not 0 <= start < end <= 24:
                raise AdsError("Hours go from 0 to 24 and start_hour must be before end_hour.")
            pct = float(s.get("bid_adjustment_pct") or 0)
            for d in days:
                op = self.w.op()
                c = op.campaign_criterion_operation.create
                c.campaign = self.w.rn(acct, "campaigns", cid)
                c.ad_schedule.day_of_week = e.DayOfWeekEnum[d]
                c.ad_schedule.start_hour = start
                c.ad_schedule.end_hour = end
                c.ad_schedule.start_minute = e.MinuteOfHourEnum.ZERO
                c.ad_schedule.end_minute = e.MinuteOfHourEnum.ZERO
                if pct:
                    c.bid_modifier = _modifier(pct, f"{d} {start}:00")
                ch.ops.append(op)
            ch.lines.append(f"{', '.join(dd.title()[:3] for dd in days)} {start:02d}:00-{end:02d}:00"
                            + (f", bids {pct:+.0f}%" if pct else ""))
            if pct > self.w.settings.max_increase_pct:
                ch.large.append(f"bids go up {pct:.0f}% in some hours")
        ch.warnings.append(f"Times are in the account's time zone ({acct.time_zone or 'see list_accounts'}). "
                           "Ads only show inside these slots.")
        return ch

    def bid_adjustments(self, grt, acct, campaign_id, devices: dict[str, float] | None,
                        locations: dict[str, float] | None) -> Change:
        cid, cname = self._campaign(grt, acct, campaign_id)
        if not devices and not locations:
            raise AdsError("Give devices and/or locations adjustments.")
        e = self.w.t.enums
        ch = Change(acct, f"Change bid adjustments of '{cname}'")
        existing = self.w.rows(grt, acct, "SELECT campaign_criterion.resource_name, campaign_criterion.criterion_id, "
                                          "campaign_criterion.type, campaign_criterion.bid_modifier, campaign_criterion.negative "
                                          f"FROM campaign_criterion WHERE campaign.id = {cid} "
                                          "AND campaign_criterion.type IN ('DEVICE', 'LOCATION')")
        by_id = {(r["campaign_criterion.type"], str(r["campaign_criterion.criterion_id"])): r for r in existing}
        for name, pct in (devices or {}).items():
            dev = str(name).upper()
            if dev not in DEVICE_IDS:
                raise AdsError("Devices are DESKTOP, MOBILE and TABLET.")
            mod = _modifier(float(pct), dev.lower(), allow_exclude=True)
            r = by_id.get(("DEVICE", str(DEVICE_IDS[dev])))
            op = self.w.op()
            if r:
                c = op.campaign_criterion_operation.update
                c.resource_name = r["campaign_criterion.resource_name"]
                c.bid_modifier = mod
                op.campaign_criterion_operation.update_mask.paths.append("bid_modifier")
            else:
                c = op.campaign_criterion_operation.create
                c.campaign = self.w.rn(acct, "campaigns", cid)
                c.device.type_ = e.DeviceEnum[dev]
                c.bid_modifier = mod
            ch.ops.append(op)
            ch.lines.append(f"{dev.lower()}: " + ("don't show" if mod == 0 else f"bids {float(pct):+.0f}%"))
            if float(pct) > self.w.settings.max_increase_pct:
                ch.large.append(f"{dev.lower()} bids go up {float(pct):.0f}%")
        for loc, pct in (locations or {}).items():
            n = str(_num(loc, "location ID"))
            r = by_id.get(("LOCATION", n))
            if not r or r.get("campaign_criterion.negative"):
                raise AdsError(f"Location {n} isn't targeted by this campaign. Add it with update_campaign_targeting first.")
            op = self.w.op()
            c = op.campaign_criterion_operation.update
            c.resource_name = r["campaign_criterion.resource_name"]
            c.bid_modifier = _modifier(float(pct), f"location {n}")
            op.campaign_criterion_operation.update_mask.paths.append("bid_modifier")
            ch.ops.append(op)
            ch.lines.append(f"location {n}: bids {float(pct):+.0f}%")
            if float(pct) > self.w.settings.max_increase_pct:
                ch.large.append(f"bids for location {n} go up {float(pct):.0f}%")
        ch.warnings.append("Smart bidding (maximize conversions or value) ignores most bid adjustments except device -100%.")
        return ch


def register_extra_tools(mcp: FastMCP, w: Writer) -> Extras:
    x = Extras(w)

    @mcp.tool(annotations=PREVIEW)
    async def add_sitelinks(
        customer_id: CID,
        sitelinks: Annotated[list[dict[str, Any]], Field(description=(
            "List of {text (max 25), url, description1 and description2 (optional pair, max 35 each)}."))],
        level: Level = "campaign",
        campaign_id: str | None = None,
    ) -> dict[str, Any]:
        """Preview adding sitelinks (extra links under the ad) to a campaign or the whole account."""
        return await w.prepared("add_sitelinks", customer_id, lambda g, a: x.sitelinks(g, a, level, campaign_id, sitelinks))

    @mcp.tool(annotations=PREVIEW)
    async def add_callouts(
        customer_id: CID,
        callouts: Annotated[list[str], Field(description="Short phrases, max 25 characters, e.g. 'Free Hotel Pickup'.")],
        level: Level = "campaign",
        campaign_id: str | None = None,
    ) -> dict[str, Any]:
        """Preview adding callouts (short selling points shown with the ad)."""
        return await w.prepared("add_callouts", customer_id, lambda g, a: x.callouts(g, a, level, campaign_id, callouts))

    @mcp.tool(annotations=PREVIEW)
    async def add_call_button(
        customer_id: CID,
        phone_number: str,
        country_code: Annotated[str, Field(description="2 letters, e.g. AE, AU, PK.")],
        level: Level = "campaign",
        campaign_id: str | None = None,
    ) -> dict[str, Any]:
        """Preview adding a phone number / call button to ads."""
        return await w.prepared("add_call_button", customer_id,
                                lambda g, a: x.call(g, a, level, campaign_id, phone_number, country_code))

    @mcp.tool(annotations=PREVIEW)
    async def add_structured_snippet(
        customer_id: CID,
        header: Annotated[str, Field(description="One of: " + ", ".join(SNIPPET_HEADERS))],
        values: Annotated[list[str], Field(description="3 to 10 values, max 25 characters each.")],
        level: Level = "campaign",
        campaign_id: str | None = None,
    ) -> dict[str, Any]:
        """Preview adding a structured snippet, e.g. Types: Desert Safari, Dhow Cruise, City Tour."""
        return await w.prepared("add_structured_snippet", customer_id,
                                lambda g, a: x.snippet(g, a, level, campaign_id, header, values))

    @mcp.tool(annotations=PREVIEW)
    async def set_ad_schedule(
        customer_id: CID,
        campaign_id: str,
        slots: Annotated[list[dict[str, Any]], Field(description=(
            "List of {days: 'ALL' | 'WEEKDAYS' | 'WEEKEND' | ['MONDAY', ...], start_hour 0-23, end_hour 1-24, "
            "bid_adjustment_pct (optional, e.g. 20 or -30)}."))],
        replace_existing: Annotated[bool, Field(description="Remove the campaign's current schedule first.")] = True,
    ) -> dict[str, Any]:
        """Preview setting when a campaign's ads run (and optional bid changes by time of day)."""
        return await w.prepared("set_ad_schedule", customer_id,
                                lambda g, a: x.schedule(g, a, campaign_id, slots, replace_existing))

    @mcp.tool(annotations=PREVIEW)
    async def set_bid_adjustments(
        customer_id: CID,
        campaign_id: str,
        devices: Annotated[dict[str, float] | None, Field(description=(
            "{DESKTOP|MOBILE|TABLET: percent}, e.g. {'MOBILE': 20}. -100 stops ads on that device."))] = None,
        locations: Annotated[dict[str, float] | None, Field(description=(
            "{location_id: percent} for locations the campaign already targets."))] = None,
    ) -> dict[str, Any]:
        """Preview bidding more or less on devices or targeted locations."""
        return await w.prepared("set_bid_adjustments", customer_id,
                                lambda g, a: x.bid_adjustments(g, a, campaign_id, devices, locations))

    return x


__all__ = ["register_extra_tools", "Extras", "MAX_ITEMS"]
