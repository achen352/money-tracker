"""Paths and settings. Secrets come from .env (never committed)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"          # gitignored: database + Claude-made views
SECRETS_DIR = ROOT / "secrets"    # gitignored: Plaid access tokens (Claude must never read)
VIEWS_DIR = DATA_DIR / "views"
WEB_DIR = Path(__file__).resolve().parent / "web"
ENV_FILE = ROOT / ".env"
ITEMS_FILE = SECRETS_DIR / "plaid_items.json"

DEFAULT_DB = DATA_DIR / "money.db"
DEMO_DB = DATA_DIR / "demo.db"


_EXTERNAL_ENV = set(os.environ)  # variables set outside .env, which always win


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse .env (KEY=VALUE lines, # comments, optional quotes)."""
    path = path or ENV_FILE
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.split(" #", 1)[0].strip().strip('"').strip("'")
    return values


def load_env(path: Path | None = None) -> None:
    for key, value in read_env_file(path).items():
        os.environ.setdefault(key, value)


load_env()


def setting(key: str, default: str = "") -> str:
    """A setting from the real environment, else from .env read fresh, so editing .env takes
    effect without restarting the long-running dashboard server."""
    if key in _EXTERNAL_ENV:
        return os.environ.get(key, default)
    return read_env_file().get(key, default)


def db_path() -> Path:
    return Path(os.environ.get("MONEY_DB") or DEFAULT_DB)


def use_demo() -> None:
    os.environ["MONEY_DB"] = str(DEMO_DB)


def is_demo() -> bool:
    return db_path().resolve() == DEMO_DB.resolve()


def port() -> int:
    return int(setting("MONEY_PORT") or 8765)


def plaid_settings() -> dict:
    return {
        "client_id": setting("PLAID_CLIENT_ID").strip(),
        "secret": setting("PLAID_SECRET").strip(),
        "env": (setting("PLAID_ENV") or "production").strip().lower(),
    }
