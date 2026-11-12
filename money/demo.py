"""Synthetic demo data (fake people, fake accounts) shaped like Plaid responses.

Lets you try the dashboard before linking anything, and backs the tests.
Writes only to data/demo.db.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta

from money import db, rules, sync

FRIENDS = ["Alex Kim", "Jordan Lee", "Sam Patel", "Priya Shah", "Chris Wong"]


def _tx(i: int, account: str, d: date, name: str, plaid_amount: float, primary: str, detailed: str,
        merchant: str | None = None, counterparty: str | None = None) -> dict:
    return {
        "transaction_id": f"demo-tx-{i}",
        "account_id": account,
        "date": d.isoformat(),
        "name": name,
        "merchant_name": merchant,
        "counterparties": [{"name": counterparty, "type": "merchant"}] if counterparty else [],
        "amount": round(plaid_amount, 2),  # Plaid sign: positive = money out
        "iso_currency_code": "USD",
        "pending": False,
        "payment_channel": "online",
        "personal_finance_category": {"primary": primary, "detailed": detailed},
    }


def _accounts() -> dict[str, tuple[str, list[dict]]]:
    def acct(aid, name, typ, sub, cur, avail=None, limit=None, mask=None):
        return {"account_id": aid, "name": name, "official_name": name, "mask": mask, "type": typ, "subtype": sub,
                "balances": {"current": cur, "available": avail, "limit": limit, "iso_currency_code": "USD"}}

    return {
        "demo-item-bank": ("Demo Bank", [
            acct("demo-checking", "Everyday Checking", "depository", "checking", 3412.18, 3380.02, mask="1234"),
            acct("demo-savings", "High Yield Savings", "depository", "savings", 9150.00, 9150.00, mask="5678"),
        ]),
        "demo-item-card": ("Demo Card Co", [
            acct("demo-card", "Rewards Card", "credit", "credit card", 742.55, 4257.45, 5000, mask="9012"),
        ]),
        "demo-item-venmo": ("Venmo", [
            acct("demo-venmo", "Venmo", "depository", "paypal", 86.40, 86.40),
        ]),
        "demo-item-broker": ("Demo Brokerage", [
            acct("demo-brokerage", "Individual Brokerage", "investment", "brokerage", 15320.75, mask="3456"),
        ]),
    }


def generate(days: int = 240, seed: int = 7) -> tuple[dict, list[dict]]:
    rng = random.Random(seed)
    today = date.today()
    start = today - timedelta(days=days)
    txs: list[dict] = []
    n = 0

    def add(*args, **kw):
        nonlocal n
        n += 1
        txs.append(_tx(n, *args, **kw))

    dining = ["Sweetgreen", "Chipotle", "Local Ramen", "Blue Bottle Coffee", "Joe's Pizza", "Taqueria Sol"]
    shopping = ["Amazon", "Target", "Uniqlo", "Best Buy"]
    d = start
    while d <= today:
        dow = d.weekday()
        if d.day in (1,):
            add("demo-checking", d, "RENT PAYMENT", 1450.00, "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT",
                merchant="Maple Apartments")
            add("demo-checking", d, "Transfer to Savings", 300.00, "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
            add("demo-savings", d, "Transfer from Checking", -300.00, "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")
        if d.day == 3:
            add("demo-card", d, "Spotify", 11.99, "ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO", merchant="Spotify")
            add("demo-card", d, "Netflix", 15.49, "ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES", merchant="Netflix")
        if d.day == 12:
            add("demo-checking", d, "City Power & Light", rng.uniform(45, 95), "RENT_AND_UTILITIES",
                "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY", merchant="City Power")
            add("demo-card", d, "Phone bill", 45.00, "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_TELEPHONE",
                merchant="Mint Mobile")
        if d.day == 20:
            pay = round(rng.uniform(600, 1100), 2)
            add("demo-checking", d, "Payment to Rewards Card", pay, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
            add("demo-card", d, "Payment - Thank You", -pay, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        if dow == 4 and (d - start).days % 14 < 7:
            add("demo-checking", d, "ACME CORP PAYROLL", -2150.00, "INCOME", "INCOME_WAGES", merchant="Acme Corp")
        if dow == 6:
            add("demo-card", d, "Trader Joe's", rng.uniform(55, 140), "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES",
                merchant="Trader Joe's")
        if rng.random() < 0.45:
            m = rng.choice(dining)
            add("demo-card", d, m.upper(), rng.uniform(8, 48), "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", merchant=m)
        if rng.random() < 0.12:
            m = rng.choice(shopping)
            add("demo-card", d, m.upper(), rng.uniform(15, 160), "GENERAL_MERCHANDISE",
                "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", merchant=m)
        if rng.random() < 0.15:
            add("demo-card", d, "UBER TRIP", rng.uniform(9, 32), "TRANSPORTATION",
                "TRANSPORTATION_TAXIS_AND_RIDE_SHARES", merchant="Uber")
        # Venmo: you front a group dinner on the card, friends pay you back on Venmo
        if dow == 5 and rng.random() < 0.6:
            total = round(rng.uniform(80, 180), 2)
            add("demo-card", d, "THE GOOD PLACE BISTRO", total, "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT",
                merchant="The Good Place Bistro")
            for f in rng.sample(FRIENDS, 2):
                add("demo-venmo", d + timedelta(days=rng.randint(0, 2)), f"Payment from {f}", -round(total / 4, 2),
                    "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER", counterparty=f)
        # ...and sometimes you pay friends for things
        if rng.random() < 0.08:
            f = rng.choice(FRIENDS)
            add("demo-venmo", d, f"Payment to {f}", rng.uniform(10, 60), "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER",
                counterparty=f)
        if d.day == 25:
            amt = 150.0
            add("demo-venmo", d, "Standard transfer to bank", amt, "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
            add("demo-checking", d, "VENMO CASHOUT", -amt, "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER", merchant="Venmo")
        d += timedelta(days=1)

    # a couple of pending card transactions today
    for name, amt in (("Blue Bottle Coffee", 6.75), ("Amazon", 23.10)):
        add("demo-card", today, name.upper(), amt, "FOOD_AND_DRINK" if "Coffee" in name else "GENERAL_MERCHANDISE",
            "FOOD_AND_DRINK_COFFEE" if "Coffee" in name else "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", merchant=name)
        txs[-1]["pending"] = True
    return _accounts(), txs


def build(conn: sqlite3.Connection, days: int = 240) -> None:
    for table in ("items", "accounts", "balance_history", "transactions", "holdings", "securities",
                  "networth_history", "meta"):
        conn.execute(f"DELETE FROM {table}")
    accounts, txs = generate(days)
    for item_id, (institution, accts) in accounts.items():
        products = ["investments"] if item_id == "demo-item-broker" else ["transactions"]
        sync.ensure_item_row(conn, item_id, {"institution_name": institution, "products": products})
        sync.upsert_accounts(conn, item_id, institution, accts)
        conn.execute("UPDATE items SET last_synced = ? WHERE item_id = ?", (db.now_iso(), item_id))
    sync.upsert_transactions(conn, txs)

    # Investment history: weekly snapshots with a gentle random walk ending at today's balance.
    rng = random.Random(11)
    value, today = 15320.75, date.today()
    for w in range(0, days // 7):
        d = today - timedelta(days=7 * w)
        conn.execute("INSERT OR REPLACE INTO balance_history VALUES ('demo-brokerage', ?, ?, NULL)",
                     (d.isoformat(), round(value, 2)))
        value = value / (1 + rng.gauss(0.002, 0.012)) - 100  # undo a week of growth + a $100 contribution
    securities = [("demo-sec-vti", "VTI", "Vanguard Total Stock Market ETF", "etf", 9800.0, 7900.0),
                  ("demo-sec-vxus", "VXUS", "Vanguard Total International Stock ETF", "etf", 4100.0, 3900.0),
                  ("demo-sec-cash", None, "Cash sweep", "cash", 1420.75, 1420.75)]
    for sid, ticker, name, typ, val, basis in securities:
        conn.execute("INSERT INTO securities VALUES (?,?,?,?,?,?)", (sid, ticker, name, typ, None, db.now_iso()))
        conn.execute("INSERT INTO holdings VALUES ('demo-brokerage', ?, ?, ?, ?, ?, 'USD', ?)",
                     (sid, round(val / 100, 4), 100.0, val, basis, db.today()))
    rules.refresh_derived(conn)
    db.set_meta(conn, "last_sync", db.now_iso())
    db.set_meta(conn, "demo", "1")
    conn.commit()
