import os
import shutil
import tempfile
import unittest
from pathlib import Path

from money import config, db


class TempDBTest(unittest.TestCase):
    """Each test gets its own database, views dir, and secrets dir in a temp folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="money-test-"))
        self._saved = {k: getattr(config, k) for k in ("VIEWS_DIR", "SECRETS_DIR", "ITEMS_FILE")}
        self._saved_env = os.environ.get("MONEY_DB")
        os.environ["MONEY_DB"] = str(self.tmp / "test.db")
        config.VIEWS_DIR = self.tmp / "views"
        config.SECRETS_DIR = self.tmp / "secrets"
        config.ITEMS_FILE = config.SECRETS_DIR / "plaid_items.json"
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        for k, v in self._saved.items():
            setattr(config, k, v)
        if self._saved_env is None:
            os.environ.pop("MONEY_DB", None)
        else:
            os.environ["MONEY_DB"] = self._saved_env
        shutil.rmtree(self.tmp, ignore_errors=True)


class FakePlaid:
    """Stands in for PlaidClient: returns canned responses per endpoint (lists are consumed in order)."""

    env = "sandbox"

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls = []

    def post(self, endpoint, payload=None):
        self.calls.append((endpoint, payload))
        r = self.responses[endpoint]
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
