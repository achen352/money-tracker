---
name: review
description: Monthly money review. Analyzes a month's spending, income, Venmo settle-ups, subscriptions, and anomalies against the user's goals, and adds a few charts to the dashboard. Use when the user asks for a monthly review, recap, check-in, or "how am I doing".
argument-hint: "[YYYY-MM]"
---

# Monthly review

Month: `$ARGUMENTS` if given, otherwise the last complete month (or month-to-date if the user asks
about "this month").

1. If the last sync is over a day old, sync first (see the `sync` skill).
2. `python -m money summary --month <month>`: the backbone. Read `CLAUDE.local.md` goals if present.
3. Dig into what stands out, using aggregated queries only:
   - categories more than ~25% above their 3-month average: which merchants drove it
     (`query` with GROUP BY payee)
   - recurring charges / subscriptions: same payee in 3+ of the last 4 months with a similar amount
   - Venmo: who still owes whom (net by person over ~3 months), and large one-off payments
   - transactions categorized Uncategorized/Other, or possible miscategorizations (Venmo, transfers).
     Propose `rule add` fixes; apply them only after the user agrees.
   - income vs. spending, savings rate, net worth change over the month (`networth_history`)
4. Create 1-3 dashboard views that support the findings (see the `chart` skill). For example: this
   month vs. average by category (`hbar`), the trend of the category that moved most (`bar`), or
   Venmo net by person (`table`).
5. Write the review in chat: 5-10 bullets, numbers rounded to whole dollars, most important first,
   ending with 1-3 concrete suggestions tied to the user's goals. Point to the new dashboard charts.
6. If the user states a new goal or preference, offer to add it to `CLAUDE.local.md`.
