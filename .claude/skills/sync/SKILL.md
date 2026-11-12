---
name: sync
description: Refresh the user's financial data from Plaid (banks, cards, Venmo, investments) and report what changed. Use when the user says sync, refresh, update my data, or asks about recent activity and the data is stale.
---

# Sync

1. Run `python -m money sync`. Each linked institution prints one line.
2. If a line says LOGIN_REQUIRED (or `status` shows `login_required`): tell the user that institution
   needs a fresh login and give them the exact command, `python -m money link --update <item_id>`
   (it opens a browser; they log in inside Plaid's window). Don't retry in a loop.
3. If it says Plaid is still pulling history (common for an hour or so after first linking), say so;
   another sync later fills in older months.
4. Run `python -m money brief`, and if anything changed, report briefly:
   - new transactions since the previous sync that are unusually large (`txns --since <date>`)
   - anything newly categorized as Uncategorized/Other
   - balance alerts (e.g. checking unusually low, card balance unusually high)
   Keep it to a few lines unless the user asks for more.
