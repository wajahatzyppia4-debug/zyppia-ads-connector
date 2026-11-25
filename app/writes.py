"""Phase 2: changes to Google Ads accounts, always as preview -> confirm -> apply.

Every change tool here only *prepares* a change. It builds the Google Ads
operations, asks Google to check them (validate_only, nothing is changed), and
returns a plain-language summary plus a sealed confirm token. Only
apply_change(token) makes the change, and only once per token.

Safety rules:
- New campaigns are always created PAUSED.
- Budget or bid increases above MAX_INCREASE_PCT, enabling anything (which
  can start spend), removals and raw API operations are flagged as "large":
  apply_change refuses them unless confirm_large_change is true.
- Removing campaigns or ad groups needs their exact names.
- Tokens expire after 15 minutes, belong to the person who made them, and
  work once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import Account, AdsError, AdsGateway, pretty_id
from .config import Settings
from .sealing import Sealer

audit = logging.getLogger("zyppia.audit")

CHANGE_TTL = 900
MAX_ITEMS = 200

PREVIEW = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
APPLY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

NEXT_STEP = (
    "Nothing has changed yet. Show the person these changes and warnings in plain words and ask "
    "them to confirm. Only after they say yes, call apply_change with confirm_token{large}."
)

MatchType = Literal["EXACT", "PHRASE", "BROAD"]
Entity = Literal["campaign", "ad_group", "ad", "keyword"]


# --------------------------------------------------------------------- helpers


def to_micros(amount: float, what: str = "amount") -> int:
    if amount is None or amount <= 0:
        raise AdsError(f"The {what} must be more than 0.")
    # Google wants money in millionths, in steps of 0.01.
    cents = (Decimal(str(amount)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(cents) * 10_000


def money(micros: int | None) -> str:
    return "not set" if not micros else f"{micros / 1_000_000:,.2f}"


def _num(value: Any, what: str = "ID") -> int:
    digits = re.sub(r"[^0-9]", "", str(value))
    if not digits:
        raise AdsError(f"'{value}' is not a valid {what}.")
    return int(digits)


def _pair(value: str, what: str) -> tuple[int, int]:
    """'adGroupId~id' (as shown in reports) -> (ad_group_id, id)."""
    parts = re.split(r"[~/,: ]+", str(value).strip())
    if len(parts) != 2:
        raise AdsError(
            f"For {what}s give 'adGroupId~{what}Id' (both numbers are in performance_report rows), got '{value}'."
        )
    return _num(parts[0], "ad group ID"), _num(parts[1], f"{what} ID")


def _ids_clause(ids) -> str:
    return "(" + ", ".join(str(i) for i in sorted(set(ids))) + ")"


def _pct(old: int | None, new: int) -> float | None:
    if not old:
        return None
    return (new - old) / old * 100


def _check_keyword(text: str) -> str:
    t = " ".join(str(text).split())
    for ch in "[]\"+":
        t = t.strip(ch)
    if not t:
        raise AdsError("Empty keyword.")
    if len(t) > 80 or len(t.split()) > 10:
        raise AdsError(f"Keyword too long (max 80 characters and 10 words): '{t}'.")
    return t


def check_rsa(headlines: list[str], descriptions: list[str], path1: str | None, path2: str | None) -> None:
    probs = []
    if not 3 <= len(headlines) <= 15:
        probs.append(f"need 3 to 15 headlines (got {len(headlines)})")
    if not 2 <= len(descriptions) <= 4:
        probs.append(f"need 2 to 4 descriptions (got {len(descriptions)})")
    probs += [f"headline too long ({len(h)}/30): '{h}'" for h in headlines if len(h) > 30]
    probs += [f"description too long ({len(d)}/90): '{d}'" for d in descriptions if len(d) > 90]
    probs += [f"path too long ({len(p)}/15): '{p}'" for p in (path1, path2) if p and len(p) > 15]
    if path2 and not path1:
        probs.append("path2 needs path1")
    if len({h.lower() for h in headlines}) != len(headlines):
        probs.append("headlines must all be different")
    if probs:
        raise AdsError("Ad text problems: " + "; ".join(probs) + ".")


@dataclass
class Change:
    acct: Account
    title: str
    ops: list[Any] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    large: list[str] = field(default_factory=list)  # reasons it needs the extra confirmation


class _Used:
    """Best-effort one-time use of confirm tokens (per server instance)."""

    def __init__(self):
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, token: str) -> bool:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock:
            for k in [k for k, exp in self._seen.items() if exp < now]:
                del self._seen[k]
            if key in self._seen:
                return False
            self._seen[key] = now + CHANGE_TTL + 60
            return True

    def release(self, token: str) -> None:
        with self._lock:
            self._seen.pop(hashlib.sha256(token.encode()).hexdigest(), None)


# --------------------------------------------------------------------- engine


class Writer:
    def __init__(self, settings: Settings, ads: AdsGateway, sealer: Sealer):
        self.settings = settings
        self.ads = ads
        self.sealer = sealer
        self.used = _Used()

    @property
    def t(self):
        return self.ads.types()

    def op(self) -> Any:
        return self.t.get_type("MutateOperation")

    def rows(self, grt: str, acct: Account, query: str, limit: int = 1000) -> list[dict[str, Any]]:
        return self.ads.search(grt, acct, query, limit)[0]

    def flag_increase(self, ch: Change, label: str, old: int | None, new: int) -> None:
        pct = _pct(old, new)
        if pct is not None and pct > self.settings.max_increase_pct:
            ch.large.append(f"{label} goes up {pct:.0f}% (more than the {self.settings.max_increase_pct:.0f}% limit)")

    def preview(self, user: str, grt: str, ch: Change) -> dict[str, Any]:
        if not ch.ops:
            raise AdsError("There's nothing to change.")
        self.ads.mutate(grt, ch.acct, ch.ops, validate_only=True)  # raises if Google would reject it
        MO = type(self.op())
        payload = {
            "u": user,
            "c": ch.acct.customer_id,
            "t": ch.title,
            "s": ch.lines,
            "l": ch.large,
            "o": [base64.b64encode(MO.serialize(o)).decode() for o in ch.ops],
        }
        token = self.sealer.seal("change", payload, ttl=CHANGE_TTL)
        audit.info(json.dumps({"event": "preview", "user": user, "customer_id": ch.acct.customer_id,
                               "title": ch.title, "operations": len(ch.ops), "large": ch.large}))
        out = {
            "status": "preview_ok (Google checked it, nothing changed yet)",
            "account": f"{ch.acct.name} ({pretty_id(ch.acct.customer_id)})",
            "currency": ch.acct.currency,
            "change": ch.title,
            "details": ch.lines,
            "warnings": ch.warnings,
            "needs_extra_confirmation": ch.large,
            "expires_in_minutes": CHANGE_TTL // 60,
            "confirm_token": token,
            "next_step": NEXT_STEP.format(
                large=" and confirm_large_change=true (the person must agree to the flagged items)" if ch.large else ""
            ),
        }
        return out

    def apply(self, user: str, grt: str, token: str, confirm_large_change: bool) -> dict[str, Any]:
        data = self.sealer.unseal("change", token.strip())
        if not data:
            raise AdsError("This confirm token is invalid or expired (they last 15 minutes). Preview the change again.")
        if data["u"] != user:
            raise AdsError("This change was prepared by someone else. Preview it again under your own login.")
        if data["l"] and not confirm_large_change:
            raise AdsError(
                "This change needs extra confirmation because: " + "; ".join(data["l"])
                + ". Ask the person to agree to that explicitly, then call apply_change again with confirm_large_change=true."
            )
        acct = self.ads.account(user, grt, data["c"])
        if not self.used.claim(token):
            raise AdsError("This change was already applied. Preview it again if you want to repeat it.")
        MO = type(self.op())
        ops = [MO.deserialize(base64.b64decode(o)) for o in data["o"]]
        try:
            resp = self.ads.mutate(grt, acct, ops, validate_only=False)
        except Exception:
            self.used.release(token)
            raise
        results = []
        for r in resp.mutate_operation_responses:
            which = r._pb.WhichOneof("response")
            if which:
                results.append(getattr(r, which).resource_name)
        audit.info(json.dumps({"event": "apply", "user": user, "customer_id": acct.customer_id,
                               "title": data["t"], "details": data["s"], "results": results}))
        return {
            "status": "done",
            "account": f"{acct.name} ({pretty_id(acct.customer_id)})",
            "change": data["t"],
            "details": data["s"],
            "changed": results,
        }

    # ------------------------------------------------------------- builders

    def rn(self, acct: Account, kind: str, ident: Any) -> str:
        return f"customers/{acct.customer_id}/{kind}/{ident}"

    def status_change(self, grt: str, acct: Account, entity: str, ids: list[str], status: str) -> Change:
        e = self.t.enums
        ch = Change(acct, f"Set {len(ids)} {entity.replace('_', ' ')}(s) to {status}")
        if not ids or len(ids) > MAX_ITEMS:
            raise AdsError(f"Give 1 to {MAX_ITEMS} IDs.")
        found = self._lookup(grt, acct, entity, ids)
        for key, info in found.items():
            op = self.op()
            if entity == "campaign":
                obj = op.campaign_operation.update
                obj.resource_name = self.rn(acct, "campaigns", key)
                obj.status = e.CampaignStatusEnum[status]
                op.campaign_operation.update_mask.paths.append("status")
            elif entity == "ad_group":
                obj = op.ad_group_operation.update
                obj.resource_name = self.rn(acct, "adGroups", key)
                obj.status = e.AdGroupStatusEnum[status]
                op.ad_group_operation.update_mask.paths.append("status")
            elif entity == "ad":
                obj = op.ad_group_ad_operation.update
                obj.resource_name = self.rn(acct, "adGroupAds", key)
                obj.status = e.AdGroupAdStatusEnum[status]
                op.ad_group_ad_operation.update_mask.paths.append("status")
            else:
                obj = op.ad_group_criterion_operation.update
                obj.resource_name = self.rn(acct, "adGroupCriteria", key)
                obj.status = e.AdGroupCriterionStatusEnum[status]
                op.ad_group_criterion_operation.update_mask.paths.append("status")
            ch.ops.append(op)
            ch.lines.append(f"{info['label']}: {info['status']} -> {status}")
            if status == "ENABLED" and info["status"] != "ENABLED":
                ch.large.append(f"turning on {info['label']} can start spending money")
        return ch

    def _lookup(self, grt: str, acct: Account, entity: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Find the named things, return {resource key: {label, status, name}}. Errors if any are missing."""
        out: dict[str, dict[str, Any]] = {}
        if entity == "campaign":
            nums = [_num(i, "campaign ID") for i in ids]
            for r in self.rows(grt, acct, "SELECT campaign.id, campaign.name, campaign.status FROM campaign "
                                          f"WHERE campaign.id IN {_ids_clause(nums)}"):
                out[str(r["campaign.id"])] = {"label": f"campaign '{r['campaign.name']}'",
                                              "name": r["campaign.name"], "status": r["campaign.status"]}
            want = {str(n) for n in nums}
        elif entity == "ad_group":
            nums = [_num(i, "ad group ID") for i in ids]
            for r in self.rows(grt, acct, "SELECT ad_group.id, ad_group.name, ad_group.status, campaign.name FROM ad_group "
                                          f"WHERE ad_group.id IN {_ids_clause(nums)}"):
                out[str(r["ad_group.id"])] = {"label": f"ad group '{r['ad_group.name']}' (in '{r['campaign.name']}')",
                                              "name": r["ad_group.name"], "status": r["ad_group.status"]}
            want = {str(n) for n in nums}
        elif entity == "ad":
            pairs = [_pair(i, "ad") for i in ids]
            for r in self.rows(grt, acct, "SELECT ad_group.id, ad_group.name, ad_group_ad.ad.id, ad_group_ad.status "
                                          f"FROM ad_group_ad WHERE ad_group_ad.ad.id IN {_ids_clause(p[1] for p in pairs)}"):
                key = f"{r['ad_group.id']}~{r['ad_group_ad.ad.id']}"
                out[key] = {"label": f"ad {r['ad_group_ad.ad.id']} in ad group '{r['ad_group.name']}'",
                            "name": str(r["ad_group_ad.ad.id"]), "status": r["ad_group_ad.status"]}
            want = {f"{a}~{b}" for a, b in pairs}
        else:
            pairs = [_pair(i, "keyword") for i in ids]
            for r in self.rows(grt, acct, "SELECT ad_group.id, ad_group.name, ad_group_criterion.criterion_id, "
                                          "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, "
                                          "ad_group_criterion.status, ad_group_criterion.negative FROM ad_group_criterion "
                                          f"WHERE ad_group_criterion.criterion_id IN {_ids_clause(p[1] for p in pairs)}"):
                key = f"{r['ad_group.id']}~{r['ad_group_criterion.criterion_id']}"
                neg = "negative " if r.get("ad_group_criterion.negative") else ""
                text = r.get("ad_group_criterion.keyword.text") or "(not a keyword)"
                out[key] = {"label": f"{neg}keyword '{text}' [{r.get('ad_group_criterion.keyword.match_type')}] "
                                     f"in '{r['ad_group.name']}'",
                            "name": text, "status": r["ad_group_criterion.status"]}
            want = {f"{a}~{b}" for a, b in pairs}
        missing = want - set(out)
        if missing:
            raise AdsError(f"Couldn't find these {entity.replace('_', ' ')} IDs in {acct.name}: {', '.join(sorted(missing))}.")
        return {k: v for k, v in out.items() if k in want}

    def removal(self, grt: str, acct: Account, entity: str, ids: list[str], confirm_names: list[str] | None) -> Change:
        if not ids or len(ids) > MAX_ITEMS:
            raise AdsError(f"Give 1 to {MAX_ITEMS} IDs.")
        found = self._lookup(grt, acct, entity, ids)
        if entity in ("campaign", "ad_group"):
            names = sorted(v["name"].strip().lower() for v in found.values())
            given = sorted(n.strip().lower() for n in (confirm_names or []))
            if names != given:
                raise AdsError(
                    f"To remove a {entity.replace('_', ' ')}, the person must type its exact name. "
                    f"Ask them to confirm these names and pass them as confirm_names: "
                    + ", ".join(f"'{v['name']}'" for v in found.values())
                )
        kind = {"campaign": "campaigns", "ad_group": "adGroups", "ad": "adGroupAds", "keyword": "adGroupCriteria"}[entity]
        field_name = {"campaign": "campaign_operation", "ad_group": "ad_group_operation",
                      "ad": "ad_group_ad_operation", "keyword": "ad_group_criterion_operation"}[entity]
        ch = Change(acct, f"Remove {len(found)} {entity.replace('_', ' ')}(s)")
        for key, info in found.items():
            op = self.op()
            getattr(op, field_name).remove = self.rn(acct, kind, key)
            ch.ops.append(op)
            ch.lines.append(f"remove {info['label']}")
        ch.large.append("removing can't be undone (removed items can't be turned back on)")
        return ch

    def budget(self, grt: str, acct: Account, campaign_id: str, daily_amount: float) -> Change:
        cid = _num(campaign_id, "campaign ID")
        rows = self.rows(grt, acct, "SELECT campaign.name, campaign_budget.resource_name, campaign_budget.amount_micros, "
                                    "campaign_budget.explicitly_shared, campaign_budget.reference_count FROM campaign "
                                    f"WHERE campaign.id = {cid}")
        if not rows:
            raise AdsError(f"Campaign {cid} not found in {acct.name}.")
        r = rows[0]
        new = to_micros(daily_amount, "daily budget")
        old = r.get("campaign_budget.amount_micros")
        ch = Change(acct, f"Change daily budget of '{r['campaign.name']}'")
        op = self.op()
        b = op.campaign_budget_operation.update
        b.resource_name = r["campaign_budget.resource_name"]
        b.amount_micros = new
        op.campaign_budget_operation.update_mask.paths.append("amount_micros")
        ch.ops.append(op)
        ch.lines.append(f"daily budget: {money(old)} -> {money(new)} {acct.currency}")
        if r.get("campaign_budget.explicitly_shared") and (r.get("campaign_budget.reference_count") or 0) > 1:
            ch.warnings.append(f"This budget is shared by {r['campaign_budget.reference_count']} campaigns; all of them change.")
        ch.warnings.append("Google can spend up to 2x the daily budget on a single day; the monthly cap is about 30.4x daily.")
        self.flag_increase(ch, "the budget", old, new)
        return ch

    def bids(self, grt: str, acct: Account, level: str, items: list[dict[str, Any]]) -> Change:
        if not items or len(items) > MAX_ITEMS:
            raise AdsError(f"Give 1 to {MAX_ITEMS} bids.")
        ch = Change(acct, f"Change {len(items)} {level.replace('_', ' ')} max CPC bid(s)")
        if level == "ad_group":
            nums = {_num(i["id"], "ad group ID"): to_micros(i["cpc_bid"], "bid") for i in items}
            rows = self.rows(grt, acct, "SELECT ad_group.id, ad_group.name, ad_group.cpc_bid_micros, campaign.bidding_strategy_type "
                                        f"FROM ad_group WHERE ad_group.id IN {_ids_clause(nums)}")
            got = {r["ad_group.id"]: r for r in rows}
            for agid, new in nums.items():
                r = got.get(agid)
                if not r:
                    raise AdsError(f"Ad group {agid} not found in {acct.name}.")
                op = self.op()
                o = op.ad_group_operation.update
                o.resource_name = self.rn(acct, "adGroups", agid)
                o.cpc_bid_micros = new
                op.ad_group_operation.update_mask.paths.append("cpc_bid_micros")
                ch.ops.append(op)
                old = r.get("ad_group.cpc_bid_micros")
                ch.lines.append(f"ad group '{r['ad_group.name']}': {money(old)} -> {money(new)} {acct.currency}")
                self.flag_increase(ch, f"the bid for '{r['ad_group.name']}'", old, new)
                self._bid_note(ch, r.get("campaign.bidding_strategy_type"))
        else:
            pairs = {_pair(i["id"], "keyword"): to_micros(i["cpc_bid"], "bid") for i in items}
            rows = self.rows(grt, acct, "SELECT ad_group.id, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
                                        "ad_group_criterion.effective_cpc_bid_micros, campaign.bidding_strategy_type "
                                        "FROM ad_group_criterion WHERE ad_group_criterion.criterion_id IN "
                                        f"{_ids_clause(p[1] for p in pairs)}")
            got = {(r["ad_group.id"], r["ad_group_criterion.criterion_id"]): r for r in rows}
            for (agid, kid), new in pairs.items():
                r = got.get((agid, kid))
                if not r:
                    raise AdsError(f"Keyword {agid}~{kid} not found in {acct.name}.")
                op = self.op()
                o = op.ad_group_criterion_operation.update
                o.resource_name = self.rn(acct, "adGroupCriteria", f"{agid}~{kid}")
                o.cpc_bid_micros = new
                op.ad_group_criterion_operation.update_mask.paths.append("cpc_bid_micros")
                ch.ops.append(op)
                old = r.get("ad_group_criterion.effective_cpc_bid_micros")
                label = f"keyword '{r.get('ad_group_criterion.keyword.text')}'"
                ch.lines.append(f"{label}: {money(old)} -> {money(new)} {acct.currency}")
                self.flag_increase(ch, f"the bid for {label}", old, new)
                self._bid_note(ch, r.get("campaign.bidding_strategy_type"))
        return ch

    @staticmethod
    def _bid_note(ch: Change, strategy: str | None) -> None:
        if strategy and strategy not in ("MANUAL_CPC", "UNKNOWN", None):
            note = f"The campaign uses {strategy} bidding, so Google ignores manual CPC bids until it's switched to manual CPC."
            if note not in ch.warnings:
                ch.warnings.append(note)

    def bidding(self, grt: str, acct: Account, campaign_id: str, strategy: str, max_cpc_limit: float | None,
                target_cpa: float | None, target_roas: float | None) -> Change:
        cid = _num(campaign_id, "campaign ID")
        rows = self.rows(grt, acct, "SELECT campaign.name, campaign.bidding_strategy_type FROM campaign "
                                    f"WHERE campaign.id = {cid}")
        if not rows:
            raise AdsError(f"Campaign {cid} not found in {acct.name}.")
        r = rows[0]
        op = self.op()
        c = op.campaign_operation.update
        c.resource_name = self.rn(acct, "campaigns", cid)
        self._set_bidding(c, strategy, max_cpc_limit, target_cpa, target_roas)
        op.campaign_operation.update_mask.paths.append(_BIDDING_FIELD[strategy])
        ch = Change(acct, f"Change bidding of '{r['campaign.name']}'")
        ch.ops.append(op)
        ch.lines.append(f"bidding: {r.get('campaign.bidding_strategy_type')} -> {self._describe_bidding(acct, strategy, max_cpc_limit, target_cpa, target_roas)}")
        ch.warnings.append("A new bid strategy usually needs 1 to 2 weeks of learning, and results can swing meanwhile.")
        return ch

    def _set_bidding(self, campaign: Any, strategy: str, max_cpc_limit, target_cpa, target_roas) -> None:
        t = self.t
        if strategy == "manual_cpc":
            t.copy_from(campaign.manual_cpc, t.get_type("ManualCpc"))
        elif strategy == "maximize_clicks":
            ts = t.get_type("TargetSpend")
            if max_cpc_limit:
                ts.cpc_bid_ceiling_micros = to_micros(max_cpc_limit, "max CPC limit")
            t.copy_from(campaign.target_spend, ts)
        elif strategy == "maximize_conversions":
            mc = t.get_type("MaximizeConversions")
            if target_cpa:
                mc.target_cpa_micros = to_micros(target_cpa, "target CPA")
            t.copy_from(campaign.maximize_conversions, mc)
        elif strategy == "maximize_conversion_value":
            mv = t.get_type("MaximizeConversionValue")
            if target_roas:
                mv.target_roas = target_roas
            t.copy_from(campaign.maximize_conversion_value, mv)
        else:
            raise AdsError(f"Unknown bidding strategy '{strategy}'.")

    @staticmethod
    def _describe_bidding(acct, strategy, max_cpc_limit, target_cpa, target_roas) -> str:
        s = strategy.replace("_", " ")
        if strategy == "maximize_clicks" and max_cpc_limit:
            s += f" (max CPC {max_cpc_limit:.2f} {acct.currency})"
        if strategy == "maximize_conversions" and target_cpa:
            s += f" (target CPA {target_cpa:.2f} {acct.currency})"
        if strategy == "maximize_conversion_value" and target_roas:
            s += f" (target ROAS {target_roas:.0%})"
        return s

    def keywords(self, grt: str, acct: Account, ad_group_id: str, keywords: list[dict[str, Any]]) -> Change:
        agid = _num(ad_group_id, "ad group ID")
        rows = self.rows(grt, acct, f"SELECT ad_group.name, campaign.name FROM ad_group WHERE ad_group.id = {agid}")
        if not rows:
            raise AdsError(f"Ad group {agid} not found in {acct.name}.")
        ag = rows[0]
        if not keywords or len(keywords) > MAX_ITEMS:
            raise AdsError(f"Give 1 to {MAX_ITEMS} keywords.")
        ch = Change(acct, f"Add {len(keywords)} keyword(s) to '{ag['ad_group.name']}'")
        for k in keywords:
            self._keyword_op(ch, acct, self.rn(acct, "adGroups", agid), k)
        ch.warnings.append(f"New keywords start running right away if campaign '{ag['campaign.name']}' and the ad group are on.")
        return ch

    def _keyword_op(self, ch: Change, acct: Account, ad_group_rn: str, k: dict[str, Any]) -> None:
        e = self.t.enums
        text = _check_keyword(k.get("text", ""))
        match = str(k.get("match_type") or "PHRASE").upper()
        op = self.op()
        c = op.ad_group_criterion_operation.create
        c.ad_group = ad_group_rn
        c.status = e.AdGroupCriterionStatusEnum.ENABLED
        c.keyword.text = text
        c.keyword.match_type = e.KeywordMatchTypeEnum[match]
        line = f"keyword '{text}' [{match}]"
        if k.get("cpc_bid"):
            c.cpc_bid_micros = to_micros(float(k["cpc_bid"]), "bid")
            line += f", max CPC {money(c.cpc_bid_micros)} {acct.currency}"
        ch.ops.append(op)
        ch.lines.append(line)

    def negatives(self, grt: str, acct: Account, level: str, target_id: str, keywords: list[dict[str, Any]]) -> Change:
        e = self.t.enums
        tid = _num(target_id)
        if level == "campaign":
            rows = self.rows(grt, acct, f"SELECT campaign.name FROM campaign WHERE campaign.id = {tid}")
            name = rows and f"campaign '{rows[0]['campaign.name']}'"
        else:
            rows = self.rows(grt, acct, f"SELECT ad_group.name FROM ad_group WHERE ad_group.id = {tid}")
            name = rows and f"ad group '{rows[0]['ad_group.name']}'"
        if not rows:
            raise AdsError(f"{level.replace('_', ' ').title()} {tid} not found in {acct.name}.")
        if not keywords or len(keywords) > MAX_ITEMS:
            raise AdsError(f"Give 1 to {MAX_ITEMS} negative keywords.")
        ch = Change(acct, f"Add {len(keywords)} negative keyword(s) to {name}")
        for k in keywords:
            text = _check_keyword(k.get("text", ""))
            match = str(k.get("match_type") or "PHRASE").upper()
            op = self.op()
            if level == "campaign":
                c = op.campaign_criterion_operation.create
                c.campaign = self.rn(acct, "campaigns", tid)
            else:
                c = op.ad_group_criterion_operation.create
                c.ad_group = self.rn(acct, "adGroups", tid)
            c.negative = True
            c.keyword.text = text
            c.keyword.match_type = e.KeywordMatchTypeEnum[match]
            ch.ops.append(op)
            ch.lines.append(f"negative '{text}' [{match}]")
        return ch

    def rsa_op(self, acct: Account, ad_group_rn: str, ad: dict[str, Any], status: str) -> tuple[Any, str]:
        headlines = [h.strip() for h in ad.get("headlines") or []]
        descriptions = [d.strip() for d in ad.get("descriptions") or []]
        path1, path2 = ad.get("path1") or None, ad.get("path2") or None
        url = (ad.get("final_url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise AdsError("The ad needs a final_url starting with https://")
        check_rsa(headlines, descriptions, path1, path2)
        e = self.t.enums
        op = self.op()
        a = op.ad_group_ad_operation.create
        a.ad_group = ad_group_rn
        a.status = e.AdGroupAdStatusEnum[status]
        a.ad.final_urls.append(url)
        for h in headlines:
            asset = self.t.get_type("AdTextAsset")
            asset.text = h
            a.ad.responsive_search_ad.headlines.append(asset)
        for d in descriptions:
            asset = self.t.get_type("AdTextAsset")
            asset.text = d
            a.ad.responsive_search_ad.descriptions.append(asset)
        if path1:
            a.ad.responsive_search_ad.path1 = path1
        if path2:
            a.ad.responsive_search_ad.path2 = path2
        return op, f"responsive search ad ({status}) to {url}: {len(headlines)} headlines, {len(descriptions)} descriptions"

    def new_ad(self, grt: str, acct: Account, ad_group_id: str, ad: dict[str, Any], status: str) -> Change:
        agid = _num(ad_group_id, "ad group ID")
        rows = self.rows(grt, acct, f"SELECT ad_group.name FROM ad_group WHERE ad_group.id = {agid}")
        if not rows:
            raise AdsError(f"Ad group {agid} not found in {acct.name}.")
        ch = Change(acct, f"Create an ad in '{rows[0]['ad_group.name']}'")
        op, line = self.rsa_op(acct, self.rn(acct, "adGroups", agid), ad, status)
        ch.ops.append(op)
        ch.lines.append(line)
        ch.warnings.append("New ads go through Google's policy review (usually within a day) before they show.")
        return ch

    def new_ad_group(self, grt: str, acct: Account, campaign_id: str, name: str, cpc_bid: float | None,
                     status: str, keywords: list[dict[str, Any]] | None, ad: dict[str, Any] | None) -> Change:
        cid = _num(campaign_id, "campaign ID")
        rows = self.rows(grt, acct, f"SELECT campaign.name, campaign.status FROM campaign WHERE campaign.id = {cid}")
        if not rows:
            raise AdsError(f"Campaign {cid} not found in {acct.name}.")
        ch = Change(acct, f"Create ad group '{name}' in '{rows[0]['campaign.name']}'")
        ag_rn = self.rn(acct, "adGroups", -1)
        self._ad_group_ops(ch, acct, self.rn(acct, "campaigns", cid), ag_rn, name, cpc_bid, status, keywords, ad)
        if status == "ENABLED" and rows[0]["campaign.status"] == "ENABLED":
            ch.large.append("the new ad group is on, in a campaign that's on, so it can start spending money")
        return ch

    def _ad_group_ops(self, ch, acct, campaign_rn, ag_rn, name, cpc_bid, status, keywords, ad) -> None:
        e = self.t.enums
        if not name or len(name) > 255:
            raise AdsError("The ad group needs a name (max 255 characters).")
        op = self.op()
        g = op.ad_group_operation.create
        g.resource_name = ag_rn
        g.name = name
        g.campaign = campaign_rn
        g.status = e.AdGroupStatusEnum[status]
        g.type_ = e.AdGroupTypeEnum.SEARCH_STANDARD
        line = f"ad group '{name}' ({status})"
        if cpc_bid:
            g.cpc_bid_micros = to_micros(cpc_bid, "ad group bid")
            line += f", default max CPC {money(g.cpc_bid_micros)} {acct.currency}"
        ch.ops.append(op)
        ch.lines.append(line)
        for k in keywords or []:
            self._keyword_op(ch, acct, ag_rn, k)
        if ad:
            op, l2 = self.rsa_op(acct, ag_rn, ad, "ENABLED")
            ch.ops.append(op)
            ch.lines.append(l2)

    def new_campaign(self, grt: str, acct: Account, a: dict[str, Any]) -> Change:
        e = self.t.enums
        name = (a.get("name") or "").strip()
        if not name or len(name) > 255:
            raise AdsError("The campaign needs a name (max 255 characters).")
        existing = self.rows(grt, acct, "SELECT campaign.id FROM campaign WHERE campaign.name = '"
                             + name.replace("\\", "\\\\").replace("'", "\\'") + "' AND campaign.status != 'REMOVED'")
        if existing:
            raise AdsError(f"There's already a campaign called '{name}' in {acct.name}. Pick another name.")
        budget_rn = self.rn(acct, "campaignBudgets", -1)
        camp_rn = self.rn(acct, "campaigns", -2)
        ch = Change(acct, f"Create search campaign '{name}' (PAUSED)")

        op = self.op()
        b = op.campaign_budget_operation.create
        b.resource_name = budget_rn
        b.name = f"{name} budget {int(time.time())}"
        b.amount_micros = to_micros(a["daily_budget"], "daily budget")
        b.delivery_method = e.BudgetDeliveryMethodEnum.STANDARD
        b.explicitly_shared = False
        ch.ops.append(op)
        ch.lines.append(f"daily budget {money(b.amount_micros)} {acct.currency}")

        op = self.op()
        c = op.campaign_operation.create
        c.resource_name = camp_rn
        c.name = name
        c.status = e.CampaignStatusEnum.PAUSED
        c.advertising_channel_type = e.AdvertisingChannelTypeEnum.SEARCH
        c.campaign_budget = budget_rn
        c.network_settings.target_google_search = True
        c.network_settings.target_search_network = bool(a.get("search_partners"))
        c.network_settings.target_content_network = False
        c.network_settings.target_partner_search_network = False
        c.geo_target_type_setting.positive_geo_target_type = e.PositiveGeoTargetTypeEnum.PRESENCE
        c.contains_eu_political_advertising = e.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        strategy = a.get("bidding") or "maximize_clicks"
        self._set_bidding(c, strategy, a.get("max_cpc_limit"), a.get("target_cpa"), a.get("target_roas"))
        if a.get("end_date"):
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", a["end_date"]):
                raise AdsError("end_date must be YYYY-MM-DD.")
            c.end_date_time = f"{a['end_date']} 23:59:59"
        ch.ops.append(op)
        ch.lines.append(f"campaign '{name}', status PAUSED, Google Search"
                        + (" + search partners" if a.get("search_partners") else "") + ", no Display network")
        ch.lines.append("bidding: " + self._describe_bidding(acct, strategy, a.get("max_cpc_limit"), a.get("target_cpa"), a.get("target_roas")))
        if a.get("end_date"):
            ch.lines.append(f"ends {a['end_date']}")

        locs = [str(_num(x, "location ID")) for x in a.get("location_ids") or []]
        for loc in locs:
            op = self.op()
            cc = op.campaign_criterion_operation.create
            cc.campaign = camp_rn
            cc.location.geo_target_constant = f"geoTargetConstants/{loc}"
            ch.ops.append(op)
        for loc in a.get("excluded_location_ids") or []:
            op = self.op()
            cc = op.campaign_criterion_operation.create
            cc.campaign = camp_rn
            cc.negative = True
            cc.location.geo_target_constant = f"geoTargetConstants/{_num(loc, 'location ID')}"
            ch.ops.append(op)
        ch.lines.append(f"locations: {', '.join(locs) if locs else 'ALL COUNTRIES (no location set)'}"
                        + (" (people in or regularly in these places)" if locs else ""))
        if not locs:
            ch.warnings.append("No locations set, so ads could show worldwide. Use find_locations to get IDs.")
        langs = [str(_num(x, "language ID")) for x in a.get("language_ids") or ["1000"]]
        for lang in langs:
            op = self.op()
            cc = op.campaign_criterion_operation.create
            cc.campaign = camp_rn
            cc.language.language_constant = f"languageConstants/{lang}"
            ch.ops.append(op)
        ch.lines.append(f"languages: {', '.join(langs)}")

        if a.get("ad_group_name"):
            self._ad_group_ops(ch, acct, camp_rn, self.rn(acct, "adGroups", -3), a["ad_group_name"],
                               a.get("ad_group_cpc_bid"), "ENABLED", a.get("keywords"), a.get("ad"))
        elif a.get("keywords") or a.get("ad"):
            raise AdsError("To add keywords or an ad, also give ad_group_name.")
        if strategy == "manual_cpc" and a.get("ad_group_name") and not a.get("ad_group_cpc_bid"):
            ch.warnings.append("Manual CPC without an ad group bid: set ad_group_cpc_bid or the keywords won't bid.")
        ch.warnings.append("The campaign is created PAUSED. Nothing spends until you turn it on with set_status.")
        return ch

    def campaign_settings(self, grt: str, acct: Account, campaign_id: str, name: str | None, end_date: str | None,
                          search_partners: bool | None, display_network: bool | None) -> Change:
        cid = _num(campaign_id, "campaign ID")
        rows = self.rows(grt, acct, "SELECT campaign.name, campaign.end_date_time, campaign.network_settings.target_search_network, "
                                    f"campaign.network_settings.target_content_network FROM campaign WHERE campaign.id = {cid}")
        if not rows:
            raise AdsError(f"Campaign {cid} not found in {acct.name}.")
        r = rows[0]
        op = self.op()
        c = op.campaign_operation.update
        c.resource_name = self.rn(acct, "campaigns", cid)
        paths = op.campaign_operation.update_mask.paths
        ch = Change(acct, f"Change settings of '{r['campaign.name']}'")
        if name:
            c.name = name.strip()
            paths.append("name")
            ch.lines.append(f"name: '{r['campaign.name']}' -> '{c.name}'")
        if end_date:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", end_date):
                raise AdsError("end_date must be YYYY-MM-DD.")
            c.end_date_time = f"{end_date} 23:59:59"
            paths.append("end_date_time")
            ch.lines.append(f"end date: {r.get('campaign.end_date_time') or 'none'} -> {end_date}")
        if search_partners is not None:
            c.network_settings.target_search_network = search_partners
            paths.append("network_settings.target_search_network")
            ch.lines.append(f"search partners: {'on' if search_partners else 'off'}")
        if display_network is not None:
            c.network_settings.target_content_network = display_network
            paths.append("network_settings.target_content_network")
            ch.lines.append(f"display network: {'on' if display_network else 'off'}")
            if display_network:
                ch.warnings.append("Display network expansion often spends on low-quality placements.")
        if not paths:
            raise AdsError("Say what to change: name, end_date, search_partners or display_network.")
        ch.ops.append(op)
        return ch

    def targeting(self, grt: str, acct: Account, campaign_id: str, add_locations, remove_locations,
                  exclude_locations, add_languages, remove_languages) -> Change:
        cid = _num(campaign_id, "campaign ID")
        rows = self.rows(grt, acct, f"SELECT campaign.name FROM campaign WHERE campaign.id = {cid}")
        if not rows:
            raise AdsError(f"Campaign {cid} not found in {acct.name}.")
        ch = Change(acct, f"Change targeting of '{rows[0]['campaign.name']}'")
        camp_rn = self.rn(acct, "campaigns", cid)
        current = self.rows(grt, acct, "SELECT campaign_criterion.resource_name, campaign_criterion.criterion_id, "
                                       "campaign_criterion.type, campaign_criterion.negative FROM campaign_criterion "
                                       f"WHERE campaign.id = {cid} AND campaign_criterion.type IN ('LOCATION', 'LANGUAGE')")
        by_id = {(r["campaign_criterion.type"], str(r["campaign_criterion.criterion_id"])): r for r in current}
        for kind, ids, negative in (("LOCATION", add_locations, False), ("LOCATION", exclude_locations, True),
                                    ("LANGUAGE", add_languages, False)):
            for x in ids or []:
                n = str(_num(x))
                if (kind, n) in by_id:
                    continue
                op = self.op()
                cc = op.campaign_criterion_operation.create
                cc.campaign = camp_rn
                if kind == "LOCATION":
                    cc.location.geo_target_constant = f"geoTargetConstants/{n}"
                    cc.negative = negative
                else:
                    cc.language.language_constant = f"languageConstants/{n}"
                ch.ops.append(op)
                ch.lines.append(f"{'exclude' if negative else 'add'} {kind.lower()} {n}")
        for kind, ids in (("LOCATION", remove_locations), ("LANGUAGE", remove_languages)):
            for x in ids or []:
                n = str(_num(x))
                r = by_id.get((kind, n))
                if not r:
                    raise AdsError(f"{kind.title()} {n} isn't set on this campaign, so it can't be removed.")
                op = self.op()
                op.campaign_criterion_operation.remove = r["campaign_criterion.resource_name"]
                ch.ops.append(op)
                ch.lines.append(f"stop targeting {kind.lower()} {n}")
        if not ch.ops:
            raise AdsError("Nothing to change: those are already set.")
        remaining = {k for k in by_id if k[0] == "LOCATION" and not by_id[k].get("campaign_criterion.negative")}
        removing = {("LOCATION", str(_num(x))) for x in remove_locations or []}
        if remaining and remaining <= removing and not add_locations:
            ch.large.append("this removes every location, so ads could show worldwide")
        return ch

    def raw(self, acct: Account, operations: list[dict[str, Any]], description: str) -> Change:
        MO = type(self.op())
        if not operations or len(operations) > MAX_ITEMS:
            raise AdsError(f"Give 1 to {MAX_ITEMS} operations.")
        ch = Change(acct, description or "Custom Google Ads API change")
        for i, d in enumerate(operations):
            try:
                op = MO.from_json(json.dumps(d))
            except Exception as ex:  # noqa: BLE001 - shown to the caller
                raise AdsError(f"Operation {i + 1} isn't a valid MutateOperation: {ex}") from None
            which = op._pb.WhichOneof("operation")
            if not which:
                raise AdsError(f"Operation {i + 1} is empty.")
            inner = getattr(op, which)
            verb = inner._pb.WhichOneof("operation") or "?"
            ch.ops.append(op)
            ch.lines.append(f"{verb} via {which}" + (f": {getattr(inner, verb)}" if verb == "remove" else ""))
        ch.large.append("custom API operations aren't checked by the connector's own safety rules")
        return ch


_BIDDING_FIELD = {
    "manual_cpc": "manual_cpc",
    "maximize_clicks": "target_spend",
    "maximize_conversions": "maximize_conversions",
    "maximize_conversion_value": "maximize_conversion_value",
}

Bidding = Literal["manual_cpc", "maximize_clicks", "maximize_conversions", "maximize_conversion_value"]
CID = Annotated[str, Field(description="Client account ID, e.g. 399-355-6480")]


# --------------------------------------------------------------------- tools


def register_write_tools(mcp: FastMCP, settings: Settings, ads: AdsGateway, sealer: Sealer, run) -> Writer:
    w = Writer(settings, ads, sealer)

    def prepared(tool: str, customer_id: str, build):
        def go(user, grt):
            acct = ads.account(user, grt, customer_id)
            return w.preview(user, grt, build(grt, acct))

        return run(tool, customer_id, go)

    @mcp.tool(annotations=PREVIEW)
    async def set_status(
        customer_id: CID,
        entity: Annotated[Entity, Field(description="What to turn on or off.")],
        ids: Annotated[list[str], Field(description="Campaign or ad group IDs. For ads and keywords use 'adGroupId~id'.")],
        status: Literal["ENABLED", "PAUSED"],
    ) -> dict[str, Any]:
        """Preview pausing or enabling campaigns, ad groups, ads or keywords. Changes nothing until
        apply_change. Enabling is flagged because it can start spending."""
        return await prepared("set_status", customer_id, lambda g, a: w.status_change(g, a, entity, ids, status))

    @mcp.tool(annotations=PREVIEW)
    async def remove_items(
        customer_id: CID,
        entity: Entity,
        ids: Annotated[list[str], Field(description="Campaign or ad group IDs. For ads and keywords use 'adGroupId~id'.")],
        confirm_names: Annotated[list[str] | None, Field(
            description="Campaigns and ad groups only: their exact names, as the person typed them to confirm.")] = None,
    ) -> dict[str, Any]:
        """Preview permanently removing campaigns, ad groups, ads or keywords (including negative keywords
        in ad groups). Removal can't be undone; prefer set_status PAUSED unless the person clearly wants removal."""
        return await prepared("remove_items", customer_id, lambda g, a: w.removal(g, a, entity, ids, confirm_names))

    @mcp.tool(annotations=PREVIEW)
    async def update_budget(
        customer_id: CID,
        campaign_id: str,
        daily_amount: Annotated[float, Field(gt=0, description="New average daily budget in the account's currency.")],
    ) -> dict[str, Any]:
        """Preview changing a campaign's daily budget. Big increases need extra confirmation."""
        return await prepared("update_budget", customer_id, lambda g, a: w.budget(g, a, campaign_id, daily_amount))

    @mcp.tool(annotations=PREVIEW)
    async def update_bids(
        customer_id: CID,
        level: Literal["ad_group", "keyword"],
        bids: Annotated[list[dict[str, Any]], Field(description=(
            "List of {id, cpc_bid}. id is the ad group ID, or 'adGroupId~keywordId' for keywords. "
            "cpc_bid is the max cost per click in the account's currency."))],
    ) -> dict[str, Any]:
        """Preview changing max CPC bids on ad groups or keywords (used by manual CPC bidding)."""
        return await prepared("update_bids", customer_id, lambda g, a: w.bids(g, a, level, bids))

    @mcp.tool(annotations=PREVIEW)
    async def set_bidding_strategy(
        customer_id: CID,
        campaign_id: str,
        strategy: Bidding,
        max_cpc_limit: Annotated[float | None, Field(description="maximize_clicks only: highest CPC allowed.")] = None,
        target_cpa: Annotated[float | None, Field(description="maximize_conversions only: target cost per conversion.")] = None,
        target_roas: Annotated[float | None, Field(description="maximize_conversion_value only: e.g. 4.0 = 400%.")] = None,
    ) -> dict[str, Any]:
        """Preview switching a campaign's bid strategy."""
        return await prepared("set_bidding_strategy", customer_id,
                              lambda g, a: w.bidding(g, a, campaign_id, strategy, max_cpc_limit, target_cpa, target_roas))

    @mcp.tool(annotations=PREVIEW)
    async def add_keywords(
        customer_id: CID,
        ad_group_id: str,
        keywords: Annotated[list[dict[str, Any]], Field(description=(
            "List of {text, match_type EXACT|PHRASE|BROAD (default PHRASE), cpc_bid (optional)}."))],
    ) -> dict[str, Any]:
        """Preview adding keywords to an ad group."""
        return await prepared("add_keywords", customer_id, lambda g, a: w.keywords(g, a, ad_group_id, keywords))

    @mcp.tool(annotations=PREVIEW)
    async def add_negative_keywords(
        customer_id: CID,
        level: Literal["campaign", "ad_group"],
        target_id: Annotated[str, Field(description="The campaign ID or ad group ID.")],
        keywords: Annotated[list[dict[str, Any]], Field(description="List of {text, match_type EXACT|PHRASE|BROAD (default PHRASE)}.")],
    ) -> dict[str, Any]:
        """Preview adding negative keywords (search terms to block) to a campaign or ad group."""
        return await prepared("add_negative_keywords", customer_id,
                              lambda g, a: w.negatives(g, a, level, target_id, keywords))

    @mcp.tool(annotations=PREVIEW)
    async def create_search_campaign(
        customer_id: CID,
        name: str,
        daily_budget: Annotated[float, Field(gt=0, description="Average daily budget in the account's currency.")],
        location_ids: Annotated[list[str] | None, Field(description="Location IDs from find_locations.")] = None,
        excluded_location_ids: list[str] | None = None,
        language_ids: Annotated[list[str] | None, Field(description="Default ['1000'] English. 1019 Arabic, 1041 Urdu.")] = None,
        bidding: Bidding = "maximize_clicks",
        max_cpc_limit: float | None = None,
        target_cpa: float | None = None,
        target_roas: float | None = None,
        search_partners: bool = False,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        ad_group_name: Annotated[str | None, Field(description="Also create a first ad group with this name.")] = None,
        ad_group_cpc_bid: Annotated[float | None, Field(description="Default max CPC for the ad group (manual CPC).")] = None,
        keywords: Annotated[list[dict[str, Any]] | None, Field(description="For the ad group: {text, match_type, cpc_bid}.")] = None,
        ad: Annotated[dict[str, Any] | None, Field(description=(
            "For the ad group, a responsive search ad: {final_url, headlines (3-15, max 30 chars), "
            "descriptions (2-4, max 90 chars), path1, path2 (max 15 chars)}."))] = None,
    ) -> dict[str, Any]:
        """Preview creating a complete Google Search campaign in one step: budget, bidding, locations,
        languages, and optionally a first ad group with keywords and a responsive search ad.
        The campaign is always created PAUSED; turn it on later with set_status."""
        args = dict(name=name, daily_budget=daily_budget, location_ids=location_ids,
                    excluded_location_ids=excluded_location_ids, language_ids=language_ids, bidding=bidding,
                    max_cpc_limit=max_cpc_limit, target_cpa=target_cpa, target_roas=target_roas,
                    search_partners=search_partners, end_date=end_date, ad_group_name=ad_group_name,
                    ad_group_cpc_bid=ad_group_cpc_bid, keywords=keywords, ad=ad)
        return await prepared("create_search_campaign", customer_id, lambda g, a: w.new_campaign(g, a, args))

    @mcp.tool(annotations=PREVIEW)
    async def create_ad_group(
        customer_id: CID,
        campaign_id: str,
        name: str,
        cpc_bid: Annotated[float | None, Field(description="Default max CPC (manual CPC campaigns).")] = None,
        status: Literal["ENABLED", "PAUSED"] = "PAUSED",
        keywords: Annotated[list[dict[str, Any]] | None, Field(description="{text, match_type, cpc_bid}")] = None,
        ad: Annotated[dict[str, Any] | None, Field(description="Optional responsive search ad, same shape as in create_search_campaign.")] = None,
    ) -> dict[str, Any]:
        """Preview creating an ad group in an existing search campaign, optionally with keywords and an ad."""
        return await prepared("create_ad_group", customer_id,
                              lambda g, a: w.new_ad_group(g, a, campaign_id, name, cpc_bid, status, keywords, ad))

    @mcp.tool(annotations=PREVIEW)
    async def create_responsive_search_ad(
        customer_id: CID,
        ad_group_id: str,
        final_url: str,
        headlines: Annotated[list[str], Field(description="3 to 15 headlines, each up to 30 characters.")],
        descriptions: Annotated[list[str], Field(description="2 to 4 descriptions, each up to 90 characters.")],
        path1: Annotated[str | None, Field(description="Display URL path, up to 15 characters.")] = None,
        path2: str | None = None,
        status: Literal["ENABLED", "PAUSED"] = "ENABLED",
    ) -> dict[str, Any]:
        """Preview creating a responsive search ad in an ad group. (Existing ads can't be edited in
        Google Ads: create a new one and pause the old one.)"""
        ad = dict(final_url=final_url, headlines=headlines, descriptions=descriptions, path1=path1, path2=path2)
        return await prepared("create_responsive_search_ad", customer_id, lambda g, a: w.new_ad(g, a, ad_group_id, ad, status))

    @mcp.tool(annotations=PREVIEW)
    async def update_campaign_settings(
        customer_id: CID,
        campaign_id: str,
        name: str | None = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        search_partners: bool | None = None,
        display_network: bool | None = None,
    ) -> dict[str, Any]:
        """Preview renaming a campaign, setting its end date, or switching search partners / display network."""
        return await prepared("update_campaign_settings", customer_id,
                              lambda g, a: w.campaign_settings(g, a, campaign_id, name, end_date, search_partners, display_network))

    @mcp.tool(annotations=PREVIEW)
    async def update_campaign_targeting(
        customer_id: CID,
        campaign_id: str,
        add_location_ids: list[str] | None = None,
        remove_location_ids: list[str] | None = None,
        exclude_location_ids: list[str] | None = None,
        add_language_ids: list[str] | None = None,
        remove_language_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Preview changing a campaign's target locations (IDs from find_locations), excluded locations and languages."""
        return await prepared("update_campaign_targeting", customer_id, lambda g, a: w.targeting(
            g, a, campaign_id, add_location_ids, remove_location_ids, exclude_location_ids, add_language_ids, remove_language_ids))

    @mcp.tool(annotations=PREVIEW)
    async def advanced_change(
        customer_id: CID,
        operations: Annotated[list[dict[str, Any]], Field(description=(
            "Google Ads API v26 MutateOperation objects as JSON, e.g. "
            "{\"campaignOperation\": {\"update\": {\"resourceName\": \"customers/123/campaigns/456\", ...}, "
            "\"updateMask\": \"name\"}}. Temporary negative IDs link new items together."))],
        description: Annotated[str, Field(description="Plain-language summary of what this does, shown to the person.")],
    ) -> dict[str, Any]:
        """Preview any other Google Ads change (sitelinks and other assets, ad schedules, bid adjustments,
        labels, shared negative lists, conversion actions, Performance Max and more) using raw API operations.
        Use the dedicated tools when one fits. Always needs extra confirmation."""

        def go(user, grt):
            acct = ads.account(user, grt, customer_id)
            return w.preview(user, grt, w.raw(acct, operations, description))

        return await run("advanced_change", customer_id, go)

    @mcp.tool(annotations=APPLY)
    async def apply_change(
        confirm_token: Annotated[str, Field(description="The confirm_token from a preview.")],
        confirm_large_change: Annotated[bool, Field(description=(
            "Must be true for previews with needs_extra_confirmation, and only after the person agreed to those items."))] = False,
    ) -> dict[str, Any]:
        """Make a change you previewed, after the person has seen the preview and said yes.
        Each token works once and expires after 15 minutes."""
        return await run("apply_change", None, lambda user, grt: w.apply(user, grt, confirm_token, confirm_large_change))

    return w
