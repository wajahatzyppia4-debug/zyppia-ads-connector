"""End-to-end test of the Claude -> connector OAuth flow and a tool call.

Google is faked: no network or real credentials needed.
"""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from app.ads import Account
from app.config import Settings
from app.server import build_server

BASE = "https://ads.example.test"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def make_settings(**kw):
    return Settings(
        public_url=BASE,
        google_client_id="gid",
        google_client_secret="gsecret",
        token_key=Fernet.generate_key().decode(),
        allowed_emails=["wajahat@zyppia.test", "@team.zyppia.test"],
        mcc_id="3476426639",
        **kw,
    )


def fake_id_token(email, verified=True):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email, "email_verified": verified}).encode()).decode().rstrip("=")
    return f"x.{payload}.y"


@pytest.fixture
def server():
    return build_server(make_settings())


def fresh_app(server):
    # Each ASGI lifespan needs its own session manager.
    server._session_manager = None
    return server.streamable_http_app()


async def sign_in(server, email, scope="openid email https://www.googleapis.com/auth/adwords",
                  asks="https://www.googleapis.com/auth/adwords"):
    provider = server.zyppia_provider

    async def fake_exchange(code):
        return {"id_token": fake_id_token(email), "refresh_token": "google-rt-" + email, "scope": scope}

    provider._google_token_exchange = fake_exchange
    app = fresh_app(server)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            # discovery
            prm = (await c.get("/.well-known/oauth-protected-resource/mcp")).json()
            assert prm["authorization_servers"]
            meta = (await c.get("/.well-known/oauth-authorization-server")).json()
            assert meta["registration_endpoint"].endswith("/register")

            # dynamic client registration
            reg = await c.post("/register", json={
                "redirect_uris": [REDIRECT], "client_name": "Claude",
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            })
            assert reg.status_code == 201, reg.text
            client = reg.json()

            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            auth = await c.get("/authorize", params={
                "client_id": client["client_id"], "redirect_uri": REDIRECT, "response_type": "code",
                "code_challenge": challenge, "code_challenge_method": "S256", "state": "abc", "scope": "ads",
            })
            assert auth.status_code == 302, auth.text
            google = urlparse(auth.headers["location"])
            assert google.netloc == "accounts.google.com"
            gq = parse_qs(google.query)
            assert asks in gq["scope"][0].split()
            assert gq["redirect_uri"][0] == BASE + "/oauth/google/callback"

            cb = await c.get("/oauth/google/callback", params={"code": "g-code", "state": gq["state"][0]})
            if cb.status_code != 302:
                return cb, None
            back = urlparse(cb.headers["location"])
            bq = parse_qs(back.query)
            assert back.geturl().startswith(REDIRECT) and bq["state"] == ["abc"]

            tok = await c.post("/token", data={
                "grant_type": "authorization_code", "code": bq["code"][0], "redirect_uri": REDIRECT,
                "client_id": client["client_id"], "client_secret": client["client_secret"], "code_verifier": verifier,
            })
            assert tok.status_code == 200, tok.text
            tokens = tok.json()

            ref = await c.post("/token", data={
                "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"], "client_secret": client["client_secret"],
            })
            assert ref.status_code == 200, ref.text
            return cb, ref.json()


async def mcp_call(server, token, method, params=None):
    app = fresh_app(server)
    transport = httpx.ASGITransport(app=app)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=BASE, headers=headers) as c:
            init = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
            if init.status_code != 200:
                return init
            return await c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}})


@pytest.mark.asyncio
async def test_full_sign_in_and_tool_call(server):
    _, tokens = await sign_in(server, "wajahat@zyppia.test")
    assert tokens and tokens["access_token"]

    r = await mcp_call(server, tokens["access_token"], "tools/list")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"list_accounts", "run_gaql", "performance_report", "keyword_ideas", "find_locations", "change_history"} <= names
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert {"set_status", "update_budget", "create_search_campaign", "apply_change"} <= names
    assert tools["apply_change"]["annotations"]["destructiveHint"] is True
    assert tools["apply_changes"]["annotations"]["destructiveHint"] is True
    assert all(t["annotations"]["readOnlyHint"] for n, t in tools.items() if n not in ("apply_change", "apply_changes"))

    seen = {}

    def fake_accounts(user, grt, refresh=False):
        seen["user"], seen["grt"] = user, grt
        return {
            "3993556480": Account("3993556480", "DXB - Travel And Tourism", "PKR", "Asia/Karachi", False, "ENABLED", "3476426639", "via manager 347-642-6639"),
            "1112223333": Account("1112223333", "Direct Client", "AED", "Asia/Dubai", False, "ENABLED", "1112223333", "direct access"),
        }

    server.zyppia_ads.accounts = fake_accounts
    r = await mcp_call(server, tokens["access_token"], "tools/call", {"name": "list_accounts", "arguments": {}})
    body = r.json()["result"]
    data = body.get("structuredContent") or json.loads(body["content"][0]["text"])
    assert seen == {"user": "wajahat@zyppia.test", "grt": "google-rt-wajahat@zyppia.test"}
    assert data["count"] == 2
    assert {a["access"] for a in data["accounts"]} == {"via manager 347-642-6639", "direct access"}


