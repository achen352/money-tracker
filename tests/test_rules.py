import unittest

from money import rules, sync
from tests.helpers import TempDBTest


def tx(tid, account, amount, name, primary, detailed=None, merchant=None, date="2026-01-15", pending=False):
    return {
        "transaction_id": tid, "account_id": account, "amount": amount, "name": name, "merchant_name": merchant,
        "date": date, "pending": pending, "iso_currency_code": "USD",
        "personal_finance_category": {"primary": primary, "detailed": detailed or primary + "_OTHER"},
    }


def acct(aid, typ="depository", cur=100.0, name="Checking"):
    return {"account_id": aid, "name": name, "type": typ, "subtype": None,
            "balances": {"current": cur, "available": cur, "iso_currency_code": "USD"}}


class CategorizeTest(TempDBTest):
    def load(self, venmo_linked: bool):
        sync.upsert_accounts(self.conn, "bank", "Big Bank", [acct("chk"), acct("card", "credit", 50, "Card")])
        if venmo_linked:
            sync.upsert_accounts(self.conn, "venmo", "Venmo", [acct("vm", name="Venmo")])
        sync.upsert_transactions(self.conn, [
            tx("t1", "card", 42.0, "SWEETGREEN", "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),
            tx("t2", "card", 80.0, "TRADER JOES", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
            tx("t3", "chk", 500.0, "PAYMENT TO CARD", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
            tx("t4", "chk", 60.0, "VENMO PAYMENT 1234", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER"),
            tx("t5", "chk", -2000.0, "ACME PAYROLL", "INCOME", "INCOME_WAGES"),
            tx("t6", "chk", 100.0, "ATM WITHDRAWAL", "TRANSFER_OUT", "TRANSFER_OUT_WITHDRAWAL"),
        ])
        if venmo_linked:
            sync.upsert_transactions(self.conn, [
                tx("v1", "vm", 25.0, "Payment to Alex", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER"),
                tx("v2", "vm", -30.0, "Payment from Sam", "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER"),
                tx("v3", "vm", 60.0, "Standard transfer to bank", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER"),
            ])
        rules.recategorize(self.conn)

    def get(self, tid):
        return self.conn.execute("SELECT category, is_transfer, amount FROM v_transactions WHERE id = ?", (tid,)).fetchone()

    def test_sign_is_flipped_from_plaid(self):
        self.load(False)
        self.assertEqual(self.get("t1")["amount"], -42.0)
        self.assertEqual(self.get("t5")["amount"], 2000.0)

    def test_base_categories_and_transfers(self):
        self.load(False)
        self.assertEqual(tuple(self.get("t1"))[:2], ("Dining", 0))
        self.assertEqual(tuple(self.get("t2"))[:2], ("Groceries", 0))
        self.assertEqual(tuple(self.get("t3"))[:2], ("Credit Card Payment", 1))
        self.assertEqual(tuple(self.get("t5"))[:2], ("Income", 0))
        self.assertEqual(tuple(self.get("t6"))[:2], ("Cash", 0))  # ATM cash is spending, not a transfer

    def test_bank_side_venmo_counts_when_venmo_not_linked(self):
        self.load(False)
        self.assertEqual(tuple(self.get("t4"))[:2], ("Venmo", 0))

    def test_bank_side_venmo_is_transfer_when_venmo_linked(self):
        self.load(True)
        self.assertEqual(tuple(self.get("t4"))[:2], ("Transfer", 1))
        self.assertEqual(tuple(self.get("v1"))[:2], ("Venmo", 0))
        self.assertEqual(tuple(self.get("v2"))[:2], ("Venmo", 0))
        self.assertEqual(tuple(self.get("v3"))[:2], ("Transfer", 1))
        # Net Venmo spending = paid 25, got back 30
        spend = self.conn.execute("SELECT SUM(spend) FROM v_spending WHERE category = 'Venmo'").fetchone()[0]
        self.assertAlmostEqual(spend, -5.0)

    def test_user_rule_then_override_precedence(self):
        self.load(False)
        self.conn.execute("INSERT INTO rules(pattern, category) VALUES ('sweetgreen', 'Lunch')")
        rules.recategorize(self.conn)
        self.assertEqual(self.get("t1")["category"], "Lunch")
        self.conn.execute("INSERT INTO overrides(transaction_id, category, is_transfer) VALUES ('t1', 'Work expense', 1)")
        self.assertEqual(tuple(self.get("t1"))[:2], ("Work expense", 1))

    def test_bad_user_regex_is_skipped(self):
        self.load(False)
        self.conn.execute("INSERT INTO rules(pattern, category) VALUES ('([', 'Broken')")
        rules.recategorize(self.conn)  # must not raise
        self.assertEqual(self.get("t1")["category"], "Dining")


class NetWorthTest(TempDBTest):
    def test_reconstructs_balances_backwards(self):
        from datetime import date, timedelta
        today = date.today()
        d = lambda n: (today - timedelta(days=n)).isoformat()  # noqa: E731
        sync.upsert_accounts(self.conn, "bank", "Bank", [acct("chk", cur=1000.0), acct("card", "credit", 200.0, "Card")])
        sync.upsert_transactions(self.conn, [
            tx("a", "chk", -500.0, "PAYROLL", "INCOME", "INCOME_WAGES", date=d(2)),     # +500 two days ago
            tx("b", "card", 50.0, "LUNCH", "FOOD_AND_DRINK", date=d(1)),               # -50 yesterday
            tx("c", "chk", 10.0, "COFFEE", "FOOD_AND_DRINK", date=d(10)),
        ])
        rules.rebuild_networth(self.conn)
        nw = dict(self.conn.execute("SELECT date, net_worth FROM networth_history").fetchall())
        self.assertAlmostEqual(nw[d(0)], 800.0)          # 1000 - 200
        self.assertAlmostEqual(nw[d(1)], 800.0)          # end of yesterday: lunch already happened
        self.assertAlmostEqual(nw[d(2)], 850.0)          # before lunch
        self.assertAlmostEqual(nw[d(3)], 350.0)          # before payroll
        self.assertAlmostEqual(nw[d(10)], 350.0)         # coffee happened on that day
        self.assertEqual(min(nw), d(10))                 # series starts at the first transaction


if __name__ == "__main__":
    unittest.main()
