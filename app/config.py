"""Settings, read from environment variables.

On Cloud Run these come from the service's variables and Secret Manager.
Locally, put them in a `.env` file (never commit it).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv(value: str | None) -> list[str]:
    return [v.strip().lower() for v in (value or "").split(",") if v.strip()]


def _digits(value: str | None) -> str | None:
    if not value:
        return None
    d = "".join(ch for ch in value if ch.isdigit())
    return d or None


@dataclass(frozen=True)
class Settings:
    # Public https URL of this server, e.g. https://zyppia-ads-xxxx.run.app
    public_url: str
    # Google OAuth "Web application" client from the Cloud project
    google_client_id: str
    google_client_secret: str
    # Secret used to seal tokens. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_key: str
    # Who may sign in. Exact emails and/or "@domain.com" entries.
    allowed_emails: list[str] = field(default_factory=list)
    # Zyppia's manager account. Used as login-customer-id for accounts under it.
    mcc_id: str | None = None
    # No longer needed since Sept 2026, still sent if set.
    developer_token: str | None = None
    # Optional allowlist of customer IDs the server may touch. Empty = any
    # account the signed-in user can access.
    allowed_customer_ids: list[str] = field(default_factory=list)

    # Budget or bid increases bigger than this (percent) need an extra, explicit
    # confirmation before they're applied.
    max_increase_pct: float = 50.0
    # Signed-in people who may only read: they can run reports but not change anything.
    read_only_emails: list[str] = field(default_factory=list)
    # Per-account cap on the total daily budget of all enabled campaigns,
    # {customer_id: amount in the account's currency}. Changes that would go over are refused.
    spend_limits: dict[str, float] = field(default_factory=dict)

    # Google Maps Platform API key for the free local rank tracker (Places API (New) only). Optional.
    places_api_key: str | None = None
    # Which connector this server is: "ads" (Zyppia Google Ads) or "gmb" (Zyppia GMB: Business Profile + rank tracking).
    connector: str = "ads"

    access_token_ttl: int = 3600
    refresh_token_ttl: int = 60 * 60 * 24 * 30

    @property
    def google_redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/oauth/google/callback"

    def email_allowed(self, email: str | None) -> bool:
        if not email:
            return False
        email = email.lower()
        for entry in self.allowed_emails:
            if entry.startswith("@") and email.endswith(entry):
                return True
            if entry == email:
                return True
        return False

    def can_edit(self, email: str | None) -> bool:
        return bool(email) and email.lower() not in self.read_only_emails

    def customer_allowed(self, customer_id: str) -> bool:
        return not self.allowed_customer_ids or customer_id in self.allowed_customer_ids


def _limits(value: str | None) -> dict[str, float]:
    """'399-355-6480=100, 1234567890=50' -> {'3993556480': 100.0, '1234567890': 50.0}"""
    out = {}
    for part in (value or "").split(","):
        if "=" not in part:
            continue
        cid, amount = part.split("=", 1)
        cid = _digits(cid)
        if cid:
            out[cid] = float(amount.strip())
    return out


def load_settings() -> Settings:
    missing = [
        k
        for k in ("PUBLIC_URL", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "TOKEN_KEY", "ALLOWED_EMAILS")
        if not os.environ.get(k)
    ]
    if missing:
        raise RuntimeError(f"Missing required settings: {', '.join(missing)}")
    return Settings(
        public_url=os.environ["PUBLIC_URL"].rstrip("/"),
        google_client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        google_client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        token_key=os.environ["TOKEN_KEY"],
        allowed_emails=_csv(os.environ.get("ALLOWED_EMAILS")),
        mcc_id=_digits(os.environ.get("MCC_ID")),
        developer_token=os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN") or None,
        allowed_customer_ids=[d for d in (_digits(x) for x in _csv(os.environ.get("ALLOWED_CUSTOMER_IDS"))) if d],
        max_increase_pct=float(os.environ.get("MAX_INCREASE_PCT") or 50),
        read_only_emails=_csv(os.environ.get("READ_ONLY_EMAILS")),
        spend_limits=_limits(os.environ.get("SPEND_LIMITS")),
        places_api_key=os.environ.get("PLACES_API_KEY") or None,
        connector=_connector(os.environ.get("CONNECTOR")),
    )


def _connector(value: str | None) -> str:
    v = (value or "ads").strip().lower()
    if v not in ("ads", "gmb"):
        raise RuntimeError(f"CONNECTOR must be 'ads' or 'gmb', not {value!r}")
    return v
