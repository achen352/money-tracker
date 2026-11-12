import json
import threading
import unittest
import urllib.error
import urllib.request

from money import config, db, demo, server, views
from tests.helpers import TempDBTest


class ViewsTest(TempDBTest):
    def setUp(self):
        super().setUp()
        demo.build(self.conn, days=120)

    def test_builtin_views_run_cleanly_on_demo_data(self):
        d = views.dashboard(6)
        for v in d["builtin"]:
            self.assertNotIn("error", v, v["id"])
            self.assertTrue(v["rows"], f"{v['id']} returned no rows")
        self.assertTrue(d["has_data"])
        self.assertLessEqual(len(d["category_order"]), 7)

    def test_custom_view_loaded_and_errors_reported(self):
        config.VIEWS_DIR.mkdir(parents=True)
        (config.VIEWS_DIR / "good.json").write_text(json.dumps(
            {"title": "Dining", "chart": "bar", "sql": "SELECT month, SUM(spend) s FROM v_spending GROUP BY 1"}))
        (config.VIEWS_DIR / "bad.json").write_text(json.dumps({"title": "Bad", "sql": "SELECT nope FROM nothing"}))
        (config.VIEWS_DIR / "broken.json").write_text("{not json")
        custom = {v["id"]: v for v in views.dashboard(6)["custom"]}
        self.assertNotIn("error", custom["good"])
        self.assertIn("SQL error", custom["bad"]["error"])
        self.assertIn("Could not read", custom["broken"]["error"])

    def test_views_cannot_write(self):
        conn = db.connect_readonly()
        out = views.run(conn, {"title": "x", "sql": "DELETE FROM transactions"}, views.params(6))
        self.assertIn("error", out)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)

    def test_runaway_query_is_aborted(self):
        conn = db.connect_readonly()
        slow = ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
                "SELECT COUNT(*) FROM r")
        db.set_deadline(conn, 0.2)
        with self.assertRaises(Exception):
            conn.execute(slow).fetchall()

    def test_validation(self):
        self.assertTrue(views.validate({"sql": "SELECT 1"}))
        self.assertTrue(views.validate({"title": "x", "sql": "SELECT 1", "chart": "pie"}))
        self.assertFalse(views.validate({"title": "x", "sql": "SELECT 1", "chart": "line"}))

    def test_custom_path_rejects_traversal(self):
        self.assertIsNone(views.custom_path("../secrets"))
        self.assertIsNone(views.custom_path(""))
        self.assertIsNotNone(views.custom_path("dining-by-month"))


class ServerTest(TempDBTest):
    def setUp(self):
        super().setUp()
        demo.build(self.conn, days=60)
        self.httpd = server.make_server(0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, path, method="GET", headers=None, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=headers or {},
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_dashboard_api(self):
        status, body = self.req("/api/dashboard?range=3")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("builtin", data)
        self.assertIn("version", data)

    def test_rejects_foreign_host_and_origin(self):
        self.assertEqual(self.req("/api/version", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.req("/api/sync", "POST", headers={"Origin": "https://attacker.example"}, body={})[0], 403)

    def test_static_whitelist(self):
        self.assertEqual(self.req("/static/app.js")[0], 200)
        self.assertEqual(self.req("/static/../config.py")[0], 404)
        self.assertEqual(self.req("/static/link.html")[0], 404)

    def test_link_token_without_keys_is_a_clear_error(self):
        import os
        saved = {k: os.environ.pop(k, None) for k in ("PLAID_CLIENT_ID", "PLAID_SECRET")}
        try:
            status, body = self.req("/api/link/token", "POST", body={"mode": "bank"})
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(status, 400)
        self.assertIn("Plaid isn't configured", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
