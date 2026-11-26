"""Phase 5: audiences and remarketing.

- list_audiences / find_audience_segments read remarketing lists, interests
  (affinity and in-market), custom audiences and Display topics.
- create_remarketing_list makes a website-visitor list from page rules.
- update_audience_targeting adds audiences to Search or Display campaigns and
  ad groups, either to only show ads to them ("targeting"), to watch and bid
  on them ("observation"), or to exclude them.

Same preview -> apply_change flow as writes.py.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import Account, AdsError
from .assets import _modifier
from .media import text
from .writes import CID, MAX_ITEMS, PREVIEW, Change, Writer, _ids_clause, _num

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

MIN_DISPLAY = 100     # active people a list needs before Display (and Performance Max) ads use it
MIN_SEARCH = 1_000    # ... and before Search ads use it
MAX_LIFE_SPAN = 540

PAGE_MATCH = {"contains": "CONTAINS", "equals": "EQUALS", "starts_with": "STARTS_WITH", "ends_with": "ENDS_WITH",
              "not_contains": "NOT_CONTAINS"}
Mode = Literal["observation", "targeting", "exclude"]


def _page_rules(rules: list[Any] | None, what: str) -> list[tuple[str, str]]:
    out = []
    for r in rules or []:
        if isinstance(r, str):
            match, value = "contains", r
        else:
            match, value = str(r.get("match") or "contains").lower(), r.get("value") or r.get("url")
        if match not in PAGE_MATCH:
            raise AdsError(f"Page rule match must be one of: {', '.join(PAGE_MATCH)}.")
        value = str(value or "").strip()
        if not value:
            raise AdsError(f"Empty {what} page rule.")
        out.append((PAGE_MATCH[match], value))
    return out


def _dimension(value: Any, enums) -> str:
    if isinstance(value, int):
        return enums.TargetingDimensionEnum(value).name
    return str(value)


def restrictions(raw: Any, enums) -> list[tuple[str, bool]]:
    """targeting_setting.target_restrictions from a report row -> [(dimension, bid_only)]."""
    out = []
    for r in raw or []:
        if isinstance(r, dict):
            out.append((_dimension(r.get("targeting_dimension"), enums), bool(r.get("bid_only"))))
    return out


class Audiences:
    def __init__(self, w: Writer):
        self.w = w

    # ------------------------------------------------------------- reading

    def list(self, grt: str, acct: Account, include_closed: bool) -> dict[str, Any]:
        rows = self.w.rows(grt, acct, "SELECT user_list.id, user_list.name, user_list.type, user_list.membership_status, "
                                      "user_list.size_for_display, user_list.size_for_search, user_list.eligible_for_display, "
                                      "user_list.eligible_for_search, user_list.membership_life_span, user_list.description, "
                                      "user_list.read_only FROM user_list"
                           + ("" if include_closed else " WHERE user_list.membership_status = 'OPEN'"))
        lists = []
        for r in rows:
            disp, srch = r.get("user_list.size_for_display") or 0, r.get("user_list.size_for_search") or 0
            notes = []
            if disp < MIN_DISPLAY:
                notes.append(f"too small for Display and Performance Max until it has {MIN_DISPLAY}+ people")
            if srch < MIN_SEARCH:
                notes.append(f"too small for Search until it has {MIN_SEARCH:,}+ people")
            if r.get("user_list.membership_status") == "CLOSED":
                notes.append("closed: no new people are added")
            lists.append({
                "id": str(r["user_list.id"]), "name": r.get("user_list.name"), "type": r.get("user_list.type"),
                "status": r.get("user_list.membership_status"), "people_display": disp, "people_search": srch,
                "days_people_stay": r.get("user_list.membership_life_span"),
                "usable_on_display": bool(r.get("user_list.eligible_for_display")) and disp >= MIN_DISPLAY,
                "usable_on_search": bool(r.get("user_list.eligible_for_search")) and srch >= MIN_SEARCH,
                "description": r.get("user_list.description") or None, "notes": notes,
            })
        lists.sort(key=lambda d: d["people_display"], reverse=True)
        used = self.w.rows(grt, acct, "SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, "
                                      "ad_group_criterion.user_list.user_list, ad_group_criterion.negative FROM ad_group_criterion "
                                      "WHERE ad_group_criterion.type = 'USER_LIST' AND ad_group_criterion.status != 'REMOVED'")
        where: dict[str, list[str]] = {}
        for r in used:
            lid = str(r.get("ad_group_criterion.user_list.user_list") or "").rsplit("/", 1)[-1]
            where.setdefault(lid, []).append(("excluded from " if r.get("ad_group_criterion.negative") else "")
                                             + f"'{r.get('ad_group.name')}' in '{r.get('campaign.name')}'")
        for item in lists:
            if item["id"] in where:
                item["used_in"] = where[item["id"]][:10]
        return {"account": acct.name, "remarketing_lists": lists, "count": len(lists),
                "note": f"Lists need {MIN_DISPLAY}+ active people for Display and {MIN_SEARCH:,}+ for Search before ads use them."}

    def find(self, grt: str, acct: Account, query: str, kind: str, limit: int = 50) -> dict[str, Any]:
        q = (query or "").strip().lower()
        words = q.split()
        out: list[dict[str, Any]] = []
        if kind in ("interests", "in_market", "affinity"):
            rows = self.w.ads.search(grt, acct, "SELECT user_interest.user_interest_id, user_interest.name, "
                                                "user_interest.taxonomy_type FROM user_interest "
                                                "WHERE user_interest.taxonomy_type IN ('AFFINITY', 'IN_MARKET')", 10_000)[0]
            want = {"in_market": {"IN_MARKET"}, "affinity": {"AFFINITY"}}.get(kind, {"AFFINITY", "IN_MARKET"})
            for r in rows:
                name = r.get("user_interest.name") or ""
                if r.get("user_interest.taxonomy_type") in want and all(wd in name.lower() for wd in words):
                    out.append({"id": str(r["user_interest.user_interest_id"]), "name": name,
                                "type": "in-market (actively shopping)" if r.get("user_interest.taxonomy_type") == "IN_MARKET"
                                else "affinity (long-term interest)"})
            use = "interest_ids"
        elif kind == "topics":
            rows = self.w.ads.search(grt, acct, "SELECT topic_constant.id, topic_constant.path FROM topic_constant", 10_000)[0]
            for r in rows:
                path = " > ".join(r.get("topic_constant.path") or [])
                if path and all(wd in path.lower() for wd in words):
                    out.append({"id": str(r["topic_constant.id"]), "topic": path})
            use = "topic_ids"
        elif kind == "custom_audiences":
            rows = self.w.rows(grt, acct, "SELECT custom_audience.id, custom_audience.name, custom_audience.status "
                                          "FROM custom_audience WHERE custom_audience.status = 'ENABLED'")
            out = [{"id": str(r["custom_audience.id"]), "name": r.get("custom_audience.name")} for r in rows
                   if all(wd in (r.get("custom_audience.name") or "").lower() for wd in words)]
            use = "custom_audience_ids"
        else:
            raise AdsError("kind must be interests, in_market, affinity, topics or custom_audiences.")
        out.sort(key=lambda d: len(d.get("name") or d.get("topic") or ""))
        return {"query": query, "kind": kind, "results": out[:limit], "more": max(0, len(out) - limit),
                "use_as": use}

    # ------------------------------------------------------------- helpers shared with Display and PMax

    def segments(self, grt: str, acct: Account, user_list_ids=None, interest_ids=None, custom_audience_ids=None,
                 network: str | None = None, ch: Change | None = None) -> list[tuple[str, str, str]]:
        """[(kind, resource name, label)] for the given audiences, checking remarketing lists exist and are big enough."""
        out: list[tuple[str, str, str]] = []
        lids = [_num(i, "remarketing list ID") for i in user_list_ids or []]
        if lids:
            rows = self.w.rows(grt, acct, "SELECT user_list.id, user_list.name, user_list.size_for_display, "
                                          "user_list.size_for_search, user_list.eligible_for_search, user_list.membership_status "
                                          f"FROM user_list WHERE user_list.id IN {_ids_clause(lids)}")
            got = {r["user_list.id"]: r for r in rows}
            missing = [str(i) for i in lids if i not in got]
            if missing:
                raise AdsError(f"These remarketing lists aren't in {acct.name}: {', '.join(missing)}. See list_audiences.")
            for i in lids:
                r = got[i]
                out.append(("user_list", self.w.rn(acct, "userLists", i), f"remarketing list '{r['user_list.name']}'"))
                if ch is not None:
                    disp, srch = r.get("user_list.size_for_display") or 0, r.get("user_list.size_for_search") or 0
                    if network == "SEARCH" and (srch < MIN_SEARCH or not r.get("user_list.eligible_for_search")):
                        ch.warnings.append(f"'{r['user_list.name']}' has about {srch:,} people for Search; Search ads only "
                                           f"use lists with {MIN_SEARCH:,}+, so it does nothing until it grows.")
                    elif network != "SEARCH" and disp < MIN_DISPLAY:
                        ch.warnings.append(f"'{r['user_list.name']}' has about {disp:,} people; ads only use lists with "
                                           f"{MIN_DISPLAY}+, so it does nothing until it grows.")
                    if r.get("user_list.membership_status") == "CLOSED":
                        ch.warnings.append(f"'{r['user_list.name']}' is closed, so no new people join it.")
        for i in interest_ids or []:
            n = _num(i, "interest ID")
            out.append(("user_interest", self.w.rn(acct, "userInterests", n), f"interest {n}"))
        for i in custom_audience_ids or []:
            n = _num(i, "custom audience ID")
            out.append(("custom_audience", self.w.rn(acct, "customAudiences", n), f"custom audience {n}"))
        if len(out) > MAX_ITEMS:
            raise AdsError(f"Give at most {MAX_ITEMS} audiences.")
        return out

    @staticmethod
    def set_segment(criterion: Any, kind: str, rn: str) -> None:
        if kind == "user_list":
            criterion.user_list.user_list = rn
        elif kind == "user_interest":
            criterion.user_interest.user_interest_category = rn
        else:
            criterion.custom_audience.custom_audience = rn

    def restriction_update(self, target: Any, mask_paths: Any, current: list[tuple[str, bool]], bid_only: bool) -> bool:
        """Set the AUDIENCE targeting restriction, keeping the others. False if nothing changes."""
        if ("AUDIENCE", bid_only) in current:
            return False
        e = self.w.t.enums
        for dim, b in [(d, b) for d, b in current if d != "AUDIENCE"] + [("AUDIENCE", bid_only)]:
            tr = self.w.t.get_type("TargetRestriction")
            tr.targeting_dimension = e.TargetingDimensionEnum[dim]
            tr.bid_only = b
            target.targeting_setting.target_restrictions.append(tr)
        mask_paths.append("targeting_setting.target_restrictions")
        return True

    # ------------------------------------------------------------- changes

    def create_list(self, grt: str, acct: Account, name: str, include_pages, exclude_pages, membership_days: int,
                    prefill: bool, description: str | None) -> Change:
        e = self.w.t.enums
        name = text(name, 255, "list name")
        inc, exc = _page_rules(include_pages, "include"), _page_rules(exclude_pages, "exclude")
        if not inc:
            raise AdsError("Give at least one include_pages rule, e.g. the website address to collect every visitor.")
        if not 1 <= int(membership_days) <= MAX_LIFE_SPAN:
            raise AdsError(f"membership_days must be 1 to {MAX_LIFE_SPAN}.")
        clash = self.w.rows(grt, acct, "SELECT user_list.id FROM user_list WHERE user_list.name = '"
                            + name.replace("\\", "\\\\").replace("'", "\\'") + "'")
        if clash:
            raise AdsError(f"There's already a remarketing list called '{name}' in {acct.name}. Pick another name.")
        ch = Change(acct, f"Create remarketing list '{name}'")
        op = self.w.op()
        ul = op.user_list_operation.create
        ul.name = name
        if description:
            ul.description = text(description, 1000, "description")
        ul.membership_status = e.UserListMembershipStatusEnum.OPEN
        ul.membership_life_span = int(membership_days)
        rb = ul.rule_based_user_list
        rb.prepopulation_status = e.UserListPrepopulationStatusEnum.REQUESTED if prefill else \
            e.UserListPrepopulationStatusEnum.UNSPECIFIED
        flex = rb.flexible_rule_user_list
        flex.inclusive_rule_operator = e.UserListFlexibleRuleOperatorEnum.OR
        for rules, target in ((inc, flex.inclusive_operands), (exc, flex.exclusive_operands)):
            for operator, value in rules:
                item = self.w.t.get_type("UserListRuleItemInfo")
                item.name = "url__"
                item.string_rule_item.operator = e.UserListStringRuleItemOperatorEnum[operator]
                item.string_rule_item.value = value
                group = self.w.t.get_type("UserListRuleItemGroupInfo")
                group.rule_items.append(item)
                operand = self.w.t.get_type("FlexibleRuleOperandInfo")
                operand.rule.rule_item_groups.append(group)
                target.append(operand)
        ch.ops.append(op)
        nice = {v: k.replace("_", " ") for k, v in PAGE_MATCH.items()}
        ch.lines.append(f"remarketing list '{name}': people who visited a page whose address "
                        + " OR ".join(f"{nice[o]} '{v}'" for o, v in inc))
        if exc:
            ch.lines.append("but not people who visited a page whose address " + " OR ".join(f"{nice[o]} '{v}'" for o, v in exc))
        ch.lines.append(f"people stay on the list for {int(membership_days)} days after their last visit")
        ch.lines.append("start with people from the last 30 days" if prefill else "start empty (only new visitors)")
        ch.warnings.append("The website needs the Google tag (or Google Ads remarketing tag) for people to be added. "
                           "Check with gtm_workspace, or add one with gtm_tag.")
        ch.warnings.append(f"Ads only use the list once it has {MIN_DISPLAY}+ people (Display and Performance Max) or "
                           f"{MIN_SEARCH:,}+ (Search). Small sites can take weeks.")
        return ch

    def update_list(self, grt: str, acct: Account, list_id: str, name: str | None, membership_days: int | None,
                    status: str | None) -> Change:
        e = self.w.t.enums
        lid = _num(list_id, "remarketing list ID")
        rows = self.w.rows(grt, acct, "SELECT user_list.name, user_list.membership_life_span, user_list.membership_status, "
                                      f"user_list.read_only FROM user_list WHERE user_list.id = {lid}")
        if not rows:
            raise AdsError(f"Remarketing list {lid} not found in {acct.name}.")
        r = rows[0]
        if r.get("user_list.read_only"):
            raise AdsError(f"'{r['user_list.name']}' is made by Google or another product, so it can't be changed here.")
        op = self.w.op()
        ul = op.user_list_operation.update
        ul.resource_name = self.w.rn(acct, "userLists", lid)
        paths = op.user_list_operation.update_mask.paths
        ch = Change(acct, f"Change remarketing list '{r['user_list.name']}'")
        if name:
            ul.name = text(name, 255, "list name")
            paths.append("name")
            ch.lines.append(f"name: '{r['user_list.name']}' -> '{ul.name}'")
        if membership_days is not None:
            if not 1 <= int(membership_days) <= MAX_LIFE_SPAN:
                raise AdsError(f"membership_days must be 1 to {MAX_LIFE_SPAN}.")
            ul.membership_life_span = int(membership_days)
            paths.append("membership_life_span")
            ch.lines.append(f"days people stay: {r.get('user_list.membership_life_span')} -> {int(membership_days)}")
        if status:
            ul.membership_status = e.UserListMembershipStatusEnum[status]
            paths.append("membership_status")
            ch.lines.append(f"status: {r.get('user_list.membership_status')} -> {status}"
                            + (" (no new people are added)" if status == "CLOSED" else ""))
        if not paths:
            raise AdsError("Say what to change: name, membership_days or status.")
        ch.ops.append(op)
        return ch

    def targeting(self, grt: str, acct: Account, level: str, target_id: str, user_list_ids, interest_ids,
                  custom_audience_ids, mode: str, bid_adjustment_pct: float | None, remove_ids) -> Change:
        tid = _num(target_id, f"{level.replace('_', ' ')} ID")
        if level == "campaign":
            rows = self.w.rows(grt, acct, "SELECT campaign.name, campaign.advertising_channel_type, "
                                          f"campaign.targeting_setting.target_restrictions FROM campaign WHERE campaign.id = {tid}")
            if not rows:
                raise AdsError(f"Campaign {tid} not found in {acct.name}.")
            label, channel = f"campaign '{rows[0]['campaign.name']}'", rows[0].get("campaign.advertising_channel_type")
            current = restrictions(rows[0].get("campaign.targeting_setting.target_restrictions"), self.w.t.enums)
        else:
            rows = self.w.rows(grt, acct, "SELECT ad_group.name, campaign.name, campaign.advertising_channel_type, "
                                          f"ad_group.targeting_setting.target_restrictions FROM ad_group WHERE ad_group.id = {tid}")
            if not rows:
                raise AdsError(f"Ad group {tid} not found in {acct.name}.")
            label = f"ad group '{rows[0]['ad_group.name']}' (in '{rows[0]['campaign.name']}')"
            channel = rows[0].get("campaign.advertising_channel_type")
            current = restrictions(rows[0].get("ad_group.targeting_setting.target_restrictions"), self.w.t.enums)
        if channel == "PERFORMANCE_MAX":
            raise AdsError("Performance Max doesn't use audience targeting. Use set_pmax_signals to give it audience hints.")
        if channel not in ("SEARCH", "DISPLAY"):
            raise AdsError(f"This tool works on Search and Display campaigns; this one is {channel}.")
        ch = Change(acct, f"Change audiences of {label}")
        segs = self.segments(grt, acct, user_list_ids, interest_ids, custom_audience_ids, channel, ch)
        parent = self.w.rn(acct, "campaigns" if level == "campaign" else "adGroups", tid)
        modifier = None
        if bid_adjustment_pct:
            if mode == "exclude":
                raise AdsError("Excluded audiences can't have a bid adjustment.")
            modifier = _modifier(float(bid_adjustment_pct), "the audience")
            if channel == "DISPLAY" and level == "campaign":
                raise AdsError("On Display, give audience bid adjustments at ad group level.")
        for kind, rn, name in segs:
            op = self.w.op()
            c = (op.campaign_criterion_operation if level == "campaign" else op.ad_group_criterion_operation).create
            if level == "campaign":
                c.campaign = parent
            else:
                c.ad_group = parent
                c.status = self.w.t.enums.AdGroupCriterionStatusEnum.ENABLED
            self.set_segment(c, kind, rn)
            if mode == "exclude":
                c.negative = True
            elif modifier is not None:
                c.bid_modifier = modifier
            ch.ops.append(op)
            ch.lines.append(("exclude " if mode == "exclude" else "add ") + name
                            + (f", bids {float(bid_adjustment_pct):+.0f}%" if modifier is not None else ""))
            if bid_adjustment_pct and float(bid_adjustment_pct) > self.w.settings.max_increase_pct:
                ch.large.append(f"bids for {name} go up {float(bid_adjustment_pct):.0f}%")
        for rid in remove_ids or []:
            n = _num(rid, "audience criterion ID")
            op = self.w.op()
            if level == "campaign":
                op.campaign_criterion_operation.remove = self.w.rn(acct, "campaignCriteria", f"{tid}~{n}")
            else:
                op.ad_group_criterion_operation.remove = self.w.rn(acct, "adGroupCriteria", f"{tid}~{n}")
            ch.ops.append(op)
            ch.lines.append(f"stop using audience criterion {n}")
        if mode in ("observation", "targeting") and segs:
            op = self.w.op()
            inner = op.campaign_operation if level == "campaign" else op.ad_group_operation
            inner.update.resource_name = parent
            if self.restriction_update(inner.update, inner.update_mask.paths, current, mode == "observation"):
                ch.ops.append(op)
                if mode == "targeting":
                    ch.lines.append(f"{label} now ONLY shows ads to people in its audiences")
                    ch.warnings.append("Targeting mode limits who sees the ads, so traffic usually drops a lot. "
                                       "Observation keeps everyone and just reports (and can bid) on these people.")
                else:
                    ch.lines.append(f"{label} keeps showing ads to everyone and reports on these audiences (observation)")
        if not ch.ops:
            raise AdsError("Give audiences to add or criterion IDs to remove.")
        if remove_ids:
            ch.large.append("removing audiences can't be undone automatically")
        return ch


def register_audience_tools(mcp: FastMCP, w: Writer) -> Audiences:
    au = Audiences(w)

    @mcp.tool(annotations=READ_ONLY)
    async def list_audiences(
        customer_id: CID,
        include_closed: bool = False,
    ) -> dict[str, Any]:
        """List an account's remarketing lists (audience lists): how many people are in each for Display and Search,
        whether ads can use it yet, how long people stay on it, and which ad groups use it."""
        return await w.run("list_audiences", customer_id,
                           lambda user, grt: au.list(grt, w.ads.account(user, grt, customer_id), include_closed))

    @mcp.tool(annotations=READ_ONLY)
    async def find_audience_segments(
        customer_id: CID,
        query: Annotated[str, Field(description="Words to look for, e.g. 'travel', 'real estate', 'luxury'.")],
        kind: Annotated[Literal["interests", "in_market", "affinity", "topics", "custom_audiences"], Field(description=(
            "interests = affinity + in-market Google audiences; topics = Display website topics; "
            "custom_audiences = the account's own custom segments."))] = "interests",
    ) -> dict[str, Any]:
        """Find Google audience segments (people interested in or shopping for something), Display topics, or custom
        audiences by name, with the IDs the Display, remarketing and Performance Max tools need."""
        return await w.run("find_audience_segments", customer_id,
                           lambda user, grt: au.find(grt, w.ads.account(user, grt, customer_id), query, kind))

    @mcp.tool(annotations=PREVIEW)
    async def create_remarketing_list(
        customer_id: CID,
        name: str,
        include_pages: Annotated[list[Any], Field(description=(
            "Who joins: people who visited a page matching ANY rule. Each rule is {match: contains|equals|starts_with|"
            "ends_with, value} or just text (= contains). To collect every visitor, give the site's domain, e.g. 'example.com'."))],
        exclude_pages: Annotated[list[Any] | None, Field(description=(
            "Leave out people who visited these pages, e.g. [{'match': 'contains', 'value': '/thank-you'}] to skip buyers."))] = None,
        membership_days: Annotated[int, Field(description="How long people stay on the list after their last visit, 1-540.")] = 30,
        prefill: Annotated[bool, Field(description="Also add matching visitors from the last 30 days.")] = True,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Preview creating a website remarketing list (people who visited certain pages), to show them ads later
        with create_remarketing_campaign, update_audience_targeting or Performance Max signals. Needs the Google tag
        on the website."""
        return await w.prepared("create_remarketing_list", customer_id, lambda g, a: au.create_list(
            g, a, name, include_pages, exclude_pages, membership_days, prefill, description))

    @mcp.tool(annotations=PREVIEW)
    async def update_remarketing_list(
        customer_id: CID,
        list_id: str,
        name: str | None = None,
        membership_days: Annotated[int | None, Field(description="1-540")] = None,
        status: Annotated[Literal["OPEN", "CLOSED"] | None, Field(description="CLOSED stops adding new people.")] = None,
    ) -> dict[str, Any]:
        """Preview renaming a remarketing list, changing how long people stay on it, or closing / reopening it."""
        return await w.prepared("update_remarketing_list", customer_id,
                                lambda g, a: au.update_list(g, a, list_id, name, membership_days, status))

    @mcp.tool(annotations=PREVIEW)
    async def update_audience_targeting(
        customer_id: CID,
        level: Literal["campaign", "ad_group"],
        target_id: Annotated[str, Field(description="The campaign ID or ad group ID.")],
        user_list_ids: Annotated[list[str] | None, Field(description="Remarketing list IDs from list_audiences.")] = None,
        interest_ids: Annotated[list[str] | None, Field(description="Affinity / in-market IDs from find_audience_segments.")] = None,
        custom_audience_ids: list[str] | None = None,
        mode: Annotated[Mode, Field(description=(
            "observation: ads still show to everyone, these audiences are reported on and can get a bid adjustment "
            "(best for Search). targeting: ads ONLY show to these audiences (usual for remarketing). "
            "exclude: never show ads to these people (e.g. past buyers)."))] = "observation",
        bid_adjustment_pct: Annotated[float | None, Field(description="e.g. 30 to bid 30% more for these people.")] = None,
        remove_criterion_ids: Annotated[list[str] | None, Field(description=(
            "Audience criterion IDs to stop using (from run_gaql on ad_group_criterion / campaign_criterion)."))] = None,
    ) -> dict[str, Any]:
        """Preview adding audiences (remarketing lists, interests, custom audiences) to a Search or Display campaign
        or ad group: to watch and bid on them, to only target them, or to exclude them. For Performance Max use
        set_pmax_signals."""
        return await w.prepared("update_audience_targeting", customer_id, lambda g, a: au.targeting(
            g, a, level, target_id, user_list_ids, interest_ids, custom_audience_ids, mode, bid_adjustment_pct,
            remove_criterion_ids))

    return au


__all__ = ["Audiences", "register_audience_tools", "restrictions"]
