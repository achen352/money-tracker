---
name: chart
description: Put a chart or table of the user's finances on the local money dashboard. Use whenever the user asks to see, graph, plot, chart, visualize, or compare anything about their money (spending, categories, merchants, Venmo, net worth, investments, trends).
---

# Make a dashboard chart

Charts are JSON files in `data/views/<kebab-slug>.json`. The dashboard (the Money sidebar in VS Code,
usually open beside you; also http://127.0.0.1:8765) polls every 2s, shows new views at the top under
"From Claude", and highlights them. The SQL runs locally on every refresh; you never need to put
data rows in the file or in your context.

## Steps

1. Explore with `python -m money query "..."` (aggregated, small) until the SQL returns exactly the
   rows to plot.
2. Write the spec file (Write tool). Re-using a slug updates that chart in place.
3. Validate: `python -m money view check <slug>`. It prints row count + first rows, or the error.
4. Reply in chat with the takeaway in a sentence or two, and say it's on the dashboard. If the
   user doesn't have it open: click the Money icon in the Activity Bar (it starts the server).

## Spec

```json
{
  "title": "Dining out by month",
  "subtitle": "Restaurants + coffee; card and Venmo",
  "chart": "bar",
  "x": "month",
  "y": "Dining",
  "sql": "SELECT month, ROUND(SUM(spend), 2) AS \"Dining\" FROM v_spending WHERE category = 'Dining' AND date >= :start GROUP BY month ORDER BY month",
  "note": "Down 40% since August; October was the lowest month this year."
}
```

| field | meaning |
|---|---|
| `title` | required. Sentence case, says what's plotted |
| `sql` | required. Read-only SQLite against the `v_` views (see CLAUDE.md). 3s time limit, 2000 rows max |
| `chart` | `bar` (columns; several `y` = grouped), `stacked` (needs `series`), `hbar` (ranked horizontal bars, one `y`), `line`, `area`, `stat` (big numbers from row 1; each column is a tile), `table` |
| `x` | x / category column (default: first column). `YYYY-MM` values render as "Oct '26" |
| `y` | column or list of columns (default: other numeric columns) |
| `series` | long-format column to split into series (e.g. `category`). Spending categories keep the same colors as the built-in charts |
| `format` | `usd` (default), `number`, `percent` (values are fractions: 0.25 = 25%) |
| `formats` | per-column override, e.g. `{"Payments": "number"}` |
| `subtitle`, `note` | optional context line; optional one-line takeaway shown under the chart |
| `hide_empty` | hide the card when there are no rows |

SQL parameters, filled from the dashboard's range picker: `:start` (first day of the range),
`:end`, `:month_start` (first of this month), `:today`. Use `date >= :start` so the chart follows
the range picker unless the question fixes the period.

## Choosing the form

- One number → `stat` (e.g. "how much did I spend on Uber this year").
- Change over time → `line` (daily/weekly), `bar` (monthly totals), `stacked` (monthly composition).
- Ranking / comparing categories or merchants → `hbar`, sorted descending, top 10-15.
- Two measures with different scales (dollars vs. counts) → two charts, never one dual axis.
- More than 7 series → fold the tail into 'Other' in SQL (see `spending_by_month` in `money/views.py`).
- Exact values or lists → `table`.
- No pies or donuts; use `hbar`.

## Examples

Top merchants this month:
```sql
SELECT payee, ROUND(SUM(spend), 2) AS spend FROM v_spending
WHERE date >= :month_start AND spend > 0 GROUP BY payee ORDER BY spend DESC LIMIT 12
```
(chart `hbar`, x `payee`, y `spend`)

Venmo balance with one friend over time (`table` or `bar`):
```sql
SELECT month, ROUND(SUM(amount), 2) AS "Net" FROM v_transactions
WHERE category = 'Venmo' AND payee LIKE '%Alex%' GROUP BY month ORDER BY month
```

Savings rate by month (`line`, format `percent`):
```sql
SELECT month, (SUM(CASE WHEN category='Income' THEN amount ELSE 0 END) + SUM(CASE WHEN category<>'Income' THEN amount ELSE 0 END))
       / NULLIF(SUM(CASE WHEN category='Income' THEN amount ELSE 0 END), 0) AS "Savings rate"
FROM v_transactions WHERE is_transfer = 0 AND date >= :start GROUP BY month ORDER BY month
```

Keep only views the user still wants: `python -m money view list`, `python -m money view rm <slug>`.
Never publish these charts as Artifacts or anywhere off this machine.
