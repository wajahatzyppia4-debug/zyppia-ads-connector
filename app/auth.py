"""OAuth for Claude, backed by "Sign in with Google".

Flow when someone adds the connector in Claude:

1. Claude registers itself (dynamic client registration) and sends the user to
   our /authorize endpoint.
2. We send the user on to Google sign-in, asking for their email, Google
   Ads access and Tag Manager access (offline, so we get a Google refresh token).
3. Google sends them back to /oauth/google/callback. We check the email is on
   the allowlist, then hand Claude an authorization code.
4. Claude swaps the code for our access + refresh tokens.

Each tool call then acts with the signed-in person's own Google Ads access:
accounts under the MCC and any account their login was added to directly.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from urllib.parse import urlencode

import httpx
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import Settings
from .sealing import Sealer

log = logging.getLogger("zyppia.auth")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
ADWORDS_SCOPE = "https://www.googleapis.com/auth/adwords"
# Tag Manager (phase 4): edit containers, save versions, publish. Optional at sign-in:
# without them Google Ads still works and the Tag Manager tools ask to reconnect.
TAGMANAGER_SCOPES = [
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
    "https://www.googleapis.com/auth/tagmanager.publish",
]
# Phase 5, also optional: Google Analytics (setup + reports), Search Console (read only), Merchant Center.
ANALYTICS_SCOPES = [
    "https://www.googleapis.com/auth/analytics.edit",
    "https://www.googleapis.com/auth/analytics.readonly",
]
SEARCH_CONSOLE_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
MERCHANT_SCOPES = ["https://www.googleapis.com/auth/content"]
# Phase 6, optional: Google Business Profile (locations, reviews, posts, photos, insights).
BUSINESS_PROFILE_SCOPES = ["https://www.googleapis.com/auth/business.manage"]
GOOGLE_SCOPES = ["openid", "email", ADWORDS_SCOPE, *TAGMANAGER_SCOPES, *ANALYTICS_SCOPES, *SEARCH_CONSOLE_SCOPES,
                 *MERCHANT_SCOPES, *BUSINESS_PROFILE_SCOPES]

CODE_TTL = 300
STATE_TTL = 900


class ZCode(AuthorizationCode):
    google_refresh_token: str


class ZAccessToken(AccessToken):
    google_refresh_token: str


class ZRefreshToken(RefreshToken):
    google_refresh_token: str


def _decode_jwt_payload(jwt: str) -> dict:
    # The ID token comes straight from Google's token endpoint over TLS,
    # authenticated with our client secret, so its signature need not be
    # re-checked (Google's own guidance for this case).
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _page(title: str, body: str, status: int) -> HTMLResponse:
    html = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{title}</title><body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1 style='font-size:1.3rem'>{title}</h1><p>{body}</p></body>"
    )
    return HTMLResponse(html, status_code=status)


class GoogleBackedOAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider."""

    def __init__(self, settings: Settings, sealer: Sealer | None = None):
        self.settings = settings
        self.sealer = sealer or Sealer(settings.token_key)

    # ---- client registration (stateless: the client ID *is* the sealed record)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        record = {
            "ru": [str(u) for u in (client_info.redirect_uris or [])],
            "am": client_info.token_endpoint_auth_method,
            "gt": client_info.grant_types,
            "cs": client_info.client_secret,
            "sc": client_info.scope,
            "n": client_info.client_name,
            "t": client_info.client_id_issued_at,
        }
        client_info.client_id = self.sealer.seal("client", record, ttl=None)
        log.info("registered client name=%s", client_info.client_name)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        r = self.sealer.unseal("client", client_id)
        if r is None:
            return None
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=r.get("cs"),
            client_id_issued_at=r.get("t"),
            redirect_uris=[AnyUrl(u) for u in r["ru"]],
            token_endpoint_auth_method=r.get("am"),
            grant_types=r.get("gt") or ["authorization_code", "refresh_token"],
            scope=r.get("sc"),
            client_name=r.get("n"),
        )

    # ---- authorize: hand off to Google

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = self.sealer.seal(
            "gstate",
            {
                "cid": client.client_id,
                "ru": str(params.redirect_uri),
                "rue": params.redirect_uri_provided_explicitly,
                "cc": params.code_challenge,
                "st": params.state,
                "sc": params.scopes or [],
                "res": params.resource,
            },
            ttl=STATE_TTL,
        )
        query = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.google_redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "access_type": "offline",
            "prompt": "consent select_account",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(query)}"

    async def _google_token_exchange(self, code: str) -> dict | None:
        async with httpx.AsyncClient(timeout=20) as http:
            resp = await http.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.settings.google_client_id,
                    "client_secret": self.settings.google_client_secret,
                    "redirect_uri": self.settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        if resp.status_code != 200:
            log.warning("google token exchange failed status=%s body=%s", resp.status_code, resp.text[:300])
            return None
        return resp.json()

    async def google_callback(self, request: Request) -> Response:
        if request.query_params.get("error"):
            return _page("Sign-in cancelled", "Google sign-in was cancelled. Close this tab and try again from Claude.", 400)
        st = self.sealer.unseal("gstate", request.query_params.get("state", ""))
        code = request.query_params.get("code")
        if st is None or not code:
            return _page("Sign-in expired", "This sign-in link expired. Start again from Claude.", 400)

        tok = await self._google_token_exchange(code)
        if tok is None:
            return _page("Sign-in failed", "Google did not accept the sign-in. Try again from Claude.", 502)
        claims = _decode_jwt_payload(tok.get("id_token", "..e30"))
        email = (claims.get("email") or "").lower()
        if not claims.get("email_verified") or not self.settings.email_allowed(email):
            log.warning("sign-in refused for email=%s", email or "?")
            return _page(
                "Not allowed",
                f"{email or 'This account'} is not on Zyppia's allowed list for this connector. Ask an admin to add it.",
                403,
            )
        if ADWORDS_SCOPE not in (tok.get("scope") or "").split():
            return _page("Google Ads access not granted", "Please tick the Google Ads permission when signing in. Try again from Claude.", 400)
        grt = tok.get("refresh_token")
        if not grt:
            return _page("Sign-in incomplete", "Google did not return offline access. Try again from Claude.", 400)

        our_code = self.sealer.seal(
            "code",
            {
                "cid": st["cid"],
                "ru": st["ru"],
                "rue": st["rue"],
                "cc": st["cc"],
                "sc": st["sc"],
                "res": st["res"],
                "sub": email,
                "grt": grt,
            },
            ttl=CODE_TTL,
        )
        log.info("sign-in ok email=%s", email)
        return RedirectResponse(construct_redirect_uri(st["ru"], code=our_code, state=st["st"]), status_code=302)

    # ---- codes and tokens

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> ZCode | None:
        c = self.sealer.unseal("code", authorization_code)
        if c is None or c["cid"] != client.client_id:
            return None
        return ZCode(
            code=authorization_code,
            scopes=c["sc"],
            expires_at=time.time() + 60,  # already checked by unseal
            client_id=c["cid"],
            code_challenge=c["cc"],
            redirect_uri=AnyUrl(c["ru"]),
            redirect_uri_provided_explicitly=c["rue"],
            resource=c["res"],
            subject=c["sub"],
            google_refresh_token=c["grt"],
        )

    def _issue(self, client_id: str, email: str, grt: str, scopes: list[str], resource: str | None) -> OAuthToken:
        s = self.settings
        base = {"cid": client_id, "sub": email, "grt": grt, "sc": scopes, "res": resource}
        access = self.sealer.seal("at", base, ttl=s.access_token_ttl)
        refresh = self.sealer.seal("rt", base, ttl=s.refresh_token_ttl)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=s.access_token_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: ZCode) -> OAuthToken:
        if not self.settings.email_allowed(authorization_code.subject):
            raise TokenError("invalid_grant", "user no longer allowed")
        return self._issue(
            client.client_id,
            authorization_code.subject,
            authorization_code.google_refresh_token,
            authorization_code.scopes,
            authorization_code.resource,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> ZRefreshToken | None:
        r = self.sealer.unseal("rt", refresh_token)
        if r is None or r["cid"] != client.client_id or not self.settings.email_allowed(r["sub"]):
            return None
        return ZRefreshToken(
            token=refresh_token,
            client_id=r["cid"],
            scopes=r["sc"],
            resource=r["res"],
            subject=r["sub"],
            google_refresh_token=r["grt"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: ZRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        return self._issue(
            client.client_id,
            refresh_token.subject,
            refresh_token.google_refresh_token,
            scopes or refresh_token.scopes,
            refresh_token.resource,
        )

    async def load_access_token(self, token: str) -> ZAccessToken | None:
        a = self.sealer.unseal("at", token)
        if a is None or not self.settings.email_allowed(a["sub"]):
            return None
        return ZAccessToken(
            token=token,
            client_id=a["cid"],
            scopes=a["sc"],
            resource=a["res"],
            subject=a["sub"],
            google_refresh_token=a["grt"],
        )

    async def revoke_token(self, token) -> None:  # stateless; tokens expire on their own
        return None
