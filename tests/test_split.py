"""The two connectors built from this code: Zyppia Google Ads (CONNECTOR=ads) and Zyppia GMB (CONNECTOR=gmb)."""

import pytest

from app.config import load_settings
from tests.test_flow import make_settings, mcp_call, sign_in
from app.server import build_server

ADWORDS = "https://www.googleapis.com/auth/adwords"
GBP = "https://www.googleapis.com/auth/business.manage"
GMB_TOOLS = {
    "apply_change", "apply_changes", "undo_change",
    "gbp_list_locations", "gbp_profile_audit", "gbp_reviews", "gbp_posts", "gbp_insights", "gbp_find_categories",
    "gbp_reply_review", "gbp_create_post", "gbp_delete_post", "gbp_add_photo", "gbp_update_profile",
    "gbp_advanced_change", "rank_grid_scan", "rank_find_business",
}


async def tool_names(server):
    return {t.name for t in await server.list_tools()}


@pytest.mark.asyncio
async def test_ads_connector_has_no_gmb_tools():
    server = build_server(make_settings())
    names = await tool_names(server)
    assert server.name == "Zyppia Google Ads"
    assert not any(n.startswith(("gbp_", "rank_")) for n in names)
    assert {"list_accounts", "apply_change", "account_health_check", "gtm_publish", "ga4_report"} <= names
    assert "Business Profile" not in server.instructions


@pytest.mark.asyncio
async def test_gmb_connector_has_only_gmb_tools():
    server = build_server(make_settings(connector="gmb"))
    assert server.name == "Zyppia GMB"
    assert await tool_names(server) == GMB_TOOLS
    assert "rank_grid_scan" in server.instructions and "list_accounts" not in server.instructions


@pytest.mark.asyncio
async def test_each_connector_asks_google_for_its_own_permission():
    _, tokens = await sign_in(build_server(make_settings()), "wajahat@zyppia.test")
    assert tokens
    gmb = build_server(make_settings(connector="gmb"))
    _, tokens = await sign_in(gmb, "wajahat@zyppia.test", scope=f"openid email {GBP}", asks=GBP)
    assert tokens
    r = await mcp_call(gmb, tokens["access_token"], "tools/list")
    assert {t["name"] for t in r.json()["result"]["tools"]} == GMB_TOOLS


@pytest.mark.asyncio
async def test_gmb_sign_in_without_business_profile_permission_is_refused():
    gmb = build_server(make_settings(connector="gmb"))
    cb, tokens = await sign_in(gmb, "wajahat@zyppia.test", scope=f"openid email {ADWORDS}", asks=GBP)
    assert cb.status_code == 400 and "Business Profile" in cb.text and tokens is None


def test_connector_setting(monkeypatch):
    for k, v in {"PUBLIC_URL": "https://x", "GOOGLE_OAUTH_CLIENT_ID": "i", "GOOGLE_OAUTH_CLIENT_SECRET": "s",
                 "TOKEN_KEY": "k", "ALLOWED_EMAILS": "a@b.c"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CONNECTOR", raising=False)
    assert load_settings().connector == "ads"
    monkeypatch.setenv("CONNECTOR", " GMB ")
    assert load_settings().connector == "gmb"
    monkeypatch.setenv("CONNECTOR", "both")
    with pytest.raises(RuntimeError, match="CONNECTOR"):
        load_settings()
