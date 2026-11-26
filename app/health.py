"""Phase 3: account health check.

Runs a set of read-only checks on one account and returns problems ranked by
severity, each with a suggested fix (the change tool and arguments to preview it).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from .ads import Account, AdsGateway, pretty_id

SEVERITY_COST = {"high": 15, "medium": 7, "low": 3}


def _m(v: Any) -> float:
    return round((v or 0) / 1_000_000, 2)


class HealthCheck:
    def __init__(self, ads: AdsGateway, grt: str, acct: Account, days: int):
        self.ads, self.grt, self.acct = ads, grt, acct
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        self.period = f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
        self.period_label = f"{start.isoformat()} to {end.isoformat()}"
        self.issues: list[dict[str, Any]] = []
        self.skipped: list[str] = []
        self.summary: dict[str, Any] = {}
        self.campaigns: list[dict[str, Any]] = []

    def q(self, query: str, limit: int = 1000) -> list[dict[str, Any]]:
        return self.ads.search(self.grt, self.acct, query, limit)[0]

    def add(self, severity: str, area: str, finding: str, fix: str | None = None,
            tool: str | None = None, arguments: dict[str, Any] | None = None) -> None:
        item: dict[str, Any] = {"severity": severity, "area": area, "finding": finding}
        if fix:
            item["suggested_fix"] = fix
        if tool:
            item["fix_with"] = {"tool": tool, "arguments": {"customer_id": pretty_id(self.acct.customer_id), **(arguments or {})}}
        self.issues.append(item)

    def run(self) -> dict[str, Any]:
        checks: list[tuple[str, Callable[[], None]]] = [
            ("campaigns and budgets", self.check_campaigns),
            ("conversion tracking", self.check_conversions),
            ("wasted search terms", self.check_search_terms),
            ("quality score", self.check_quality_score),
            ("ads", self.check_ads),
            ("extensions", self.check_extensions),
        ]
        for name, fn in checks:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - one failing check shouldn't stop the report
                self.skipped.append(f"{name}: {str(e)[:200]}")
        order = {"high": 0, "medium": 1, "low": 2}
        self.issues.sort(key=lambda i: order[i["severity"]])
        score = max(0, 100 - sum(SEVERITY_COST[i["severity"]] for i in self.issues))
        return {
            "account": f"{self.acct.name} ({pretty_id(self.acct.customer_id)})",
            "currency": self.acct.currency,
            "period": self.period_label,
            "health_score": score,
            "summary": self.summary,
            "issue_count": {s: sum(i["severity"] == s for i in self.issues) for s in ("high", "medium", "low")},
            "issues": self.issues,
            "checks_skipped": self.skipped,
            "how_to_fix": "Each fix_with names a change tool and its arguments. Calling it only previews; "
                          "show the person, and apply with apply_change after they agree.",
        }

    # ------------------------------------------------------------- checks

    def check_campaigns(self) -> None:
        rows = self.q(
            "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, campaign.bidding_strategy_type, "
            "campaign.network_settings.target_content_network, campaign_budget.amount_micros, "
            "metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions, metrics.conversions_value, "
            "metrics.search_budget_lost_impression_share, metrics.search_rank_lost_impression_share, "
            "metrics.search_impression_share FROM campaign WHERE campaign.status = 'ENABLED' AND " + self.period
        )
        self.campaigns = rows
        cost = sum(_m(r.get("metrics.cost_micros")) for r in rows)
        conv = sum(r.get("metrics.conversions") or 0 for r in rows)
        self.summary = {
            "running_campaigns": len(rows),
            "spend": round(cost, 2),
            "clicks": sum(r.get("metrics.clicks") or 0 for r in rows),
            "conversions": round(conv, 1),
            "cost_per_conversion": round(cost / conv, 2) if conv else None,
            "daily_budget_total": round(sum(_m(r.get("campaign_budget.amount_micros")) for r in rows), 2),
        }
        if not rows:
            self.add("medium", "campaigns", "No campaigns are running in this account.")
            return
        for r in rows:
            name, cid = r["campaign.name"], str(r["campaign.id"])
            is_search = r.get("campaign.advertising_channel_type") == "SEARCH"
            c_cost, c_conv = _m(r.get("metrics.cost_micros")), r.get("metrics.conversions") or 0
            lost_budget = r.get("metrics.search_budget_lost_impression_share") or 0
            lost_rank = r.get("metrics.search_rank_lost_impression_share") or 0
            if is_search and r.get("campaign.network_settings.target_content_network"):
                self.add("medium", "settings", f"'{name}' is a Search campaign that also shows on the Display network, "
                         "which often wastes budget on low-quality sites.", "Turn off the Display network.",
                         "update_campaign_settings", {"campaign_id": cid, "display_network": False})
            if is_search and lost_budget > 0.2:
                budget = _m(r.get("campaign_budget.amount_micros"))
                sev = "high" if c_conv and lost_budget > 0.4 else "medium"
                self.add(sev, "budget", f"'{name}' missed {lost_budget:.0%} of possible impressions because its budget "
                         f"({budget:,.2f}/day) ran out" + (f", while getting {c_conv:.0f} conversions." if c_conv else "."),
                         "Raise the budget if results are good, or narrow keywords and locations.",
                         "update_budget", {"campaign_id": cid, "daily_amount": round(budget * 1.3, 2)})
            if is_search and lost_rank > 0.5:
                self.add("medium", "ad rank", f"'{name}' missed {lost_rank:.0%} of impressions because of low ad rank "
                         "(bids or ad quality).", "Improve ad relevance and Quality Score, or raise bids on the best keywords.")
            if c_cost > 0 and not c_conv and self.summary["conversions"]:
                self.add("medium", "results", f"'{name}' spent {c_cost:,.2f} {self.acct.currency} with no conversions.",
                         "Check its search terms and keywords; pause it if it doesn't improve.")

    def check_conversions(self) -> None:
        rows = self.q("SELECT conversion_action.name, conversion_action.status, conversion_action.primary_for_goal "
                      "FROM conversion_action WHERE conversion_action.status = 'ENABLED'")
        primary = [r for r in rows if r.get("conversion_action.primary_for_goal")]
        if not rows:
            self.add("high", "tracking", "No conversion tracking is set up, so Google can't tell which clicks bring "
                     "customers, and smart bidding can't work.", "Set up conversion tracking (form submits, calls, purchases).")
        elif not primary:
            self.add("high", "tracking", "Conversion actions exist, but none is marked as primary, so bidding ignores them.",
                     "Mark the main conversion action as primary in Goals.")
        elif self.summary.get("spend") and not self.summary.get("conversions"):
            self.add("high", "tracking", f"{self.summary['spend']:,.2f} {self.acct.currency} spent and zero conversions "
                     "recorded. Tracking may be broken.", "Test the conversion tag on the website (Google Tag Assistant).")

    def check_search_terms(self) -> None:
        if not self.summary.get("conversions"):
            note = "no conversions are recorded, so 'wasted' can't be judged"
            self.skipped.append(f"wasted search terms: {note}")
            return
        cpa = self.summary["cost_per_conversion"] or 0
        rows = self.q(
            "SELECT search_term_view.search_term, search_term_view.status, campaign.id, campaign.name, "
            "metrics.cost_micros, metrics.clicks, metrics.conversions FROM search_term_view "
            f"WHERE {self.period} AND metrics.conversions = 0 AND metrics.cost_micros > 0 "
            "ORDER BY metrics.cost_micros DESC", 50,
        )
        threshold = max(cpa * 0.5, 0.01)
        wasted = [r for r in rows if _m(r.get("metrics.cost_micros")) >= threshold
                  and r.get("search_term_view.status") not in ("EXCLUDED", "ADDED_EXCLUDED")]
        if not wasted:
            return
        total = sum(_m(r.get("metrics.cost_micros")) for r in wasted)
        by_campaign: dict[str, list[dict[str, Any]]] = {}
        for r in wasted[:30]:
            by_campaign.setdefault(str(r["campaign.id"]), []).append(r)
        for cid, terms in by_campaign.items():
            sev = "high" if sum(_m(t.get("metrics.cost_micros")) for t in terms) > cpa * 2 else "medium"
            listed = ", ".join(f"'{t['search_term_view.search_term']}' ({_m(t.get('metrics.cost_micros')):,.2f})" for t in terms[:8])
            self.add(sev, "search terms", f"In '{terms[0]['campaign.name']}', these searches cost money with no conversions: "
                     f"{listed}.", "Review them, and block the irrelevant ones as negative keywords.",
                     "add_negative_keywords", {"level": "campaign", "target_id": cid,
                                               "keywords": [{"text": t["search_term_view.search_term"], "match_type": "EXACT"}
                                                            for t in terms]})
        self.summary["spend_on_non_converting_search_terms"] = round(total, 2)

    def check_quality_score(self) -> None:
        rows = self.q(
            "SELECT ad_group.id, ad_group.name, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
            "ad_group_criterion.quality_info.quality_score, ad_group_criterion.quality_info.creative_quality_score, "
            "ad_group_criterion.quality_info.post_click_quality_score, ad_group_criterion.quality_info.search_predicted_ctr "
            "FROM ad_group_criterion WHERE ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status = 'ENABLED' "
            "AND ad_group_criterion.negative = FALSE AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED' "
            "AND ad_group_criterion.quality_info.quality_score <= 4", 200,
        )
        if not rows:
            return
        reasons: dict[str, int] = {}
        for r in rows:
            for field, label in (("creative_quality_score", "ad relevance"), ("post_click_quality_score", "landing page"),
                                 ("search_predicted_ctr", "expected click rate")):
                if r.get(f"ad_group_criterion.quality_info.{field}") == "BELOW_AVERAGE":
                    reasons[label] = reasons.get(label, 0) + 1
        why = ", ".join(f"{k} below average on {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
        sample = ", ".join(f"'{r['ad_group_criterion.keyword.text']}' (QS {r['ad_group_criterion.quality_info.quality_score']}, "
                           f"id {r['ad_group.id']}~{r['ad_group_criterion.criterion_id']})" for r in rows[:10])
        self.add("medium" if len(rows) >= 5 else "low", "quality score",
                 f"{len(rows)} running keyword(s) have a Quality Score of 4 or less, so clicks cost more: {sample}."
                 + (f" Main causes: {why}." if why else ""),
                 "Put the keyword in its ad headlines, make the landing page match, or pause keywords that don't fit.")

    def check_ads(self) -> None:
        ads = self.q(
            "SELECT campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.type, "
            "ad_group_ad.policy_summary.approval_status, ad_group_ad.ad_strength FROM ad_group_ad "
            "WHERE ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'", 2000,
        )
        groups = self.q("SELECT campaign.name, campaign.advertising_channel_type, ad_group.id, ad_group.name FROM ad_group "
                        "WHERE ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'", 2000)
        with_ads = {str(a["ad_group.id"]) for a in ads}
        for g in groups:
            if g.get("campaign.advertising_channel_type") == "SEARCH" and str(g["ad_group.id"]) not in with_ads:
                self.add("high", "ads", f"Ad group '{g['ad_group.name']}' in '{g['campaign.name']}' is on but has no running "
                         "ads, so it can't show.", "Create a responsive search ad for it.",
                         "create_responsive_search_ad", {"ad_group_id": str(g["ad_group.id"])})
        for a in ads:
            status = a.get("ad_group_ad.policy_summary.approval_status")
            ident = f"{a['ad_group.id']}~{a['ad_group_ad.ad.id']}"
            if status == "DISAPPROVED":
                self.add("high", "ads", f"An ad in '{a['ad_group.name']}' ({a['campaign.name']}) is disapproved by Google "
                         f"(ad id {ident}).", "Check the policy reason in Google Ads, then fix it with a new ad and remove this one.")
            elif status == "APPROVED_LIMITED":
                self.add("low", "ads", f"An ad in '{a['ad_group.name']}' is approved but limited by policy (ad id {ident}).",
                         "Check the policy details; it may not show in some places.")
        poor = [a for a in ads if a.get("ad_group_ad.ad_strength") == "POOR"]
        if poor:
            names = ", ".join(sorted({a["ad_group.name"] for a in poor})[:8])
            self.add("medium", "ads", f"{len(poor)} ad(s) have Poor ad strength (in {names}).",
                     "Add more unique headlines (aim for 10 to 15) that include the main keywords.")

    def check_extensions(self) -> None:
        search = [r for r in self.campaigns if r.get("campaign.advertising_channel_type") == "SEARCH"]
        if not search:
            return
        account_types = {r.get("customer_asset.field_type") for r in self.q(
            "SELECT customer_asset.field_type FROM customer_asset WHERE customer_asset.status = 'ENABLED'")}
        camp_types: dict[str, set[str]] = {}
        for r in self.q("SELECT campaign.id, campaign_asset.field_type FROM campaign_asset "
                        "WHERE campaign_asset.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            camp_types.setdefault(str(r["campaign.id"]), set()).add(r.get("campaign_asset.field_type"))
        for kind, label, tool in (("SITELINK", "sitelinks", "add_sitelinks"), ("CALLOUT", "callouts", "add_callouts"),
                                  ("CALL", "a phone number", "add_call_button")):
            missing = [r for r in search if kind not in account_types and kind not in camp_types.get(str(r["campaign.id"]), set())]
            if missing:
                names = ", ".join(f"'{r['campaign.name']}'" for r in missing[:6])
                sev = "medium" if kind in ("SITELINK", "CALLOUT") else "low"
                self.add(sev, "extensions", f"{len(missing)} search campaign(s) have no {label}: {names}.",
                         f"Add {label}; they make ads bigger and usually raise click rate.", tool,
                         {"level": "account"} if len(missing) == len(search) else {"level": "campaign",
                                                                                     "campaign_id": str(missing[0]["campaign.id"])})
