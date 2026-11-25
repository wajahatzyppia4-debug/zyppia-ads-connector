"""Zyppia Google Ads connector: an MCP server for Claude.

Phase 1: accounts, reports (GAQL), keyword ideas, change history (read-only).
Phase 2: changes, always previewed first and applied with apply_change (see writes.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, timedelta
from typing import Annotated, Any, Literal

import anyio
from google.ads.googleads.errors import GoogleAdsException
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import AdsError, AdsGateway, describe_ads_exception, error_codes, pretty_id
from .auth import GoogleBackedOAuthProvider
from .config import Settings, load_settings
from .writes import register_write_tools

log = logging.getLogger("zyppia")
audit = logging.getLogger("zyppia.audit")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

INSTRUCTIONS = """\
Zyppia's Google Ads connector. It works on every Google Ads account the signed-in
person can access: client accounts under Zyppia's manager (MCC) account and accounts
where their login was added directly.

Start with list_accounts to find the customer ID for a client. Money values are in
each account's own currency (shown in list_accounts); fields ending in _micros are
also given in normal units without the suffix, and average_cpc / cost_per_conversion
are already in normal units.

Making changes always takes two steps:
1. Call a change tool (set_status, update_budget, add_keywords, create_search_campaign,
   ...). It only previews: Google checks the change and nothing is modified.
2. Show the person exactly what will change, in plain words, and ask them to confirm.
   Only after a clear yes, call apply_change with the confirm_token. If the preview
   lists needs_extra_confirmation, name those items and get an explicit yes before
   passing confirm_large_change=true. Never apply a change the person hasn't seen.

