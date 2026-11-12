# money-tracker

A small, local replacement for subscription finance apps. It pulls your bank, credit card, Venmo,
and investment accounts into one SQLite file on your machine, shows a live dashboard, and is set
up so Claude Code can analyze your money and draw charts for you while you chat.

- **Aggregation:** [Plaid](https://plaid.com) (free Trial plan: up to 10 connected institutions).
- **Storage:** `data/money.db` (SQLite). Nothing is committed; a git hook blocks data and secrets.
- **Dashboard:** a **Money** icon in VS Code's left Activity Bar opens it in the sidebar (or an
  editor tab), right next to the Claude panel. Also at `http://127.0.0.1:8765` in any browser.
- **Claude:** reads compact summaries via `python -m money ...` and adds charts by writing small
  JSON files that the dashboard picks up live.
- **No dependencies:** Python 3.10+ standard library only.

## Setup (about 15 minutes, once)

1. **Initialize**

   ```sh
   python -m money init
   ```

   This creates `.env`, `data/`, `secrets/`, and your private `CLAUDE.local.md`, and turns on the
   pre-commit hook.

2. **Get Plaid keys (free).** Sign up at [dashboard.plaid.com](https://dashboard.plaid.com/signup),
   then apply for the free **Trial** plan: use the Trial button on the dashboard home page, or go
   to dashboard.plaid.com/trial-plan. It's usually approved automatically after an identity check.
   The Trial plan uses Plaid's real production API (so `PLAID_ENV=production`), but it's never billed.

   > **Don't click "Get Production access" or "Request access" under Production.** That applies
   > for the *paid* plan. It can't be undone, and it bills a monthly fee per connected account
   > even if you never sync.

   Trial limits: 10 connections (bank logins) in total. Removing one doesn't free its slot, so
   link each institution once, and use *Reconnect* instead of a fresh link when a bank asks you to
   log in again. API calls are unlimited. From the Keys page, copy the `client_id` and the
   Production secret into `.env`:

   ```
   PLAID_CLIENT_ID=...
   PLAID_SECRET=...
   PLAID_ENV=production
   ```

   To try the flow with a fake bank first, use the sandbox secret with `PLAID_ENV=sandbox` and log
   in as `user_good` / `pass_good`.

3. **Add the dashboard to VS Code** (once):

   ```sh
   python -m money vscode
   ```

   This installs a small local extension (no marketplace, no npm). A bar-chart **Money** icon
   appears in the left Activity Bar. Click it and the dashboard opens in the sidebar; the server
   starts and stops by itself. The *Open Dashboard in Editor* button in the view's title bar opens
   it as a wider editor tab instead. If Claude Code also lives in the left sidebar, drag either one to the right-hand
   (secondary) sidebar so you can see both at once.

4. **Link your accounts:** click *Link account* in the dashboard, or run `python -m money link`.
   This opens your normal browser, because bank logins use pop-up windows that VS Code can't show.
   Pick **Bank, credit card, or Venmo** or **Investment account**, then log in inside Plaid's
   window. Repeat once per institution. Venmo is just another institution in the list.

That's it. Each login is saved as a revocable access token in `secrets/` (gitignored). From then
on, `python -m money sync` (or the dashboard's *Sync* button) pulls fresh data whenever you want.
The dashboard also auto-syncs on start if the data is more than 6 hours old.

**Do I ever need to link again?** Occasionally. If you change a bank password, or a bank's
periodic consent expires (some ask yearly), that connection stops syncing. The dashboard then
shows a *Reconnect* banner, or you can run `python -m money link --update <item_id>` and log in
again. Your history is kept.

## Try it without any accounts

```sh
python -m money --demo serve      # fake accounts and 8 months of fake transactions
```

## Using it with Claude

Open Claude Code in this folder and just ask:

- "How much did I spend on food this month vs. usual?"
- "Who owes me on Venmo?"
- "Graph my spending by category for the last year." The chart appears in the dashboard tab.
- "Do a monthly review." Runs the `/review` skill.
- "Always categorize anything from Costco as Groceries." Claude adds a rule.

What makes this work well:

- `CLAUDE.md` gives Claude the schema, commands, sign conventions, and privacy rules.
- A **SessionStart hook** prints a short status at the start of each session: last sync, net
  worth, month-to-date spending, and any broken connections. Claude starts with context.
- `python -m money summary` packs balances, cash flow, categories vs. average, top merchants,
  large transactions, and Venmo-by-person into about 60 lines. Claude aggregates in SQL instead of
  reading thousands of rows.
- Skills: `/chart` (dashboard charts), `/sync`, `/review`.
- `CLAUDE.local.md` (gitignored) holds your goals, budgets, and context about friends and
  accounts. Claude reads it every session.

## Commands

| command | what it does |
|---|---|
| `python -m money vscode` | install the VS Code sidebar (Activity Bar icon) |
| `python -m money serve` | run the dashboard server by hand (http://127.0.0.1:8765) |
| `python -m money link [--update ITEM]` | connect (or reconnect) an institution |
| `python -m money sync` | pull new data |
| `python -m money status` | connections and their health |
| `python -m money summary [--month YYYY-MM]` | compact overview |
| `python -m money txns --month 2026-10 --category Dining` | list transactions |
| `python -m money query "SELECT ..."` | read-only SQL |
| `python -m money tag ID --category X` / `--transfer` | fix one transaction |
| `python -m money rule add REGEX --category X` | fix all matching transactions, now and in future |
| `python -m money view check SLUG` | validate a dashboard chart in `data/views/` |
| `python -m money unlink ITEM` | disconnect an institution |
| `python -m money --demo <cmd>` | any command against fake demo data |

## How spending is counted

- Amounts are negative for money out and positive for money in.
- Transfers between your own accounts are excluded from spending and income. That covers card
  payments, savings transfers, and moving money between a bank and Venmo.
- Refunds and Venmo reimbursements count as negative spending. If you front a $120 dinner and
  three friends pay you back $30 each, your net spend is $30.
- Categories come from Plaid. You can override them with rules (patterns) or tags (single
  transactions).

## Privacy

- Your data stays in `data/money.db` on your machine. Plaid keys are in `.env` and access tokens
  in `secrets/`. All three are gitignored, and `.githooks/pre-commit` refuses to commit them.
- Plaid sees your account data, just as Copilot Money or any other aggregator does.
- When you ask Claude about your finances, the summaries and query results it reads become part
  of that conversation, like anything else you share with Claude. Claude is instructed (in
  `CLAUDE.md`) never to read `.env` or `secrets/`, and `.claude/settings.json` enforces it.
  Claude is also told not to publish financial data anywhere.
- The dashboard listens on `127.0.0.1` only, and rejects requests from other sites or host names.

## Development

```sh
python -m unittest discover -s tests -t .
```

Layout: `money/` (CLI, Plaid sync, rules, views, server), `money/web/` (dashboard), `vscode/`
(the sidebar extension: plain JS, no build step), `tests/`, `.claude/` (Claude Code settings +
skills), `.vscode/tasks.json`. After changing `vscode/`, run `python -m money vscode` again.
Dashboard changes in `money/web/` need no reinstall; click the view's refresh button.