@pytest.mark.asyncio
async def test_domain_allowlist(server):
    _, tokens = await sign_in(server, "someone@team.zyppia.test")
    assert tokens


@pytest.mark.asyncio
async def test_unlisted_email_is_refused(server):
    cb, tokens = await sign_in(server, "stranger@gmail.com")
    assert cb.status_code == 403 and tokens is None


@pytest.mark.asyncio
async def test_missing_ads_scope_is_refused(server):
    cb, tokens = await sign_in(server, "wajahat@zyppia.test", scope="openid email")
    assert cb.status_code == 400 and tokens is None


@pytest.mark.asyncio
async def test_no_token_is_401(server):
    r = await mcp_call(server, None, "tools/list")
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


@pytest.mark.asyncio
async def test_tokens_from_other_key_rejected(server):
    _, tokens = await sign_in(server, "wajahat@zyppia.test")
    other = build_server(make_settings())
    r = await mcp_call(other, tokens["access_token"], "tools/list")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_preview_then_apply_over_mcp(server):
    from tests.test_writes import BUDGET_ROWS, FakeAds

    _, tokens = await sign_in(server, "wajahat@zyppia.test")
    fake = FakeAds(server.zyppia_ads.settings, [BUDGET_ROWS])
    w = server.zyppia_writer
    w.ads = fake
    acct = Account("3993556480", "DXB", "AED", "Asia/Dubai", False, "ENABLED", "3476426639", "via manager")
    server.zyppia_ads.account = lambda user, grt, cid: acct

    def result(r):
        body = r.json()["result"]
        assert not body.get("isError"), body
        return body.get("structuredContent") or json.loads(body["content"][0]["text"])

    r = await mcp_call(server, tokens["access_token"], "tools/call",
                       {"name": "update_budget", "arguments": {"customer_id": "399-355-6480", "campaign_id": "11", "daily_amount": 22}})
    prev = result(r)
    assert prev["confirm_token"] and fake.calls[-1][0] is True
    r = await mcp_call(server, tokens["access_token"], "tools/call",
                       {"name": "apply_change", "arguments": {"confirm_token": prev["confirm_token"]}})
    assert result(r)["status"] == "done" and fake.calls[-1][0] is False


@pytest.mark.asyncio
async def test_apply_many_and_new_tools_listed(server):
    from tests.test_writes import BUDGET_ROWS, FakeAds

    _, tokens = await sign_in(server, "wajahat@zyppia.test")
    fake = FakeAds(server.zyppia_ads.settings, [BUDGET_ROWS])
    server.zyppia_writer.ads = fake
    acct = Account("3993556480", "DXB", "AED", "Asia/Dubai", False, "ENABLED", "3476426639", "via manager")
    server.zyppia_ads.account = lambda user, grt, cid: acct

    r = await mcp_call(server, tokens["access_token"], "tools/list")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"account_health_check", "website_to_campaign_plan", "add_sitelinks", "add_callouts", "add_call_button",
            "add_structured_snippet", "set_ad_schedule", "set_bid_adjustments", "undo_change", "apply_changes"} <= names

    toks = []
    for amount in (21, 22):
        r = await mcp_call(server, tokens["access_token"], "tools/call", {"name": "update_budget", "arguments": {
            "customer_id": "399-355-6480", "campaign_id": "11", "daily_amount": amount}})
        body = r.json()["result"]
        toks.append((body.get("structuredContent") or json.loads(body["content"][0]["text"]))["confirm_token"])
    r = await mcp_call(server, tokens["access_token"], "tools/call", {"name": "apply_changes", "arguments": {"confirm_tokens": toks}})
    body = r.json()["result"]
    out = body.get("structuredContent") or json.loads(body["content"][0]["text"])
    assert out["done"] == 2 and out["failed"] == 0