New campaigns are created PAUSED. Prefer pausing over removing. IDs for ads and
keywords are 'adGroupId~id' (both are columns in performance_report rows).
"""

DATE_PRESETS = Literal[
    "TODAY", "YESTERDAY", "LAST_7_DAYS", "LAST_14_DAYS", "LAST_30_DAYS",
    "THIS_MONTH", "LAST_MONTH", "THIS_WEEK_MON_TODAY", "LAST_WEEK_MON_SUN",
]

LEVELS = {
    "account": ("customer", ["customer.descriptive_name"], None),
    "campaign": (
        "campaign",
        ["campaign.id", "campaign.name", "campaign.status", "campaign.advertising_channel_type",
         "campaign.bidding_strategy_type", "campaign_budget.amount_micros"],
        "campaign.status != 'REMOVED'",
    ),
    "ad_group": (
        "ad_group",
        ["campaign.id", "campaign.name", "ad_group.id", "ad_group.name", "ad_group.status", "ad_group.cpc_bid_micros"],
        "ad_group.status != 'REMOVED'",
    ),
    "keyword": (
        "keyword_view",
        ["campaign.id", "campaign.name", "ad_group.id", "ad_group.name", "ad_group_criterion.criterion_id",
         "ad_group_criterion.keyword.text", "ad_group_criterion.keyword.match_type", "ad_group_criterion.status",
         "ad_group_criterion.effective_cpc_bid_micros"],
        "ad_group_criterion.status != 'REMOVED'",
    ),
    "search_term": (
        "search_term_view",
        ["campaign.id", "campaign.name", "ad_group.id", "ad_group.name", "search_term_view.search_term",
         "search_term_view.status"],
        None,
    ),
    "ad": (
        "ad_group_ad",
        ["campaign.id", "campaign.name", "ad_group.id", "ad_group.name", "ad_group_ad.ad.id", "ad_group_ad.ad.type",
         "ad_group_ad.status", "ad_group_ad.ad.final_urls"],
        "ad_group_ad.status != 'REMOVED'",
    ),
}
METRICS = [
    "metrics.impressions", "metrics.clicks", "metrics.ctr", "metrics.average_cpc",
    "metrics.cost_micros", "metrics.conversions", "metrics.conversions_value",
    "metrics.cost_per_conversion",
]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _user() -> tuple[str, str]:
    tok = get_access_token()
    grt = getattr(tok, "google_refresh_token", None)
    if tok is None or not grt:
        raise AdsError("Not signed in. Reconnect the Zyppia Google Ads connector in Claude's settings.")
    return tok.subject or "unknown", grt


def build_server(settings: Settings) -> FastMCP:
    provider = GoogleBackedOAuthProvider(settings)
    ads = AdsGateway(settings)

    mcp = FastMCP(
        "Zyppia Google Ads",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=settings.public_url + "/mcp",
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["ads"], default_scopes=["ads"]
            ),
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=False,
        ),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        stateless_http=True,
        json_response=True,
    )

    async def run(tool: str, customer_id: str | None, fn):
        """Run a blocking Google Ads call in a worker thread with uniform errors and audit."""
        user, grt = _user()
        audit.info(json.dumps({"event": "tool", "tool": tool, "user": user, "customer_id": customer_id}))
        try:
            return await anyio.to_thread.run_sync(lambda: fn(user, grt))
        except AdsError as e:
            raise ValueError(str(e)) from None
        except GoogleAdsException as e:
            log.warning("ads error tool=%s %s", tool, describe_ads_exception(e))
            raise ValueError(describe_ads_exception(e)) from None

    # ---------------------------------------------------------------- accounts

    @mcp.tool(annotations=READ_ONLY)
    async def list_accounts(
        refresh: Annotated[bool, Field(description="Re-read the account list from Google instead of the 10-minute cache.")] = False,
    ) -> dict[str, Any]:
        """List every Google Ads account you can work on: clients under the MCC and accounts
        your login was added to directly. Returns customer IDs, names, currency and how each is accessed."""

        def go(user, grt):
            accts = ads.accounts(user, grt, refresh=refresh)
            items = sorted((a.as_dict() for a in accts.values()), key=lambda d: (d["is_manager"], d["name"].lower()))
            return {"signed_in_as": user, "count": len(items), "accounts": items}

        return await run("list_accounts", None, go)

    # ---------------------------------------------------------------- reports

    @mcp.tool(annotations=READ_ONLY)
    async def run_gaql(
        customer_id: Annotated[str, Field(description="Client account ID, e.g. 399-355-6480")],
        query: Annotated[str, Field(description="A Google Ads Query Language SELECT statement.")],
        max_rows: Annotated[int, Field(ge=1, le=5000)] = 500,
    ) -> dict[str, Any]:
        """Run any read-only Google Ads Query Language (GAQL) report on one account.
        Use for anything the ready-made performance_report doesn't cover."""
        if not query.strip().lower().startswith("select"):
            raise ValueError("Only SELECT queries are allowed.")

        def go(user, grt):
            acct = ads.account(user, grt, customer_id)
            rows, truncated = ads.search(grt, acct, query, max_rows)
            return {
                "account": acct.name, "customer_id": pretty_id(acct.customer_id), "currency": acct.currency,
                "row_count": len(rows), "truncated": truncated, "rows": rows,
            }

        return await run("run_gaql", customer_id, go)

    @mcp.tool(annotations=READ_ONLY)
    async def performance_report(
        customer_id: Annotated[str, Field(description="Client account ID, e.g. 399-355-6480")],
        level: Annotated[
            Literal["account", "campaign", "ad_group", "keyword", "search_term", "ad"],
            Field(description="What each row is."),
        ] = "campaign",
        date_range: Annotated[DATE_PRESETS | None, Field(description="Preset range. Ignored if start_date/end_date given.")] = "LAST_30_DAYS",
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        by_day: Annotated[bool, Field(description="Split rows by date.")] = False,
        campaign_id: Annotated[str | None, Field(description="Only this campaign.")] = None,
        max_rows: Annotated[int, Field(ge=1, le=5000)] = 200,
    ) -> dict[str, Any]:
        """Performance report (impressions, clicks, CTR, avg CPC, cost, conversions, value, cost/conv)
        for one account at the chosen level, sorted by cost."""
        resource, fields, where = LEVELS[level]
        sel = list(fields) + METRICS + (["segments.date"] if by_day else [])
        if start_date or end_date:
            if not (start_date and end_date and _DATE_RE.match(start_date) and _DATE_RE.match(end_date)):
                raise ValueError("Give both start_date and end_date as YYYY-MM-DD.")
            date_clause = f"segments.date BETWEEN '{start_date}' AND '{end_date}'"
        else:
            date_clause = f"segments.date DURING {date_range or 'LAST_30_DAYS'}"
        conds = [date_clause] + ([where] if where else [])
        if campaign_id:
            conds.append(f"campaign.id = {int(re.sub(r'[^0-9]', '', campaign_id))}")
        if level != "account":
            conds.append("metrics.impressions > 0")
        q = f"SELECT {', '.join(sel)} FROM {resource} WHERE {' AND '.join(conds)} ORDER BY metrics.cost_micros DESC"

        def go(user, grt):
            acct = ads.account(user, grt, customer_id)
            rows, truncated = ads.search(grt, acct, q, max_rows)
            return {
                "account": acct.name, "customer_id": pretty_id(acct.customer_id), "currency": acct.currency,
                "level": level, "period": date_clause.replace("segments.date ", ""),
                "row_count": len(rows), "truncated": truncated, "rows": rows,
            }

        return await run("performance_report", customer_id, go)

    @mcp.tool(annotations=READ_ONLY)
    async def change_history(
        customer_id: Annotated[str, Field(description="Client account ID")],
        days: Annotated[int, Field(ge=1, le=29, description="How many days back (max 29).")] = 7,
        max_rows: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """Who changed what in an account recently (from Google's change history), newest first."""
        start = (date.today() - timedelta(days=days)).isoformat()
        end = (date.today() + timedelta(days=1)).isoformat()
        q = (
            "SELECT change_event.change_date_time, change_event.user_email, change_event.client_type, "
            "change_event.change_resource_type, change_event.resource_change_operation, "
            "change_event.changed_fields, campaign.name, ad_group.name FROM change_event "
            f"WHERE change_event.change_date_time >= '{start}' AND change_event.change_date_time <= '{end}' "
            f"ORDER BY change_event.change_date_time DESC LIMIT {max_rows}"
        )

        def go(user, grt):
            acct = ads.account(user, grt, customer_id)
            rows, _ = ads.search(grt, acct, q, max_rows)
            return {"account": acct.name, "customer_id": pretty_id(acct.customer_id), "rows": rows}

        return await run("change_history", customer_id, go)

    # ---------------------------------------------------------------- keyword research

    @mcp.tool(annotations=READ_ONLY)
    async def find_locations(
        customer_id: Annotated[str, Field(description="Any account ID you can access (used for the API call).")],
        names: Annotated[list[str], Field(description="Place names, e.g. ['Dubai', 'Pakistan'].")],
        country_code: Annotated[str | None, Field(description="Optional 2-letter country to narrow results, e.g. AE")] = None,
    ) -> dict[str, Any]:
        """Look up Google location IDs (for keyword_ideas and targeting) from place names."""

        def go(user, grt):
            acct = ads.account(user, grt, customer_id)
            client = ads.client(grt, acct.login_customer_id)
            svc = client.get_service("GeoTargetConstantService")
            req = client.get_type("SuggestGeoTargetConstantsRequest")
            req.locale = "en"
            if country_code:
                req.country_code = country_code.upper()
            req.location_names.names.extend(names)
            resp = svc.suggest_geo_target_constants(request=req)
            out = []
            for s in resp.geo_target_constant_suggestions:
                g = s.geo_target_constant
                out.append({
                    "location_id": str(g.id), "name": g.name, "canonical_name": g.canonical_name,
                    "type": g.target_type, "country": g.country_code, "reach": s.reach,
                })
            return {"locations": out}

        return await run("find_locations", customer_id, go)

    @mcp.tool(annotations=READ_ONLY)
    async def keyword_ideas(
        customer_id: Annotated[str, Field(description="Client account ID the research is for.")],
        seed_keywords: Annotated[list[str] | None, Field(description="Up to 20 starting keywords.")] = None,
        page_url: Annotated[str | None, Field(description="A web page to pull ideas from.")] = None,
        location_ids: Annotated[list[str] | None, Field(description="Location IDs from find_locations. Empty = all locations.")] = None,
        language_id: Annotated[str, Field(description="Language constant ID. 1000 = English, 1019 = Arabic, 1041 = Urdu.")] = "1000",
        include_search_partners: bool = False,
        max_ideas: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """Keyword Planner ideas: average monthly searches, competition, and top-of-page bid range
        (in the account's currency) for keywords related to seed words and/or a page URL."""
        if not seed_keywords and not page_url:
            raise ValueError("Give seed_keywords, page_url, or both.")

        def go(user, grt):
            acct = ads.account(user, grt, customer_id)
            client = ads.client(grt, acct.login_customer_id)
            svc = client.get_service("KeywordPlanIdeaService")
            req = client.get_type("GenerateKeywordIdeasRequest")
            req.customer_id = acct.customer_id
            req.language = f"languageConstants/{int(language_id)}"
            for loc in location_ids or []:
                req.geo_target_constants.append(f"geoTargetConstants/{int(loc)}")
            req.include_adult_keywords = False
            net = client.enums.KeywordPlanNetworkEnum
            req.keyword_plan_network = net.GOOGLE_SEARCH_AND_PARTNERS if include_search_partners else net.GOOGLE_SEARCH
            seeds = (seed_keywords or [])[:20]
            if seeds and page_url:
                req.keyword_and_url_seed.url = page_url
                req.keyword_and_url_seed.keywords.extend(seeds)
            elif seeds:
                req.keyword_seed.keywords.extend(seeds)
            else:
                req.url_seed.url = page_url
            try:
                ideas = []
                for idea in svc.generate_keyword_ideas(request=req):
                    m = idea.keyword_idea_metrics
                    ideas.append({
                        "keyword": idea.text,
                        "avg_monthly_searches": m.avg_monthly_searches,
                        "competition": m.competition.name,
                        "competition_index": m.competition_index,
                        "top_of_page_bid_low": round(m.low_top_of_page_bid_micros / 1e6, 2),
                        "top_of_page_bid_high": round(m.high_top_of_page_bid_micros / 1e6, 2),
                    })
                    if len(ideas) >= max_ideas:
                        break
            except GoogleAdsException as e:
                codes = error_codes(e)
                if any(c.startswith(("authorization_error.", "access_level_error.")) or "DEVELOPER_TOKEN" in c for c in codes) or (
                    "access" in describe_ads_exception(e).lower() and "level" in describe_ads_exception(e).lower()
                ):
                    raise AdsError(
                        "Keyword Planner isn't available yet: it needs Basic API access, and this connector's "
                        "Google Cloud project is on Explorer access. Finish brand verification in Google Cloud, "
                        "then re-apply for Basic. Details: " + describe_ads_exception(e)
                    ) from None
                raise
            ideas.sort(key=lambda d: d["avg_monthly_searches"] or 0, reverse=True)
            return {"account": acct.name, "currency": acct.currency, "count": len(ideas), "ideas": ideas}

        return await run("keyword_ideas", customer_id, go)

    # ---------------------------------------------------------------- changes (phase 2)

    mcp.zyppia_writer = register_write_tools(mcp, settings, ads, provider.sealer, run)

    # ---------------------------------------------------------------- plain routes

    @mcp.custom_route("/oauth/google/callback", methods=["GET"])
    async def google_callback(request: Request):
        return await provider.google_callback(request)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request):
        return JSONResponse({"ok": True})

    mcp.zyppia_provider = provider  # exposed for tests
    mcp.zyppia_ads = ads
    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    server = build_server(settings)
    import uvicorn

    uvicorn.run(server.streamable_http_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
