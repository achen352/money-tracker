"""Categorization + transfer detection, and derived tables.

Order of precedence (lowest -> highest):
  1. Plaid's personal_finance_category, mapped to friendly names
  2. Venmo handling (see below)
  3. User rules  (table `rules`, managed with `python -m money rule ...`)
  4. Per-transaction overrides (table `overrides`, applied in the v_transactions view)

`is_transfer = 1` means "money moving between your own accounts" - excluded from spending
and income so nothing is double counted (credit-card payments, savings transfers, ...).

Venmo: when a Venmo account is linked, Venmo is treated like any other account. Payments to /
from friends inside Venmo count as spending / reimbursements (category "Venmo"), while bank-side
"VENMO" debits/credits are just money moving into/out of Venmo (transfers). If Venmo is not
linked, bank-side Venmo transactions are the only record, so they count as category "Venmo".
These heuristics are a first pass: check them against real data after the first sync.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

PRIMARY_NAMES = {
    "INCOME": "Income",
    "TRANSFER_IN": "Transfer",
    "TRANSFER_OUT": "Transfer",
    "LOAN_DISBURSEMENTS": "Transfer",
    "LOAN_PAYMENTS": "Loan Payments",
    "BANK_FEES": "Fees",
    "ENTERTAINMENT": "Entertainment",
    "FOOD_AND_DRINK": "Dining",
    "GENERAL_MERCHANDISE": "Shopping",
    "HOME_IMPROVEMENT": "Home",
    "MEDICAL": "Health",
    "PERSONAL_CARE": "Personal Care",
    "GENERAL_SERVICES": "Services",
    "GOVERNMENT_AND_NON_PROFIT": "Taxes & Gov",
    "TRANSPORTATION": "Transportation",
    "TRAVEL": "Travel",
    "RENT_AND_UTILITIES": "Utilities",
    "OTHER": "Other",
}

DETAILED_NAMES = {
    "FOOD_AND_DRINK_GROCERIES": "Groceries",
    "RENT_AND_UTILITIES_RENT": "Rent",
    "GENERAL_SERVICES_INSURANCE": "Insurance",
    "TRANSFER_OUT_WITHDRAWAL": "Cash",
    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT": "Credit Card Payment",
}

# Transfer categories that are real outflows rather than internal moves.
NOT_TRANSFERS = {"TRANSFER_OUT_WITHDRAWAL"}

VENMO_RE = re.compile(r"venmo", re.I)
# Inside a Venmo account: money moving between Venmo and a bank (not a payment to a person).
VENMO_BANK_MOVE_RE = re.compile(
    r"(standard|instant) transfer|transfer (to|from)|cash ?out|add(ed)? (money|funds)|top[- ]?up|bank transfer",
    re.I,
)


def base_category(primary: str | None, detailed: str | None, amount: float) -> tuple[str, int]:
    if detailed in DETAILED_NAMES:
        category = DETAILED_NAMES[detailed]
    elif primary in PRIMARY_NAMES:
        category = PRIMARY_NAMES[primary]
    else:
        category = "Uncategorized"

    is_transfer = 0
    if primary in ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_DISBURSEMENTS") and detailed not in NOT_TRANSFERS:
        is_transfer = 1
    if detailed == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT":
        is_transfer = 1
    return category, is_transfer


def _is_venmo_account(institution: str | None, account_name: str | None) -> bool:
    return bool(VENMO_RE.search(f"{institution or ''} {account_name or ''}"))


def recategorize(conn: sqlite3.Connection) -> int:
    """Recompute `category` and `is_transfer` for every transaction. Returns rows touched."""
    accounts = {
        r["account_id"]: r
        for r in conn.execute("SELECT account_id, institution, name FROM accounts")
    }
    venmo_linked = any(_is_venmo_account(a["institution"], a["name"]) for a in accounts.values())
    user_rules = []
    for r in conn.execute("SELECT * FROM rules ORDER BY id"):
        try:
            user_rules.append((
                re.compile(r["pattern"], re.I),
                re.compile(r["account_pattern"], re.I) if r["account_pattern"] else None,
                r["category"],
                r["is_transfer"],
            ))
        except re.error:
            continue  # a bad regex shouldn't break syncing; `rule list` shows it

    updates = []
    rows = conn.execute(
        "SELECT transaction_id, account_id, name, merchant_name, counterparty, amount, "
        "plaid_category, plaid_category_detailed FROM transactions"
    ).fetchall()
    for t in rows:
        acct = accounts.get(t["account_id"])
        institution = acct["institution"] if acct else ""
        account_name = acct["name"] if acct else ""
        text = " ".join(x for x in (t["name"], t["merchant_name"], t["counterparty"]) if x)

        category, is_transfer = base_category(t["plaid_category"], t["plaid_category_detailed"], t["amount"])

        if _is_venmo_account(institution, account_name):
            if VENMO_BANK_MOVE_RE.search(text):
                category, is_transfer = "Transfer", 1
            elif t["plaid_category"] in (None, "TRANSFER_IN", "TRANSFER_OUT", "OTHER"):
                category, is_transfer = "Venmo", 0
        elif VENMO_RE.search(text):
            if venmo_linked:
                category, is_transfer = "Transfer", 1
            else:
                category, is_transfer = "Venmo", 0

        acct_text = f"{institution} {account_name}"
        for pattern, acct_pattern, r_category, r_transfer in user_rules:
            if pattern.search(text) and (acct_pattern is None or acct_pattern.search(acct_text)):
                if r_category:
                    category = r_category
                if r_transfer is not None:
                    is_transfer = int(r_transfer)

        updates.append((category, is_transfer, t["transaction_id"]))

    conn.executemany("UPDATE transactions SET category = ?, is_transfer = ? WHERE transaction_id = ?", updates)
    return len(updates)


def rebuild_networth(conn: sqlite3.Connection, days: int = 730) -> int:
    """Materialize a daily net-worth series into `networth_history`.

    Cash/credit/loan accounts are reconstructed backwards from today's balance using
    transactions (balance yesterday = balance today - today's net flow). Investment accounts
    use recorded balance snapshots (flat before the first snapshot).
    """
    accounts = conn.execute(
        "SELECT account_id, type, balance_current FROM accounts WHERE hidden = 0"
    ).fetchall()
    if not accounts:
        conn.execute("DELETE FROM networth_history")
        return 0

    first_tx = conn.execute("SELECT MIN(date) FROM transactions").fetchone()[0]
    end = date.today()
    start = end - timedelta(days=days)
    if first_tx:
        start = max(start, date.fromisoformat(first_tx))
    else:
        first_snap = conn.execute("SELECT MIN(date) FROM balance_history").fetchone()[0]
        start = max(start, date.fromisoformat(first_snap)) if first_snap else end

    n_days = (end - start).days + 1
    buckets = {k: [0.0] * n_days for k in ("cash", "investments", "credit", "loans")}

    for a in accounts:
        kind = {"investment": "investments", "credit": "credit", "loan": "loans"}.get(a["type"], "cash")
        sign = -1.0 if kind in ("credit", "loans") else 1.0
        current = sign * (a["balance_current"] or 0.0)
        series = buckets[kind]

        if kind == "investments":
            snaps = conn.execute(
                "SELECT date, balance_current FROM balance_history WHERE account_id = ? ORDER BY date",
                (a["account_id"],),
            ).fetchall()
            snap_by_day = {s["date"]: s["balance_current"] for s in snaps if s["balance_current"] is not None}
            value = snaps[0]["balance_current"] if snaps and snaps[0]["balance_current"] is not None else current
            for i in range(n_days):
                d = (start + timedelta(days=i)).isoformat()
                if d in snap_by_day:
                    value = snap_by_day[d]
                series[i] += value
            continue

        flows = dict(conn.execute(
            "SELECT date, SUM(amount) FROM transactions WHERE account_id = ? AND date > ? GROUP BY date",
            (a["account_id"], start.isoformat()),
        ).fetchall())
        value = current
        for i in range(n_days - 1, -1, -1):
            series[i] += value
            d = (start + timedelta(days=i)).isoformat()
            value -= flows.get(d, 0.0)  # undo this day's flows to get the previous day's balance

    conn.execute("DELETE FROM networth_history")
    rows = []
    for i in range(n_days):
        cash, inv = buckets["cash"][i], buckets["investments"][i]
        credit, loans = buckets["credit"][i], buckets["loans"][i]
        rows.append((
            (start + timedelta(days=i)).isoformat(),
            round(cash, 2), round(inv, 2), round(credit, 2), round(loans, 2),
            round(cash + inv + credit + loans, 2),
        ))
    conn.executemany("INSERT INTO networth_history VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def refresh_derived(conn: sqlite3.Connection) -> None:
    recategorize(conn)
    rebuild_networth(conn)
    conn.commit()
