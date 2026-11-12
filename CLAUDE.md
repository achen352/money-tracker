# money-tracker

Local personal-finance tracker: Plaid pulls bank / credit card / Venmo / investment data into
SQLite (`data/money.db`); a local dashboard (http://127.0.0.1:8765) shows charts; you (Claude)
analyze through the `python -m money` CLI and add charts to the dashboard.
Python 3.10+, standard library only - do not add dependencies without asking.

## Privacy rules (this repo is PUBLIC)

- Never read, print, or `cat` `.env` or anything in `secrets/` (Plaid keys and access tokens).
- Never put real financial data (names, amounts, merchants, account numbers, balances) in committed
  files: code, tests, README, CLAUDE.md, commit messages. Test fixtures use made-up data (see `money/demo.py`).
  Personal context belongs in `CLAUDE.local.md` (gitignored).
- Never send financial data off this machine except to the user in chat: no Artifacts, gists,
  pastebins, or web requests containing it. Charts go to the local dashboard (`data/views/`).
- `data/`, `secrets/`, `.env`, `CLAUDE.local.md` are gitignored and a pre-commit hook blocks them.
  If `git status` ever shows one of them as staged, stop and tell the user.

## Answering money questions (keep context small)

1. `python -m money summary [--month YYYY-MM]`: balances, monthly income/spending, categories vs.
   3-month average, top merchants, big transactions, Venmo by person, alerts. Start here.
2. `python -m money query "<SQL>"`: read-only SQL. Aggregate in SQL (GROUP BY, SUM); don't pull raw
   rows to sum them yourself. Default limit 100 rows; never `SELECT *` from transactions unbounded.
3. `python -m money txns --month 2026-10 --category Dining --search uber --limit 40`: to look at specifics.
4. If data looks stale (`brief` says so) or the user asks, run `python -m money sync` first.
5. If the answer is visual or a trend, also make a dashboard chart (see below).

Other commands: `status` (connection health), `link [--update ITEM]`, `tag ID --category X |
--transfer | --not-transfer | --note "..."`, `rule add REGEX --category X [--account REGEX] [--transfer]`,
`rule list|rm`, `recategorize`, `view check|list|rm`, `--demo <cmd>` (fake data in `data/demo.db`).

## Data model (SQLite; query the v_ views)

Sign convention: `amount < 0` = money out, `amount > 0` = money in (Plaid's sign is flipped on import).

- `v_transactions`: id, date (YYYY-MM-DD), month (YYYY-MM), institution, account, account_type,
  payee, description, amount, category, is_transfer, note, pending, account_id
- `v_spending`: v_transactions minus transfers and Income, plus `spend = -amount` (refunds and
  Venmo reimbursements are negative spend, so totals are net)
- `v_income`: category = 'Income', plus `income = amount`
- `v_accounts`: per account: type (depository|credit|investment|loan), balance_current, `net_value`
  (liabilities negative; `SUM(net_value)` = net worth), item_status
- `v_holdings`: ticker, security, quantity, price, value, cost_basis, gain
- `networth_history`: date, cash, investments, credit, loans, net_worth (daily; cash/cards
  reconstructed from transactions, investments from snapshots)
- Raw tables: transactions (incl. plaid_category / plaid_category_detailed / raw JSON), accounts,
  balance_history, items, rules, overrides, securities, holdings.

Categories come from Plaid's personal_finance_category mapped to friendly names (`money/rules.py`):
Dining, Groceries, Shopping, Rent, Utilities, Transportation, Travel, Entertainment, Health,
Personal Care, Services, Insurance, Fees, Cash, Loan Payments, Income, Transfer, Credit Card
Payment, Venmo, Other. Precedence: Plaid → Venmo handling → user `rules` → per-txn `overrides`.
When the user corrects a category, prefer a `rule` if it should apply to future transactions,
`tag` for one-offs.

`is_transfer = 1`: money moving between the user's own accounts (card payments, savings
transfers, bank<->Venmo). Excluded from spending and income to avoid double counting.

## Venmo

Linked through Plaid like a bank. Payments to/from people inside the Venmo account are category
`Venmo` (payee = the person); bank-side "VENMO" lines become transfers once Venmo is linked.
These heuristics (`money/rules.py`) were written before seeing real Venmo data from Plaid: after
the first real sync, check `txns --account venmo` and refine the rules if needed.

## Dashboard charts ("show me a graph of…")

Write a JSON spec to `data/views/<kebab-slug>.json`; the open dashboard picks it up within ~2s and
highlights it under "From Claude". Then run `python -m money view check <slug>`. Full spec and
examples: the `chart` skill (`.claude/skills/chart/SKILL.md`); built-in examples in `money/views.py`.
Remove views the user no longer wants (`view rm`). Don't create Artifacts for financial charts.

## Code layout

- `money/cli.py`: argparse commands · `money/sync.py`: Plaid → SQLite · `money/plaid_api.py`: REST
  client + token store · `money/rules.py`: categories, transfers, net-worth history · `money/views.py`:
  dashboard specs · `money/reports.py`: text summaries · `money/server.py`: local HTTP server ·
  `money/web/`: dashboard + Plaid Link page (Plotly from CDN) · `money/demo.py`: fake data
- Tests: `python -m unittest discover -s tests -t .` (stdlib unittest, fake Plaid client, temp DBs).
- Windows/PowerShell 5.1 note: don't rewrite source files with `Get-Content | Set-Content` (it
  corrupts UTF-8); use the Edit tool.
