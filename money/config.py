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


def load_env(path: Path = ENV_FILE) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


load_env()


def db_path() -> Path:
    return Path(os.environ.get("MONEY_DB") or DEFAULT_DB)


def use_demo() -> None:
    os.environ["MONEY_DB"] = str(DEMO_DB)


def is_demo() -> bool:
    return db_path().resolve() == DEMO_DB.resolve()


def port() -> int:
    return int(os.environ.get("MONEY_PORT") or 8765)


def plaid_settings() -> dict:
    return {
        "client_id": os.environ.get("PLAID_CLIENT_ID", "").strip(),
        "secret": os.environ.get("PLAID_SECRET", "").strip(),
        "env": (os.environ.get("PLAID_ENV") or "production").strip().lower(),
    }
