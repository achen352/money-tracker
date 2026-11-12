"""Dashboard views: a chart = a SQL query + how to draw it.

Built-in views live in this file. Claude (or you) can add more by writing JSON files to
data/views/<slug>.json - the dashboard picks them up live. Spec fields:

  title     (required) chart title, sentence case
  sql       (required) read-only SQL against the database; may use :start, :end, :month_start, :today
  chart     stat | bar | hbar | stacked | line | area | table   (default: table)
  x         column for the x axis / category axis (default: first column)
  y         column or list of columns to plot      (default: remaining numeric columns)
  series    optional column whose values become separate series (long -> wide)
  format    usd | number | percent                 (default: usd)
  formats   optional per-column overrides, e.g. {"Txns": "number"}
  subtitle  optional one-line context under the title
  note      optional takeaway line (Claude's insight), shown under the chart
  hide_empty  hide the card when the query returns no rows (default: false)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from money import config, db

CHART_TYPES = {"stat", "bar", "hbar", "stacked", "line", "area", "table"}
FORMATS = {"usd", "number", "percent"}
MAX_ROWS = 2000

BUILTIN: list[dict] = [
    {
        "id": "kpis",
        "title": "Overview",
        "chart": "stat",
        "sql": """
            SELECT
              (SELECT ROUND(SUM(net_value), 2) FROM v_accounts) AS "Net worth",
              (SELECT ROUND(SUM(net_value), 2) FROM v_accounts WHERE type = 'depository') AS "Cash",
              (SELECT ROUND(SUM(net_value), 2) FROM v_accounts WHERE type = 'investment') AS "Investments",
              (SELECT ROUND(-SUM(net_value), 2) FROM v_accounts WHERE type IN ('credit', 'loan')) AS "Owed on cards & loans",
              (SELECT ROUND(SUM(spend), 2) FROM v_spending WHERE date >= :month_start) AS "Spent this month",
              (SELECT ROUND(SUM(spend), 2) FROM v_spending
                 WHERE date >= date(:month_start, '-1 month') AND date <= date(:today, '-1 month'))
                 AS "Spent this month__compare"
        """,
    },
    {
        "id": "spending_by_month",
        "title": "Spending by month",
        "subtitle": "Top categories; transfers and income excluded, refunds and reimbursements netted",
        "chart": "stacked",
        "x": "month",
        "y": "spend",
        "series": "category",
        "sql": """
            WITH top AS (
              SELECT category FROM v_spending GROUP BY category
              HAVING SUM(spend) > 0 ORDER BY SUM(spend) DESC LIMIT 7
            )
            SELECT month,
                   CASE WHEN category IN (SELECT category FROM top) THEN category ELSE 'Other' END AS category,
                   ROUND(SUM(spend), 2) AS spend
            FROM v_spending
            WHERE date >= :start
            GROUP BY 1, 2
            ORDER BY 1
        """,
    },
    {
        "id": "category_this_month",
        "title": "This month by category",
        "chart": "hbar",
        "x": "category",
        "y": "spend",
        "hide_empty": True,
        "sql": """
            SELECT category, ROUND(SUM(spend), 2) AS spend
            FROM v_spending WHERE date >= :month_start
            GROUP BY 1 HAVING SUM(spend) > 0 ORDER BY 2 DESC
        """,
    },
    {
        "id": "cashflow",
        "title": "Income vs. spending",
        "chart": "bar",
        "x": "month",
        "y": ["Income", "Spending"],
        "sql": """
            SELECT month,
                   ROUND(SUM(CASE WHEN category = 'Income' THEN amount ELSE 0 END), 2) AS "Income",
                   ROUND(SUM(CASE WHEN category <> 'Income' THEN -amount ELSE 0 END), 2) AS "Spending"
            FROM v_transactions
            WHERE is_transfer = 0 AND date >= :start
            GROUP BY month ORDER BY month
        """,
    },
    {
        "id": "networth",
        "title": "Net worth",
        "subtitle": "Cash & cards reconstructed from transactions; investments from daily snapshots",
        "chart": "line",
        "x": "date",
        "y": "Net worth",
        "hide_empty": True,
        "sql": """
            SELECT date, net_worth AS "Net worth" FROM networth_history
            WHERE date >= :start ORDER BY date
        """,
    },
    {
        "id": "venmo_people",
        "title": "Venmo by person",
        "subtitle": "Net = what they paid you minus what you paid them",
        "chart": "table",
        "hide_empty": True,
        "formats": {"Payments": "number"},
        "sql": """
            SELECT payee AS "Person",
                   ROUND(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 2) AS "You paid",
                   ROUND(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2) AS "They paid you",
                   ROUND(SUM(amount), 2) AS "Net",
                   COUNT(*) AS "Payments"
            FROM v_transactions
            WHERE category = 'Venmo' AND is_transfer = 0 AND date >= :start
            GROUP BY payee ORDER BY SUM(ABS(amount)) DESC LIMIT 15
        """,
    },
    {
        "id": "accounts",
        "title": "Accounts",
        "chart": "table",
        "hide_empty": True,
        "sql": """
            SELECT institution AS "Institution",
                   name || COALESCE(' ··' || mask, '') AS "Account",
                   type AS "Type",
                   net_value AS "Balance"
            FROM v_accounts ORDER BY type, net_value DESC
        """,
    },
    {
        "id": "recent",
        "title": "Recent transactions",
        "chart": "table",
        "hide_empty": True,
        "sql": """
            SELECT date AS "Date", payee AS "Payee", category AS "Category", account AS "Account",
                   amount AS "Amount"
            FROM v_transactions WHERE is_transfer = 0
            ORDER BY date DESC, id LIMIT 30
        """,
    },
]


def params(range_months: int | None) -> dict:
    today = date.today()
    month_start = today.replace(day=1)
    if range_months:
        y, m = month_start.year, month_start.month - (range_months - 1)
        while m <= 0:
            y, m = y - 1, m + 12
        start = date(y, m, 1)
    else:
        start = date(1900, 1, 1)
    return {
        "start": start.isoformat(),
        "end": (today + timedelta(days=1)).isoformat(),
        "month_start": month_start.isoformat(),
        "today": today.isoformat(),
    }


def validate(spec: dict) -> list[str]:
    problems = []
    if not isinstance(spec.get("title"), str) or not spec["title"].strip():
        problems.append("missing 'title'")
    if not isinstance(spec.get("sql"), str) or not spec["sql"].strip():
        problems.append("missing 'sql'")
    if spec.get("chart", "table") not in CHART_TYPES:
        problems.append(f"'chart' must be one of {sorted(CHART_TYPES)}")
    if spec.get("format", "usd") not in FORMATS:
        problems.append(f"'format' must be one of {sorted(FORMATS)}")
    for col, fmt in (spec.get("formats") or {}).items():
        if fmt not in FORMATS:
            problems.append(f"formats[{col!r}] must be one of {sorted(FORMATS)}")
    return problems


def run(conn: sqlite3.Connection, spec: dict, p: dict) -> dict:
    """Evaluate one view. Never raises: errors are returned in the payload."""
    out = {k: v for k, v in spec.items() if k != "sql"}
    out.setdefault("chart", "table")
    problems = validate(spec)
    if problems:
        out.update(error="; ".join(problems), columns=[], rows=[])
        return out
    try:
        db.set_deadline(conn, 3.0)
        cur = conn.execute(spec["sql"], p)
        columns = [d[0] for d in cur.description or []]
        rows = [list(r) for r in cur.fetchmany(MAX_ROWS)]
        out.update(columns=columns, rows=rows, truncated=len(rows) == MAX_ROWS)
    except sqlite3.Error as e:
        out.update(error=f"SQL error: {e}", columns=[], rows=[])
    return out


def load_custom() -> list[dict]:
    """Views saved in data/views/*.json, newest first."""
    if not config.VIEWS_DIR.exists():
        return []
    specs = []
    for path in sorted(config.VIEWS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(spec, dict):
                raise ValueError("top level must be a JSON object")
        except (ValueError, OSError) as e:
            spec = {"title": path.stem, "error": f"Could not read {path.name}: {e}"}
        spec["id"] = path.stem
        spec["updated"] = int(path.stat().st_mtime)
        specs.append(spec)
    return specs


def custom_path(view_id: str) -> Path | None:
    if not view_id or not all(c.isalnum() or c in "-_" for c in view_id):
        return None
    return config.VIEWS_DIR / f"{view_id}.json"


def dashboard(range_months: int | None = 6) -> dict:
    p = params(range_months)
    conn = db.connect_readonly()
    try:
        builtin = [run(conn, spec, p) for spec in BUILTIN]
        custom = []
        for spec in load_custom():
            if "error" in spec and "sql" not in spec:
                custom.append({**spec, "chart": "table", "columns": [], "rows": []})
            else:
                custom.append(run(conn, spec, p))
        order = [r[0] for r in conn.execute(
            "SELECT category FROM v_spending GROUP BY category HAVING SUM(spend) > 0 "
            "ORDER BY SUM(spend) DESC LIMIT 7"
        )]
        items = [dict(r) for r in conn.execute(
            "SELECT item_id, institution_name, status, error, last_synced FROM items "
            "WHERE status <> 'removed' ORDER BY institution_name"
        )]
        has_accounts = conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None
        last_sync = db.get_meta(conn, "last_sync")
    finally:
        conn.close()
    return {
        "builtin": builtin,
        "custom": custom,
        "category_order": order,
        "items": items,
        "has_data": has_accounts,
        "last_sync": last_sync,
        "params": p,
    }
