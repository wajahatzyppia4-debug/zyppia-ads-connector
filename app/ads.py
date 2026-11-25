"""Thin layer over the Google Ads API: clients, account discovery, queries."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.oauth2.credentials import Credentials

from .config import Settings

API_VERSION = "v26"
TOKEN_URI = "https://oauth2.googleapis.com/token"
ACCOUNTS_CACHE_SECONDS = 600

# Money metrics Google returns in micros without a "_micros" suffix.
MICROS_WITHOUT_SUFFIX = {
    "metrics.average_cpc", "metrics.average_cpm", "metrics.average_cost", "metrics.average_cpe",
    "metrics.average_cpv", "metrics.cost_per_conversion", "metrics.cost_per_all_conversions",
    "metrics.cost_per_current_model_attributed_conversion",
}


class AdsError(Exception):
    """An error safe to show to the user."""


def clean_customer_id(value: str | int) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) != 10:
        raise AdsError(f"'{value}' is not a valid Google Ads customer ID (expected 10 digits like 123-456-7890).")
    return digits


def pretty_id(cid: str) -> str:
    return f"{cid[0:3]}-{cid[3:6]}-{cid[6:10]}"


def describe_ads_exception(ex: GoogleAdsException) -> str:
    parts = []
    for err in ex.failure.errors:
        code = err.error_code
        which = code._pb.WhichOneof("error_code") if hasattr(code, "_pb") else None
        val = getattr(code, which).name if which else "UNKNOWN"
        parts.append(f"{which}={val}: {err.message}")
    return f"Google Ads API error (request {ex.request_id}): " + "; ".join(parts)


def error_codes(ex: GoogleAdsException) -> set[str]:
    out = set()
    for err in ex.failure.errors:
        code = err.error_code
        which = code._pb.WhichOneof("error_code")
        if which:
            out.add(f"{which}.{getattr(code, which).name}")
    return out


@dataclass
class Account:
    customer_id: str
    name: str
    currency: str
    time_zone: str
    manager: bool
    status: str
    login_customer_id: str
    route: str  # "direct access" or "via MCC 123-456-7890"
    test_account: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "customer_id": pretty_id(self.customer_id),
            "name": self.name,
            "currency": self.currency,
            "time_zone": self.time_zone,
            "is_manager": self.manager,
            "status": self.status,
            "access": self.route,
            "test_account": self.test_account,
        }


class _LRU(OrderedDict):
    def __init__(self, size: int):
        super().__init__()
        self.size = size
        self.lock = threading.Lock()

    def get_or(self, key, factory):
        with self.lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
        value = factory()
        with self.lock:
            self[key] = value
            while len(self) > self.size:
                self.popitem(last=False)
        return value


class AdsGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._creds = _LRU(64)
        self._accounts: dict[str, tuple[float, dict[str, Account]]] = {}
        self._types: GoogleAdsClient | None = None

    # ---- clients

    def _credentials(self, google_refresh_token: str) -> Credentials:
        key = hashlib.sha256(google_refresh_token.encode()).hexdigest()
        return self._creds.get_or(
            key,
            lambda: Credentials(
                token=None,
                refresh_token=google_refresh_token,
                client_id=self.settings.google_client_id,
                client_secret=self.settings.google_client_secret,
                token_uri=TOKEN_URI,
            ),
        )

    def client(self, google_refresh_token: str, login_customer_id: str | None) -> GoogleAdsClient:
        return GoogleAdsClient(
            credentials=self._credentials(google_refresh_token),
            developer_token=self.settings.developer_token,
            login_customer_id=login_customer_id,
            version=API_VERSION,
            use_proto_plus=True,
        )

    # ---- account discovery

    def accounts(self, user: str, grt: str, refresh: bool = False) -> dict[str, Account]:
        cached = self._accounts.get(user)
        if cached and not refresh and time.time() - cached[0] < ACCOUNTS_CACHE_SECONDS:
            return cached[1]
        found = self._discover(grt)
        self._accounts[user] = (time.time(), found)
        return found

    def _discover(self, grt: str) -> dict[str, Account]:
        base = self.client(grt, None)
        resp = base.get_service("CustomerService").list_accessible_customers()
        direct = [rn.split("/")[-1] for rn in resp.resource_names]
        # Query the MCC first so its clients are routed through it.
        mcc = self.settings.mcc_id
        roots = sorted(direct, key=lambda c: 0 if c == mcc else 1)

        found: dict[str, Account] = {}
        query = (
            "SELECT customer_client.id, customer_client.descriptive_name, customer_client.currency_code, "
            "customer_client.time_zone, customer_client.manager, customer_client.status, "
            "customer_client.level, customer_client.test_account FROM customer_client"
        )
        for root in roots:
            try:
                rows = self.client(grt, root).get_service("GoogleAdsService").search(customer_id=root, query=query)
                for row in rows:
                    cc = row.customer_client
                    cid = str(cc.id)
                    is_root = cid == root
                    if cid in found and not (is_root and found[cid].route != "direct access"):
                        continue
                    if is_root:
                        login, route = root, "direct access"
                    else:
                        login, route = root, f"via manager {pretty_id(root)}"
                    found[cid] = Account(
                        customer_id=cid,
                        name=cc.descriptive_name or "(no name)",
                        currency=cc.currency_code,
                        time_zone=cc.time_zone,
                        manager=bool(cc.manager),
                        status=cc.status.name,
                        login_customer_id=login,
                        route=route,
                        test_account=bool(cc.test_account),
                    )
            except GoogleAdsException:
                # Cancelled / not-yet-enabled accounts can refuse queries; list them anyway.
                found.setdefault(
                    root,
                    Account(root, "(unavailable)", "", "", False, "UNAVAILABLE", root, "direct access"),
                )
        return found

    def account(self, user: str, grt: str, customer_id: str) -> Account:
        cid = clean_customer_id(customer_id)
        if not self.settings.customer_allowed(cid):
            raise AdsError(f"Account {pretty_id(cid)} is not on this connector's allowed account list.")
        accts = self.accounts(user, grt)
        if cid not in accts:
            accts = self.accounts(user, grt, refresh=True)
        if cid not in accts:
            raise AdsError(
                f"Your Google login can't access account {pretty_id(cid)}. "
                "Check the ID, or ask for your login to be added to that account or linked under the MCC."
            )
        return accts[cid]

    def types(self) -> GoogleAdsClient:
        """A client used only to build request objects and enums (it never calls Google)."""
        if self._types is None:
            self._types = GoogleAdsClient(
                credentials=Credentials(token="unused"), developer_token=None, version=API_VERSION, use_proto_plus=True
            )
        return self._types

    def mutate(self, grt: str, acct: Account, operations: list[Any], validate_only: bool) -> Any:
        """Run MutateOperations atomically (all or nothing). validate_only=True checks without changing."""
        client = self.client(grt, acct.login_customer_id)
        req = client.get_type("MutateGoogleAdsRequest")
        req.customer_id = acct.customer_id
        req.mutate_operations.extend(operations)
        req.partial_failure = False
        req.validate_only = validate_only
        return client.get_service("GoogleAdsService").mutate(request=req)

    # ---- queries

    def search_raw(self, grt: str, acct: Account, query: str) -> list[Any]:
        """Rows as API objects (for copying values exactly, e.g. to build an undo)."""
        svc = self.client(grt, acct.login_customer_id).get_service("GoogleAdsService")
        return search_raw_rows(svc, acct.customer_id, query)

    def search(self, grt: str, acct: Account, query: str, max_rows: int) -> tuple[list[dict[str, Any]], bool]:
        svc = self.client(grt, acct.login_customer_id).get_service("GoogleAdsService")
        rows: list[dict[str, Any]] = []
        truncated = False
        stream = svc.search_stream(customer_id=acct.customer_id, query=query)
        for batch in stream:
            paths = list(batch.field_mask.paths)
            for row in batch.results:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append(row_to_dict(row, paths))
            if truncated:
                break
        return rows, truncated


def search_raw_rows(svc: Any, customer_id: str, query: str) -> list[Any]:
    return [row for batch in svc.search_stream(customer_id=customer_id, query=query) for row in batch.results]


def _get_path(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _plain(value: Any) -> Any:
    if hasattr(value, "name") and hasattr(value, "value") and isinstance(getattr(value, "value"), int):
        return value.name  # proto-plus enum
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "_pb"):
        return type(value).to_dict(value, preserving_proto_field_name=True)
    if isinstance(value, Iterable):
        return [_plain(v) for v in value]
    return str(value)


def row_to_dict(row: Any, paths: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in paths:
        try:
            v = _plain(_get_path(row, p))
        except AttributeError:
            continue
        if p in MICROS_WITHOUT_SUFFIX and isinstance(v, (int, float)):
            v = round(v / 1_000_000, 2)
        out[p] = v
        if p.endswith("_micros") and isinstance(v, (int, float)):
            out[p[: -len("_micros")]] = round(v / 1_000_000, 2)
    return out
