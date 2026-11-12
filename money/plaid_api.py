"""Tiny Plaid REST client (stdlib urllib) + local storage of access tokens.

Access tokens live in secrets/plaid_items.json, which is gitignored and must never be
printed, logged, or read into Claude's context.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from money import config

HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

# Errors that a user fixes by re-authenticating through Link "update mode".
RELINK_CODES = {
    "ITEM_LOGIN_REQUIRED",
    "INVALID_CREDENTIALS",
    "INSUFFICIENT_CREDENTIALS",
    "INVALID_MFA",
    "INVALID_UPDATED_USERNAME",
    "ITEM_LOCKED",
    "USER_SETUP_REQUIRED",
    "MFA_NOT_SUPPORTED",
    "PENDING_EXPIRATION",
    "ACCESS_NOT_GRANTED",
    "NO_ACCOUNTS",
}


class PlaidError(Exception):
    def __init__(self, error_type: str, error_code: str, message: str, display_message: str | None = None):
        super().__init__(f"{error_code}: {message}")
        self.error_type = error_type
        self.error_code = error_code
        self.message = message
        self.display_message = display_message

    @property
    def needs_relink(self) -> bool:
        return self.error_code in RELINK_CODES


class NotConfigured(Exception):
    pass


class PlaidClient:
    def __init__(self, client_id: str, secret: str, env: str = "production", timeout: float = 60):
        if env not in HOSTS:
            raise NotConfigured(f"PLAID_ENV must be one of {sorted(HOSTS)}, got {env!r}")
        self.client_id = client_id
        self.secret = secret
        self.env = env
        self.host = HOSTS[env]
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "PlaidClient":
        s = config.plaid_settings()
        if not s["client_id"] or not s["secret"]:
            raise NotConfigured(
                "Plaid isn't configured. Copy .env.example to .env and set PLAID_CLIENT_ID and "
                "PLAID_SECRET (from dashboard.plaid.com > Developers > Keys). See README.md."
            )
        return cls(s["client_id"], s["secret"], s["env"])

    def post(self, endpoint: str, payload: dict | None = None) -> dict:
        body = {"client_id": self.client_id, "secret": self.secret, **(payload or {})}
        req = urllib.request.Request(
            self.host + endpoint,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Plaid-Version": "2020-09-14"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except Exception:
                raise PlaidError("HTTP_ERROR", str(e.code), str(e)) from None
            raise PlaidError(
                err.get("error_type", "UNKNOWN"),
                err.get("error_code", "UNKNOWN"),
                err.get("error_message", ""),
                err.get("display_message"),
            ) from None

    # --- Link ---------------------------------------------------------------------------

    def create_link_token(self, mode: str = "bank", access_token: str | None = None) -> str:
        payload: dict = {
            "client_name": "Money Tracker",
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": "local-user"},
        }
        if access_token:  # update mode: re-authenticate an existing Item
            payload["access_token"] = access_token
        elif mode == "investments":
            payload["products"] = ["investments"]
        else:  # banks, credit cards, Venmo
            payload["products"] = ["transactions"]
            payload["transactions"] = {"days_requested": 730}
        return self.post("/link/token/create", payload)["link_token"]

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        resp = self.post("/item/public_token/exchange", {"public_token": public_token})
        return resp["access_token"], resp["item_id"]


# --- Local token store (secrets/plaid_items.json) ------------------------------------------


def load_items() -> dict:
    if not config.ITEMS_FILE.exists():
        return {}
    return json.loads(config.ITEMS_FILE.read_text(encoding="utf-8"))


def save_items(items: dict) -> None:
    config.SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.ITEMS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    os.replace(tmp, config.ITEMS_FILE)


def store_item(item_id: str, access_token: str, institution_name: str, institution_id: str | None,
               products: list[str]) -> None:
    items = load_items()
    items[item_id] = {
        "access_token": access_token,
        "institution_name": institution_name,
        "institution_id": institution_id,
        "products": products,
        "env": config.plaid_settings()["env"],
    }
    save_items(items)


def forget_item(item_id: str) -> dict | None:
    items = load_items()
    removed = items.pop(item_id, None)
    save_items(items)
    return removed
