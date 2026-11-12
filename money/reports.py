"""Compact text reports for the terminal and for Claude's context window.

Everything here aggregates. Raw transaction dumps are opt-in (`txns`, `query`) and capped.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from money import db, plaid_api


def usd(x: float | None, cents: bool = False) -> str:
    if x is None:
        return "-"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}" if cents else f"{sign}${abs(x):,.0f}"


def table(columns: list[str], rows: list, max_width: int = 40) -> str:
    def cell(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            s = f"{v:,.2f}"
        else:
            s = str(v)
        s = s.replace("\n", " ")
        return s if len(s) <= max_width else s[: max_width - 1] + "…"

    body = [[cell(v) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in body]) for i, c in enumerate(columns)]
    numeric = [bool(rows) and all(isinstance(r[i], (int, float)) or r[i] is None for r in rows)
               for i in range(len(columns))]

    def fmt_row(r: list[str]) -> str:
        return "  ".join(
            (v.rjust(w) if numeric[i] else v.ljust(w)) for i, (v, w) in enumerate(zip(r, widths))
        ).rstrip()

    lines = [fmt_row(columns), fmt_row(["-" * w for w in widths])]
    lines += [fmt_row(r) for r in body]
    return "\n".join(lines)


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    delta = datetime.now() - datetime.fromisoformat(iso)
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def _month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start.isoformat(), end.isoformat()


def attention(conn: sqlite3.Connection) -> list[str]:
    out = []
    for r in conn.execute("SELECT item_id, institution_name, status, error FROM items WHERE status NOT IN ('ok','removed')"):
        if r["status"] == "login_required":
            out.append(f"{r['institution_name']} needs re-login: python -m money link --update {r['item_id']}")
        else:
            out.append(f"{r['institution_name']} sync error: {r['error']}")
    linked = set(plaid_api.load_items())
    known = {r[0] for r in conn.execute("SELECT item_id FROM items")}
    if linked - known:
        out.append(f"{len(linked - known)} linked institution(s) never synced: python -m money sync")
    return out


def brief(conn: sqlite3.Connection) -> str:
    """A few lines for the start of every Claude session (SessionStart hook)."""
    if not db.has_data(conn):
        return ("money-tracker: no financial data yet. Setup: README.md (Plaid keys in .env, then "
                "`python -m money link`). Try the dashboard with demo data: `python -m money --demo serve`.")
    nw = conn.execute("SELECT SUM(net_value) FROM v_accounts").fetchone()[0]
    month_start = date.today().replace(day=1).isoformat()
    spent = conn.execute("SELECT SUM(spend) FROM v_spending WHERE date >= ?", (month_start,)).fetchone()[0]
    n_accounts = conn.execute("SELECT COUNT(*) FROM v_accounts").fetchone()[0]
    last = db.get_meta(conn, "last_sync")
    lines = [
        f"money-tracker: {n_accounts} accounts, last sync {_ago(last)}. "
        f"Net worth {usd(nw)}; spent {usd(spent or 0)} so far this month.",
        "Run `python -m money summary` before answering money questions; charts go in data/views/ (see /chart).",
    ]
    if last and (datetime.now() - datetime.fromisoformat(last)).total_seconds() > 24 * 3600:
        lines.append("Data is over a day old - offer to run `python -m money sync`.")
    lines += [f"ATTENTION: {a}" for a in attention(conn)]
    return "\n".join(lines)


def summary(conn: sqlite3.Connection, month: str | None = None) -> str:
    if not db.has_data(conn):
        return "No data yet. Link accounts first (README.md), or use --demo."
    month = month or date.today().strftime("%Y-%m")
    m_start, m_end = _month_bounds(month)
    out = []
    last = db.get_meta(conn, "last_sync")
    out.append(f"# Money summary for {month}  (data as of {last or 'n/a'}, {_ago(last)})")

    # Balances
    rows = conn.execute(
        "SELECT type, SUM(net_value) v, COUNT(*) n FROM v_accounts GROUP BY type ORDER BY v DESC"
    ).fetchall()
    nw = sum(r["v"] or 0 for r in rows)
    out.append(f"\nNet worth {usd(nw)}  (" + ", ".join(f"{r['type']} {usd(r['v'])}" for r in rows) + ")")
    accts = conn.execute(
        "SELECT institution, name, mask, type, net_value FROM v_accounts ORDER BY type, net_value DESC"
    ).fetchall()
    out.append(table(["institution", "account", "type", "balance"],
                     [[a["institution"], f"{a['name']}" + (f" ··{a['mask']}" if a["mask"] else ""), a["type"],
                       round(a["net_value"], 2)] for a in accts]))

    # Monthly totals (last 6 months incl. this one)
    hist = conn.execute(
        """SELECT month,
                  CAST(ROUND(SUM(CASE WHEN category = 'Income' THEN amount ELSE 0 END)) AS INTEGER) AS income,
                  CAST(ROUND(SUM(CASE WHEN category <> 'Income' THEN -amount ELSE 0 END)) AS INTEGER) AS spending
           FROM v_transactions WHERE is_transfer = 0 AND month <= ?
           GROUP BY month ORDER BY month DESC LIMIT 6""",
        (month,),
    ).fetchall()
    out.append("\n## Income & spending by month (transfers excluded)")
    out.append(table(["month", "income", "spending", "net"],
                     [[h["month"], h["income"], h["spending"], (h["income"] or 0) - (h["spending"] or 0)]
                      for h in hist]))
    if month == date.today().strftime("%Y-%m"):
        out.append(f"({month} is in progress: day {date.today().day})")

    # Categories this month vs trailing 3-month average
    cats = conn.execute(
        """WITH cur AS (SELECT category, SUM(spend) s FROM v_spending WHERE date >= ? AND date < ? GROUP BY 1),
                prev AS (SELECT category, SUM(spend) / 3.0 s FROM v_spending
                         WHERE date >= date(?, '-3 months') AND date < ? GROUP BY 1)
           SELECT COALESCE(cur.category, prev.category) AS category,
                  CAST(ROUND(COALESCE(cur.s, 0)) AS INTEGER) AS this_month, CAST(ROUND(COALESCE(prev.s, 0)) AS INTEGER) AS avg_prev_3mo
           FROM cur LEFT JOIN prev ON prev.category = cur.category
           UNION
           SELECT prev.category, 0, CAST(ROUND(prev.s) AS INTEGER) FROM prev
           WHERE prev.category NOT IN (SELECT category FROM cur) AND prev.s >= 25
           ORDER BY this_month DESC, avg_prev_3mo DESC""",
        (m_start, m_end, m_start, m_start),
    ).fetchall()
    out.append(f"\n## Spending by category, {month} vs. previous 3-month average")
    out.append(table(["category", "this_month", "avg_prev_3mo"], [list(c) for c in cats]))

    # Top merchants & largest transactions
    merch = conn.execute(
        """SELECT payee, COUNT(*) n, CAST(ROUND(SUM(spend)) AS INTEGER) total FROM v_spending
           WHERE date >= ? AND date < ? AND spend > 0 AND category <> 'Venmo'
           GROUP BY payee ORDER BY total DESC LIMIT 8""",
        (m_start, m_end),
    ).fetchall()
    out.append(f"\n## Top merchants, {month}")
    out.append(table(["payee", "n", "total"], [list(m) for m in merch]))

    big = conn.execute(
        """SELECT date, payee, category, account, amount, id FROM v_transactions
           WHERE is_transfer = 0 AND date >= ? AND date < ? AND ABS(amount) >= 100
           ORDER BY ABS(amount) DESC LIMIT 8""",
        (m_start, m_end),
    ).fetchall()
    if big:
        out.append(f"\n## Largest transactions, {month} (>= $100)")
        out.append(table(["date", "payee", "category", "account", "amount", "id"], [list(b) for b in big]))

    # Venmo
    venmo = conn.execute(
        """SELECT payee, ROUND(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 2) paid,
                  ROUND(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2) received,
                  ROUND(SUM(amount), 2) net
           FROM v_transactions WHERE category = 'Venmo' AND is_transfer = 0
             AND date >= date(?, '-2 months') AND date < ?
           GROUP BY payee ORDER BY SUM(ABS(amount)) DESC LIMIT 10""",
        (m_start, m_end),
    ).fetchall()
    if venmo:
        out.append("\n## Venmo by person, last 3 months (net > 0 = they paid you more)")
        out.append(table(["person", "you_paid", "they_paid", "net"], [list(v) for v in venmo]))

    pending = conn.execute("SELECT COUNT(*), SUM(amount) FROM v_transactions WHERE pending = 1").fetchone()
    uncategorized = conn.execute(
        "SELECT COUNT(*) FROM v_transactions WHERE category IN ('Uncategorized','Other') AND date >= ?", (m_start,)
    ).fetchone()[0]
    notes = []
    if pending[0]:
        notes.append(f"{pending[0]} pending transactions ({usd(pending[1], True)})")
    if uncategorized:
        notes.append(f"{uncategorized} uncategorized/other this month")
    notes += attention(conn)
    if notes:
        out.append("\n## Notes\n" + "\n".join(f"- {n}" for n in notes))
    return "\n".join(out)


def status(conn: sqlite3.Connection) -> str:
    linked = plaid_api.load_items()
    rows = conn.execute("SELECT * FROM items WHERE status <> 'removed' ORDER BY institution_name").fetchall()
    if not rows and not linked:
        return "No linked institutions. Run `python -m money link`."
    out = [table(
        ["item_id", "institution", "products", "status", "last_synced"],
        [[r["item_id"], r["institution_name"], r["products"], r["status"], _ago(r["last_synced"])] for r in rows],
        max_width=60,
    )]
    out += [f"ATTENTION: {a}" for a in attention(conn)]
    return "\n".join(out)
