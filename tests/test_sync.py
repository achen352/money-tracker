import unittest

from money import plaid_api, sync
from money.plaid_api import PlaidError
from tests.helpers import FakePlaid, TempDBTest

ACCOUNTS = {"accounts": [{"account_id": "acc1", "name": "Checking", "type": "depository", "subtype": "checking",
                          "mask": "0000", "balances": {"current": 500.0, "available": 450.0,
                                                       "iso_currency_code": "USD"}}]}


def t(tid, amount, pending=False, pending_id=None):
    return {"transaction_id": tid, "account_id": "acc1", "amount": amount, "date": "2026-02-01", "name": tid,
            "pending": pending, "pending_transaction_id": pending_id,
            "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_RESTAURANT"}}


class SyncTest(TempDBTest):
    INFO = {"access_token": "access-sandbox-test", "institution_name": "Test Bank", "products": ["transactions"]}

    def run_sync(self, responses):
        client = FakePlaid(responses)
        result = sync.sync_item(self.conn, client, "item1", dict(self.INFO))
        return result, client

    def test_paginates_and_stores_cursor(self):
        result, client = self.run_sync({
            "/accounts/get": ACCOUNTS,
            "/transactions/sync": [
                {"added": [t("a", 10.0)], "modified": [], "removed": [], "next_cursor": "c1", "has_more": True},
                {"added": [t("b", 20.0)], "modified": [], "removed": [], "next_cursor": "c2", "has_more": False,
                 "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"},
            ],
        })
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.added, 2)
        self.assertEqual(client.calls[2][1]["cursor"], "c1")
        cursor = self.conn.execute("SELECT tx_cursor FROM items WHERE item_id='item1'").fetchone()[0]
        self.assertEqual(cursor, "c2")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 2)
        bal = self.conn.execute("SELECT balance_current FROM balance_history").fetchone()[0]
        self.assertEqual(bal, 500.0)

    def test_modified_and_removed(self):
        self.run_sync({"/accounts/get": ACCOUNTS, "/transactions/sync": [
            {"added": [t("a", 10.0), t("b", 5.0)], "modified": [], "removed": [], "next_cursor": "c1", "has_more": False}]})
        self.run_sync({"/accounts/get": ACCOUNTS, "/transactions/sync": [
            {"added": [], "modified": [t("a", 12.5)], "removed": [{"transaction_id": "b"}], "next_cursor": "c2",
             "has_more": False}]})
        rows = self.conn.execute("SELECT transaction_id, amount FROM transactions").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("a", -12.5)])

    def test_override_follows_pending_to_posted(self):
        self.run_sync({"/accounts/get": ACCOUNTS, "/transactions/sync": [
            {"added": [t("p1", 10.0, pending=True)], "modified": [], "removed": [], "next_cursor": "c1",
             "has_more": False}]})
        self.conn.execute("INSERT INTO overrides(transaction_id, category) VALUES ('p1', 'Gifts')")
        self.run_sync({"/accounts/get": ACCOUNTS, "/transactions/sync": [
            {"added": [t("posted1", 10.0, pending_id="p1")], "modified": [], "removed": [{"transaction_id": "p1"}],
             "next_cursor": "c2", "has_more": False}]})
        cat = self.conn.execute("SELECT category FROM overrides WHERE transaction_id='posted1'").fetchone()[0]
        self.assertEqual(cat, "Gifts")

    def test_mutation_during_pagination_restarts(self):
        result, _ = self.run_sync({"/accounts/get": ACCOUNTS, "/transactions/sync": [
            {"added": [t("a", 1.0)], "modified": [], "removed": [], "next_cursor": "c1", "has_more": True},
            PlaidError("TRANSACTIONS_ERROR", "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "changed"),
            {"added": [t("a", 1.0), t("b", 2.0)], "modified": [], "removed": [], "next_cursor": "c9",
             "has_more": False},
        ]})
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.added, 2)

    def test_login_required_marks_item(self):
        result, _ = self.run_sync({"/accounts/get": PlaidError("ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "login")})
        self.assertEqual(result.status, "login_required")
        self.assertIn("link --update item1", result.message)
        status = self.conn.execute("SELECT status FROM items WHERE item_id='item1'").fetchone()[0]
        self.assertEqual(status, "login_required")

    def test_investments_item_skips_transactions(self):
        info = {**self.INFO, "products": ["investments"]}
        client = FakePlaid({
            "/accounts/get": {"accounts": [{"account_id": "inv", "name": "Brokerage", "type": "investment",
                                            "balances": {"current": 1000.0}}]},
            "/investments/holdings/get": {
                "accounts": [{"account_id": "inv"}],
                "holdings": [{"account_id": "inv", "security_id": "s1", "quantity": 2, "institution_price": 500,
                              "institution_value": 1000, "cost_basis": 800}],
                "securities": [{"security_id": "s1", "ticker_symbol": "VTI", "name": "Total Market", "type": "etf"}],
            },
        })
        result = sync.sync_item(self.conn, client, "item2", info)
        self.assertEqual(result.holdings, 1)
        self.assertNotIn("/transactions/sync", [c[0] for c in client.calls])
        gain = self.conn.execute("SELECT gain FROM v_holdings").fetchone()[0]
        self.assertEqual(gain, 200)


class TokenStoreTest(TempDBTest):
    def test_store_and_forget(self):
        plaid_api.store_item("i1", "access-sandbox-x", "Bank", "ins_1", ["transactions"])
        self.assertEqual(plaid_api.load_items()["i1"]["institution_name"], "Bank")
        plaid_api.forget_item("i1")
        self.assertEqual(plaid_api.load_items(), {})


if __name__ == "__main__":
    unittest.main()
