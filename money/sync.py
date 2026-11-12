"""Pull data from Plaid into SQLite: accounts/balances, transactions (/transactions/sync), holdings."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field

from money import db, plaid_api, rules
from money.plaid_api import PlaidClient, PlaidError

_lock = threading.Lock()  # one sync at a time (CLI and dashboard share this module)


@dataclass
class ItemResult:
    item_id: str
    institution: str
    added: int = 0
    modified: int = 0
    removed: int = 0
    accounts: int = 0
    holdings: int = 0
    status: str = "ok"
    message: str = ""
    notes: list[str] = field(default_factory=list)

    def line(self) -> str:
        if self.status != "ok":
            return f"{self.institution}: {self.status.upper()} - {self.message}"
        parts = [f"{self.accounts} accounts"]
        if self.added or self.modified or self.removed:
            parts.append(f"+{self.added} new, {self.modified} updated, {self.removed} removed transactions")
        else:
            parts.append("no new transactions")
        if self.holdings:
            parts.append(f"{self.holdings} holdings")
        return f"{self.institution}: " + ", ".join(parts) + "".join(f" ({n})" for n in self.notes)


def _counterparty(tx: dict) -> str | None:
    names = [c.get("name") for c in tx.get("counterparties") or [] if c.get("name")]
    return ", ".join(names) or None


def upsert_accounts(conn: sqlite3.Connection, item_id: str, institution: str, accounts: list[dict]) -> int:
    today = db.today()
    for a in accounts:
        bal = a.get("balances") or {}
        conn.execute(
            """INSERT INTO accounts(account_id, item_id, institution, name, official_name, mask, type, subtype,
                                    balance_current, balance_available, balance_limit, currency, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET
                 item_id=excluded.item_id, institution=excluded.institution, name=excluded.name,
                 official_name=excluded.official_name, mask=excluded.mask, type=excluded.type,
                 subtype=excluded.subtype, balance_current=excluded.balance_current,
                 balance_available=excluded.balance_available, balance_limit=excluded.balance_limit,
                 currency=excluded.currency, updated_at=excluded.updated_at""",
            (
                a["account_id"], item_id, institution, a.get("name"), a.get("official_name"), a.get("mask"),
                a.get("type"), a.get("subtype"), bal.get("current"), bal.get("available"), bal.get("limit"),
                bal.get("iso_currency_code") or bal.get("unofficial_currency_code"), db.now_iso(),
            ),
        )
        conn.execute(
            """INSERT INTO balance_history(account_id, date, balance_current, balance_available)
               VALUES (?,?,?,?)
               ON CONFLICT(account_id, date) DO UPDATE SET
                 balance_current=excluded.balance_current, balance_available=excluded.balance_available""",
            (a["account_id"], today, bal.get("current"), bal.get("available")),
        )
    return len(accounts)


def upsert_transactions(conn: sqlite3.Connection, txs: list[dict]) -> None:
    for t in txs:
        pfc = t.get("personal_finance_category") or {}
        # Pending -> posted: carry any manual override over to the posted transaction.
        if t.get("pending_transaction_id"):
            conn.execute(
                """INSERT OR IGNORE INTO overrides(transaction_id, category, is_transfer, note, updated_at)
                   SELECT ?, category, is_transfer, note, updated_at FROM overrides WHERE transaction_id = ?""",
                (t["transaction_id"], t["pending_transaction_id"]),
            )
        conn.execute(
            """INSERT INTO transactions(transaction_id, account_id, date, authorized_date, name, merchant_name,
                                        counterparty, amount, currency, pending, pending_transaction_id,
                                        payment_channel, plaid_category, plaid_category_detailed, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(transaction_id) DO UPDATE SET
                 account_id=excluded.account_id, date=excluded.date, authorized_date=excluded.authorized_date,
                 name=excluded.name, merchant_name=excluded.merchant_name, counterparty=excluded.counterparty,
                 amount=excluded.amount, currency=excluded.currency, pending=excluded.pending,
                 pending_transaction_id=excluded.pending_transaction_id, payment_channel=excluded.payment_channel,
                 plaid_category=excluded.plaid_category, plaid_category_detailed=excluded.plaid_category_detailed,
                 raw=excluded.raw""",
            (
                t["transaction_id"], t["account_id"], t.get("date"), t.get("authorized_date"), t.get("name"),
                t.get("merchant_name"), _counterparty(t),
                round(-float(t["amount"]), 2),  # Plaid: positive = money out. Ours: negative = money out.
                t.get("iso_currency_code") or t.get("unofficial_currency_code"),
                1 if t.get("pending") else 0, t.get("pending_transaction_id"), t.get("payment_channel"),
                pfc.get("primary"), pfc.get("detailed"), json.dumps(t, separators=(",", ":")),
            ),
        )


def sync_transactions(conn: sqlite3.Connection, client: PlaidClient, access_token: str, item_id: str,
                      result: ItemResult) -> None:
    start_cursor = conn.execute("SELECT tx_cursor FROM items WHERE item_id = ?", (item_id,)).fetchone()[0]
    for _attempt in range(3):
        cursor = start_cursor
        added, modified, removed = [], [], []
        status = None
        try:
            while True:
                req = {"access_token": access_token, "count": 500}
                if cursor:
                    req["cursor"] = cursor
                resp = client.post("/transactions/sync", req)
                added += resp.get("added", [])
                modified += resp.get("modified", [])
                removed += resp.get("removed", [])
                cursor = resp.get("next_cursor") or cursor
                status = resp.get("transactions_update_status")
                if not resp.get("has_more"):
                    break
            break
        except PlaidError as e:
            if e.error_code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                continue  # restart pagination from the saved cursor
            raise
    else:
        raise PlaidError("API_ERROR", "SYNC_RETRIES_EXHAUSTED", "transactions kept changing during sync")

    upsert_transactions(conn, added + modified)
    removed_ids = [(r["transaction_id"],) for r in removed if r.get("transaction_id")]
    conn.executemany("DELETE FROM transactions WHERE transaction_id = ?", removed_ids)
    conn.execute("UPDATE items SET tx_cursor = ? WHERE item_id = ?", (cursor, item_id))
    result.added, result.modified, result.removed = len(added), len(modified), len(removed_ids)
    if status in ("NOT_READY", "INITIAL_UPDATE_COMPLETE"):
        result.notes.append("Plaid is still pulling older history - sync again in a few minutes")


def sync_holdings(conn: sqlite3.Connection, client: PlaidClient, access_token: str, result: ItemResult) -> None:
    resp = client.post("/investments/holdings/get", {"access_token": access_token})
    for s in resp.get("securities", []):
        conn.execute(
            """INSERT INTO securities(security_id, ticker, name, type, close_price, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(security_id) DO UPDATE SET ticker=excluded.ticker, name=excluded.name,
                 type=excluded.type, close_price=excluded.close_price, updated_at=excluded.updated_at""",
            (s["security_id"], s.get("ticker_symbol"), s.get("name"), s.get("type"), s.get("close_price"),
             db.now_iso()),
        )
    account_ids = [a["account_id"] for a in resp.get("accounts", [])]
    conn.executemany("DELETE FROM holdings WHERE account_id = ?", [(a,) for a in account_ids])
    for h in resp.get("holdings", []):
        conn.execute(
            """INSERT OR REPLACE INTO holdings(account_id, security_id, quantity, price, value, cost_basis,
                                              currency, as_of)
               VALUES (?,?,?,?,?,?,?,?)""",
            (h["account_id"], h["security_id"], h.get("quantity"), h.get("institution_price"),
             h.get("institution_value"), h.get("cost_basis"), h.get("iso_currency_code"),
             h.get("institution_price_as_of") or db.today()),
        )
    result.holdings = len(resp.get("holdings", []))


def ensure_item_row(conn: sqlite3.Connection, item_id: str, info: dict) -> None:
    conn.execute(
        """INSERT INTO items(item_id, institution_id, institution_name, products, status, created_at)
           VALUES (?,?,?,?, 'ok', ?)
           ON CONFLICT(item_id) DO UPDATE SET institution_name=excluded.institution_name,
             institution_id=excluded.institution_id, products=excluded.products""",
        (item_id, info.get("institution_id"), info.get("institution_name"), json.dumps(info.get("products", [])),
         db.now_iso()),
    )


def sync_item(conn: sqlite3.Connection, client: PlaidClient, item_id: str, info: dict) -> ItemResult:
    institution = info.get("institution_name") or item_id
    result = ItemResult(item_id, institution)
    ensure_item_row(conn, item_id, info)
    token = info["access_token"]
    products = info.get("products") or ["transactions"]
    try:
        accounts = client.post("/accounts/get", {"access_token": token})["accounts"]
        result.accounts = upsert_accounts(conn, item_id, institution, accounts)
        if "transactions" in products:
            sync_transactions(conn, client, token, item_id, result)
        if "investments" in products:
            try:
                sync_holdings(conn, client, token, result)
            except PlaidError as e:
                if e.needs_relink:
                    raise
                result.notes.append(f"holdings unavailable: {e.error_code}")
        conn.execute(
            "UPDATE items SET status = 'ok', error = NULL, last_synced = ? WHERE item_id = ?",
            (db.now_iso(), item_id),
        )
    except PlaidError as e:
        result.status = "login_required" if e.needs_relink else "error"
        result.message = e.display_message or e.message or e.error_code
        if e.needs_relink:
            result.message += " - reconnect with: python -m money link --update " + item_id
        conn.execute(
            "UPDATE items SET status = ?, error = ? WHERE item_id = ?",
            (result.status, f"{e.error_code}: {e.message}", item_id),
        )
    conn.commit()
    return result


def sync_all(only_item: str | None = None, client: PlaidClient | None = None,
             conn: sqlite3.Connection | None = None) -> list[ItemResult]:
    if not _lock.acquire(blocking=False):
        raise RuntimeError("A sync is already running.")
    try:
        items = plaid_api.load_items()
        if only_item:
            items = {k: v for k, v in items.items() if k == only_item}
        if not items:
            return []
        client = client or PlaidClient.from_env()
        own_conn = conn is None
        conn = conn or db.connect()
        try:
            results = [sync_item(conn, client, item_id, info) for item_id, info in items.items()]
            rules.refresh_derived(conn)
            db.set_meta(conn, "last_sync", db.now_iso())
            conn.commit()
        finally:
            if own_conn:
                conn.close()
        return results
    finally:
        _lock.release()


def is_syncing() -> bool:
    return _lock.locked()
