# money-tracker

A small, local replacement for subscription finance apps. It pulls your bank, credit card, Venmo,
and investment accounts into one SQLite file on your machine, shows a live dashboard, and is set
up so Claude Code can analyze your money and draw charts for you while you chat.

- **Aggregation:** [Plaid](https://plaid.com) (free Trial plan: up to 10 connected institutions).
- **Storage:** `data/money.db` (SQLite). Nothing is committed; a git hook blocks data and secrets.
- **Dashboard:** `http://127.0.0.1:8765`, meant to sit in a VS Code tab next to the Claude panel.
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

2. **Get Plaid keys.** Sign up at [dashboard.plaid.com](https://dashboard.plaid.com/signup). New
   teams get a free **Trial** plan with real (production) data for up to 10 institutions. Under
   *Developers → Keys*, copy the `client_id` and the **production** secret into `.env`:

   ```
   PLAID_CLIENT_ID=...
   PLAID_SECRET=...
   PLAID_ENV=production
   ```

   To try the flow with a fake bank first, use the sandbox secret with `PLAID_ENV=sandbox` and log
   in as `user_good` / `pass_good`.

3. **Start the dashboard.** In VS Code: *Terminal → Run Task → Money: dashboard*. This task also
   starts automatically when you open the folder, once you allow automatic tasks. Or run:

   ```sh
   python -m money serve
   ```

4. **Put it beside Claude.** `Ctrl+Shift+P` → **Simple Browser: Show** → `http://127.0.0.1:8765`.
   Drag that tab to one side and keep the Claude Code panel on the other.

5. **Link your accounts:** run `python -m money link`, or click *Link account*. This opens your
   normal browser, because banks log in through pop-ups that VS Code's Simple Browser blocks. Pick
   **Bank, credit card, or Venmo** or **Investment account**, then log in inside Plaid's window.
   Repeat once per institution. Venmo is just another institution in the list.

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
| `python -m money serve` | dashboard + Link page on http://127.0.0.1:8765 |
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

Layout: `money/` (CLI, Plaid sync, rules, views, server), `money/web/` (dashboard), `tests/`,
`.claude/` (Claude Code settings + skills), `.vscode/tasks.json`.
