"""SQLite storage. One file (data/money.db), stdlib only.

Sign convention everywhere in this project: amount < 0 = money out, amount > 0 = money in.
(Plaid uses the opposite sign; we flip it on import.)
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

from money import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  item_id          TEXT PRIMARY KEY,
  institution_id   TEXT,
  institution_name TEXT,
  products         TEXT,              -- JSON list, e.g. ["transactions"]
  status           TEXT DEFAULT 'ok', -- ok | login_required | error | removed
  error            TEXT,
  tx_cursor        TEXT,              -- /transactions/sync cursor
  last_synced      TEXT,
  created_at       TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
  account_id        TEXT PRIMARY KEY,
  item_id           TEXT,
  institution       TEXT,
  name              TEXT,
  official_name     TEXT,
  mask              TEXT,
  type              TEXT,   -- depository | credit | investment | loan | other
  subtype           TEXT,
  balance_current   REAL,   -- for credit/loan: amount owed (positive)
  balance_available REAL,
  balance_limit     REAL,
  currency          TEXT,
  hidden            INTEGER NOT NULL DEFAULT 0,
  updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS balance_history (
  account_id        TEXT NOT NULL,
  date              TEXT NOT NULL,
  balance_current   REAL,
  balance_available REAL,
  PRIMARY KEY (account_id, date)
);

CREATE TABLE IF NOT EXISTS transactions (
  transaction_id          TEXT PRIMARY KEY,
  account_id              TEXT NOT NULL,
  date                    TEXT NOT NULL,
  authorized_date         TEXT,
  name                    TEXT,
  merchant_name           TEXT,
  counterparty            TEXT,
  amount                  REAL NOT NULL,  -- negative = money out
  currency                TEXT,
  pending                 INTEGER NOT NULL DEFAULT 0,
  pending_transaction_id  TEXT,
  payment_channel         TEXT,
  plaid_category          TEXT,           -- personal_finance_category.primary
  plaid_category_detailed TEXT,
  category                TEXT,           -- computed by money.rules
  is_transfer             INTEGER NOT NULL DEFAULT 0,  -- computed by money.rules
  raw                     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_id);

CREATE TABLE IF NOT EXISTS overrides (
  transaction_id TEXT PRIMARY KEY,
  category       TEXT,
  is_transfer    INTEGER,
  note           TEXT,
  updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS rules (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  pattern         TEXT NOT NULL,  -- case-insensitive regex on description/merchant/counterparty
  account_pattern TEXT,           -- optional regex on "institution account-name"
  category        TEXT,
  is_transfer     INTEGER,
  created_at      TEXT
);

CREATE TABLE IF NOT EXISTS securities (
  security_id TEXT PRIMARY KEY,
  ticker      TEXT,
  name        TEXT,
  type        TEXT,
  close_price REAL,
  updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS holdings (
  account_id  TEXT NOT NULL,
  security_id TEXT NOT NULL,
  quantity    REAL,
  price       REAL,
  value       REAL,
  cost_basis  REAL,
  currency    TEXT,
  as_of       TEXT,
  PRIMARY KEY (account_id, security_id)
);

CREATE TABLE IF NOT EXISTS networth_history (
  date        TEXT PRIMARY KEY,
  cash        REAL,
  investments REAL,
  credit      REAL,
  loans       REAL,
  net_worth   REAL
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""

VIEWS = """
DROP VIEW IF EXISTS v_transactions;
CREATE VIEW v_transactions AS
SELECT t.transaction_id AS id,
       t.date,
       substr(t.date, 1, 7) AS month,
       a.institution,
       a.name AS account,
       a.type AS account_type,
       COALESCE(NULLIF(t.merchant_name, ''), NULLIF(t.counterparty, ''), t.name) AS payee,
       t.name AS description,
       t.amount,
       COALESCE(o.category, t.category) AS category,
       COALESCE(o.is_transfer, t.is_transfer) AS is_transfer,
       o.note,
       t.pending,
       t.account_id
FROM transactions t
JOIN accounts a ON a.account_id = t.account_id
LEFT JOIN overrides o ON o.transaction_id = t.transaction_id
WHERE a.hidden = 0;

DROP VIEW IF EXISTS v_spending;
CREATE VIEW v_spending AS
SELECT *, -amount AS spend
FROM v_transactions
WHERE is_transfer = 0 AND category <> 'Income';

DROP VIEW IF EXISTS v_income;
CREATE VIEW v_income AS
SELECT *, amount AS income
FROM v_transactions
WHERE is_transfer = 0 AND category = 'Income';

DROP VIEW IF EXISTS v_accounts;
CREATE VIEW v_accounts AS
SELECT a.account_id, a.institution, a.name, a.mask, a.type, a.subtype,
       a.balance_current, a.balance_available, a.balance_limit,
       CASE WHEN a.type IN ('credit', 'loan') THEN -COALESCE(a.balance_current, 0)
            ELSE COALESCE(a.balance_current, 0) END AS net_value,
       a.updated_at, i.status AS item_status
FROM accounts a
LEFT JOIN items i ON i.item_id = a.item_id
WHERE a.hidden = 0;

DROP VIEW IF EXISTS v_holdings;
CREATE VIEW v_holdings AS
SELECT h.account_id, a.institution, a.name AS account,
       s.ticker, s.name AS security, s.type AS security_type,
       h.quantity, h.price, h.value, h.cost_basis,
       CASE WHEN h.cost_basis IS NOT NULL THEN h.value - h.cost_basis END AS gain,
       h.as_of
FROM holdings h
JOIN accounts a ON a.account_id = h.account_id
LEFT JOIN securities s ON s.security_id = h.security_id
WHERE a.hidden = 0;
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().date().isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Writable connection; creates the schema and refreshes the views."""
    path = Path(path or config.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.executescript(VIEWS)
    conn.commit()
    return conn


def connect_readonly(path: Path | None = None, max_seconds: float = 3.0) -> sqlite3.Connection:
    """Read-only connection for ad-hoc queries and dashboard views.

    Writes are impossible (mode=ro + query_only) and runaway queries are aborted.
    """
    path = Path(path or config.db_path())
    if not path.exists():
        connect(path).close()
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    set_deadline(conn, max_seconds)
    return conn


def set_deadline(conn: sqlite3.Connection, seconds: float) -> None:
    """Abort any statement on this connection that runs past `seconds` from now."""
    deadline = time.monotonic() + seconds
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def has_data(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None
