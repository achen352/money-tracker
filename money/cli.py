"""Command line: `python -m money <command>`. Run `python -m money -h` for the list."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import subprocess
import sys
import webbrowser

from money import config, db, plaid_api, reports, rules, views


def _out(text: str) -> None:
    # Windows consoles may not be UTF-8; never crash on a merchant name.
    enc = sys.stdout.encoding or "utf-8"
    sys.stdout.write(text.encode(enc, errors="replace").decode(enc) + "\n")


# --- commands ----------------------------------------------------------------------------


def cmd_init(args) -> int:
    for d in (config.DATA_DIR, config.VIEWS_DIR, config.SECRETS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not config.ENV_FILE.exists():
        shutil.copy(config.ROOT / ".env.example", config.ENV_FILE)
        _out("Created .env - add your Plaid keys to it.")
    local_md = config.ROOT / "CLAUDE.local.md"
    if not local_md.exists():
        shutil.copy(config.ROOT / "CLAUDE.local.example.md", local_md)
        _out("Created CLAUDE.local.md - your private notes for Claude (gitignored).")
    db.connect().close()
    if (config.ROOT / ".git").exists():
        subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=config.ROOT, check=False)
        _out("Enabled the pre-commit hook that blocks committing secrets/data.")
    _out(f"Database ready at {config.db_path()}")
    return 0


def cmd_serve(args) -> int:
    from money import server
    return server.serve(args.port, open_path="" if args.open else None, auto_sync=not args.no_sync)


def cmd_link(args) -> int:
    from money import server
    if args.update and args.update not in plaid_api.load_items():
        _out(f"Unknown item id {args.update}. See `python -m money status`.")
        return 1
    path = "/link" + (f"?update={args.update}" if args.update else "")
    _out("Opening the Link page in your browser. Bank logins happen in Plaid's window - "
         "credentials never touch this app.")
    return server.serve(args.port, open_path=path)


def cmd_sync(args) -> int:
    from money import sync
    from money.plaid_api import NotConfigured
    if config.is_demo():
        _out("Demo mode: nothing to sync (use `python -m money demo` to regenerate).")
        return 0
    if not plaid_api.load_items():
        _out("No linked institutions yet. Run `python -m money link` first.")
        return 1
    try:
        results = sync.sync_all(only_item=args.item)
    except NotConfigured as e:
        _out(str(e))
        return 1
    for r in results:
        _out(r.line())
    return 0 if all(r.status == "ok" for r in results) else 2


def cmd_status(args) -> int:
    conn = db.connect()
    _out(reports.status(conn))
    return 0


def cmd_brief(args) -> int:
    try:
        conn = db.connect_readonly()
        _out(reports.brief(conn))
    except Exception as e:  # a session-start hook must never fail loudly
        _out(f"money-tracker: couldn't read the database ({e.__class__.__name__}).")
    return 0


def cmd_summary(args) -> int:
    conn = db.connect_readonly()
    _out(reports.summary(conn, args.month))
    return 0


def cmd_query(args) -> int:
    conn = db.connect_readonly(max_seconds=30)
    try:
        cur = conn.execute(args.sql)
    except sqlite3.Error as e:
        _out(f"SQL error: {e}")
        return 1
    if cur.description is None:
        _out("(no result set)")
        return 0
    columns = [d[0] for d in cur.description]
    rows = cur.fetchmany(args.limit + 1)
    more = len(rows) > args.limit
    rows = [list(r) for r in rows[: args.limit]]
    if args.csv:
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(columns)
        w.writerows(rows)
    else:
        _out(reports.table(columns, rows))
    if more:
        _out(f"... more rows not shown (raise --limit, or aggregate)")
    return 0


def cmd_txns(args) -> int:
    where, p = ["1=1"], []
    if args.month:
        where.append("month = ?")
        p.append(args.month)
    if args.since:
        where.append("date >= ?")
        p.append(args.since)
    if args.category:
        where.append("category = ?")
        p.append(args.category)
    if args.search:
        where.append("(payee LIKE ? OR description LIKE ? OR note LIKE ?)")
        p += [f"%{args.search}%"] * 3
    if args.account:
        where.append("(account LIKE ? OR institution LIKE ?)")
        p += [f"%{args.account}%"] * 2
    if not args.include_transfers:
        where.append("is_transfer = 0")
    sql = (f"SELECT date, payee, category, account, amount, id FROM v_transactions WHERE {' AND '.join(where)} "
           f"ORDER BY date DESC LIMIT ?")
    conn = db.connect_readonly()
    rows = [list(r) for r in conn.execute(sql, p + [args.limit])]
    _out(reports.table(["date", "payee", "category", "account", "amount", "id"], rows))
    _out(f"({len(rows)} rows; total {reports.usd(sum(r[4] for r in rows), True)})")
    return 0


def cmd_tag(args) -> int:
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM transactions WHERE transaction_id = ?", (args.id,)).fetchone():
        _out(f"No transaction with id {args.id}")
        return 1
    if args.clear:
        conn.execute("DELETE FROM overrides WHERE transaction_id = ?", (args.id,))
    else:
        current = conn.execute("SELECT * FROM overrides WHERE transaction_id = ?", (args.id,)).fetchone()
        category = args.category if args.category is not None else (current["category"] if current else None)
        is_transfer = args.transfer if args.transfer is not None else (current["is_transfer"] if current else None)
        note = args.note if args.note is not None else (current["note"] if current else None)
        conn.execute(
            "INSERT OR REPLACE INTO overrides(transaction_id, category, is_transfer, note, updated_at) "
            "VALUES (?,?,?,?,?)",
            (args.id, category, is_transfer, note, db.now_iso()),
        )
    conn.commit()
    row = conn.execute("SELECT date, payee, category, is_transfer, note, amount FROM v_transactions WHERE id = ?",
                       (args.id,)).fetchone()
    _out(reports.table(list(row.keys()), [list(row)]))
    return 0


def cmd_rule(args) -> int:
    conn = db.connect()
    if args.rule_cmd == "add":
        import re
        try:
            re.compile(args.pattern)
            if args.account:
                re.compile(args.account)
        except re.error as e:
            _out(f"Bad regex: {e}")
            return 1
        if args.category is None and args.transfer is None:
            _out("A rule needs --category and/or --transfer/--not-transfer.")
            return 1
        conn.execute(
            "INSERT INTO rules(pattern, account_pattern, category, is_transfer, created_at) VALUES (?,?,?,?,?)",
            (args.pattern, args.account, args.category, args.transfer, db.now_iso()),
        )
        rules.refresh_derived(conn)
        rx = re.compile(args.pattern, re.I)
        n = sum(1 for r in conn.execute("SELECT name, merchant_name, counterparty FROM transactions")
                if rx.search(" ".join(x for x in r if x)))
        _out(f"Rule added and applied ({n} transactions match the pattern).")
    elif args.rule_cmd == "rm":
        conn.execute("DELETE FROM rules WHERE id = ?", (args.id,))
        rules.refresh_derived(conn)
        _out("Rule removed and categories recomputed.")
    else:
        rows = [list(r) for r in conn.execute("SELECT id, pattern, account_pattern, category, is_transfer FROM rules")]
        _out(reports.table(["id", "pattern", "account", "category", "is_transfer"], rows) if rows else "No rules.")
    conn.commit()
    return 0


def cmd_recategorize(args) -> int:
    conn = db.connect()
    rules.refresh_derived(conn)
    _out("Categories and net-worth history recomputed.")
    return 0


def cmd_view(args) -> int:
    if args.view_cmd == "list":
        specs = views.load_custom()
        _out("\n".join(f"{s['id']}: {s.get('title', '')}" for s in specs) or "No custom views in data/views/.")
        return 0
    path = views.custom_path(args.name.removesuffix(".json").split("/")[-1].split("\\")[-1])
    if path is None or not path.exists():
        _out(f"No such view: {args.name} (looked in {config.VIEWS_DIR})")
        return 1
    if args.view_cmd == "rm":
        path.unlink()
        _out(f"Removed {path.name}")
        return 0
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        _out(f"Invalid JSON: {e}")
        return 1
    conn = db.connect_readonly()
    result = views.run(conn, spec, views.params(args.range))
    if result.get("error"):
        _out(f"ERROR: {result['error']}")
        return 1
    rows = result["rows"]
    _out(f"OK: '{spec['title']}' ({spec.get('chart', 'table')}) -> {len(rows)} rows, columns {result['columns']}")
    if rows:
        _out(reports.table(result["columns"], rows[:5]))
        if len(rows) > 5:
            _out(f"... {len(rows) - 5} more rows")
    return 0


def cmd_unlink(args) -> int:
    from money.plaid_api import PlaidClient, PlaidError
    info = plaid_api.load_items().get(args.item)
    if not info:
        _out(f"Unknown item {args.item}")
        return 1
    try:
        PlaidClient.from_env().post("/item/remove", {"access_token": info["access_token"]})
    except PlaidError as e:
        _out(f"Plaid said: {e} (removing locally anyway)")
    plaid_api.forget_item(args.item)
    conn = db.connect()
    conn.execute("UPDATE items SET status = 'removed' WHERE item_id = ?", (args.item,))
    if args.delete_data:
        ids = [r[0] for r in conn.execute("SELECT account_id FROM accounts WHERE item_id = ?", (args.item,))]
        for a in ids:
            conn.execute("DELETE FROM transactions WHERE account_id = ?", (a,))
            conn.execute("DELETE FROM balance_history WHERE account_id = ?", (a,))
            conn.execute("DELETE FROM holdings WHERE account_id = ?", (a,))
        conn.execute("DELETE FROM accounts WHERE item_id = ?", (args.item,))
    else:
        conn.execute("UPDATE accounts SET hidden = 1 WHERE item_id = ?", (args.item,))
    rules.refresh_derived(conn)
    _out(f"Unlinked {info.get('institution_name')}.")
    return 0


def cmd_vscode(args) -> int:
    from money import vscode_ext
    if args.build_only:
        _out(f"Built {vscode_ext.build()}")
        return 0
    vsix, status = vscode_ext.install()
    if status != "ok":
        _out(status)
        return 1
    _out("Installed the Money Dashboard extension. Click the bar-chart icon in VS Code's left Activity Bar\n"
         "(if it isn't there yet: Ctrl+Shift+P > 'Developer: Reload Window').")
    return 0


def cmd_demo(args) -> int:
    from money import demo
    config.use_demo()
    conn = db.connect()
    demo.build(conn)
    _out(f"Demo database written to {config.DEMO_DB}. Try: python -m money --demo summary")
    return 0


# --- parser ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m money", description="Local personal finance tracker.")
    p.add_argument("--demo", action="store_true", help="use the synthetic demo database (data/demo.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create .env, folders, database, CLAUDE.local.md, git hook").set_defaults(fn=cmd_init)

    s = sub.add_parser("serve", help="run the dashboard (http://127.0.0.1:8765)")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="open in the default browser")
    s.add_argument("--no-sync", action="store_true", help="don't auto-sync stale data on start")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("link", help="link a bank / card / Venmo / brokerage via Plaid Link")
    s.add_argument("--update", metavar="ITEM_ID", help="re-authenticate an existing connection")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_link)

    s = sub.add_parser("sync", help="pull new data from Plaid")
    s.add_argument("--item", help="only this item id")
    s.set_defaults(fn=cmd_sync)

    sub.add_parser("status", help="linked institutions and their health").set_defaults(fn=cmd_status)
    sub.add_parser("brief", help="3-line status (used by the Claude SessionStart hook)").set_defaults(fn=cmd_brief)

    s = sub.add_parser("summary", help="compact overview: balances, cash flow, categories, Venmo")
    s.add_argument("--month", help="YYYY-MM (default: current month)")
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser("query", help="run read-only SQL and print a table")
    s.add_argument("sql")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--csv", action="store_true")
    s.set_defaults(fn=cmd_query)

    s = sub.add_parser("txns", help="list transactions with filters")
    s.add_argument("--month", help="YYYY-MM")
    s.add_argument("--since", help="YYYY-MM-DD")
    s.add_argument("--category")
    s.add_argument("--search", help="substring of payee/description/note")
    s.add_argument("--account", help="substring of account or institution")
    s.add_argument("--include-transfers", action="store_true")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_txns)

    s = sub.add_parser("tag", help="override one transaction's category / transfer flag / note")
    s.add_argument("id")
    s.add_argument("--category")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--transfer", dest="transfer", action="store_const", const=1)
    g.add_argument("--not-transfer", dest="transfer", action="store_const", const=0)
    s.add_argument("--note")
    s.add_argument("--clear", action="store_true", help="remove the override")
    s.set_defaults(fn=cmd_tag, transfer=None)

    s = sub.add_parser("rule", help="categorization rules applied to all matching transactions")
    rs = s.add_subparsers(dest="rule_cmd", required=True)
    ra = rs.add_parser("add")
    ra.add_argument("pattern", help="case-insensitive regex on description/merchant/counterparty")
    ra.add_argument("--account", help="only for accounts matching this regex")
    ra.add_argument("--category")
    g = ra.add_mutually_exclusive_group()
    g.add_argument("--transfer", dest="transfer", action="store_const", const=1)
    g.add_argument("--not-transfer", dest="transfer", action="store_const", const=0)
    ra.set_defaults(transfer=None)
    rs.add_parser("list")
    rr = rs.add_parser("rm")
    rr.add_argument("id", type=int)
    s.set_defaults(fn=cmd_rule)

    sub.add_parser("recategorize", help="re-apply categorization rules").set_defaults(fn=cmd_recategorize)

    s = sub.add_parser("view", help="dashboard views in data/views/")
    vs = s.add_subparsers(dest="view_cmd", required=True)
    vc = vs.add_parser("check", help="validate a view and preview its first rows")
    vc.add_argument("name", help="view id or path to data/views/<id>.json")
    vc.add_argument("--range", type=int, default=6, help="months for :start (default 6)")
    vs.add_parser("list")
    vr = vs.add_parser("rm")
    vr.add_argument("name")
    s.set_defaults(fn=cmd_view)

    s = sub.add_parser("unlink", help="disconnect an institution")
    s.add_argument("item")
    s.add_argument("--delete-data", action="store_true", help="also delete its accounts and transactions")
    s.set_defaults(fn=cmd_unlink)

    s = sub.add_parser("vscode", help="install the VS Code sidebar extension (Activity Bar icon)")
    s.add_argument("--build-only", action="store_true", help="just build data/vscode/*.vsix")
    s.set_defaults(fn=cmd_vscode)

    sub.add_parser("demo", help="(re)generate the demo database").set_defaults(fn=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.demo:
        config.use_demo()
        if not config.DEMO_DB.exists() and args.cmd != "demo":
            from money import demo
            demo.build(db.connect())
    return args.fn(args) or 0
