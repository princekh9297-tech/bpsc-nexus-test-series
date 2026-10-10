# BPSC Nexus Premium Pass Admin Panel

This package upgrades the existing webhook-based Telegram bot with a button-based, admin-only Premium Pass panel.

## Admin features

- `/admin` opens the panel only for numeric Telegram IDs listed in `ADMIN_TELEGRAM_IDS`.
- Grant pass by numeric Telegram ID or a username already known to the bot.
- Choose BPSC PYQ, Ghatnachakra PYQ, UPSC PYQ, Dual Vault (choose any two), or All-Access.
- Choose 7, 30, 90, 180 days, permanent access, or a custom number of days (1–3650).
- Search/manage students, inspect active passes, extend a pass, revoke one pass without affecting other passes, and view membership statistics.
- Admin callbacks are checked server-side; admin buttons are not sent to ordinary users. Admin panel is private-chat only.
- Existing separate passes are preserved when granting another pass.

## Before deployment

1. Back up the existing GitHub repository files locally.
2. Run `premium_pass_migration.sql` in the SQL Editor of the dedicated BPSC Nexus Neon project. Do not run it against another application's database.
3. Replace `bot.py`, `requirements.txt`, `render.yaml`, and `README.md` with this package's versions. Keep your existing webhook deployment structure and Render service.
4. In Render Environment, keep `BOT_TOKEN`, `DATABASE_URL`, and `WEBHOOK_SECRET`; ensure `ADMIN_TELEGRAM_IDS` contains your numeric Telegram user ID(s), comma-separated if multiple.
5. Deploy and test `/start`, `/admin`, `/tests`, and `/mypremium`.

## Important data mapping

`tests.vault_key` defaults to `general` so existing tests are not automatically assigned to a product without confirmation. Assign each test to its correct vault using the SQL Editor, e.g. `UPDATE tests SET vault_key = 'bpsc_pyq' WHERE ...;`. Use the exact vault key names `bpsc_pyq`, `ghatnachakra_pyq`, and `upsc_pyq` for those products. All-Access unlocks any mapped vault. A test's legacy `is_premium = TRUE` flag still means a matching pass is required, but the `vault_key` must be set to the correct product to match product passes.

## Current scope

This package manages memberships and filters the published test catalogue by vault entitlements. The interactive question attempt, answer submission, timer, and scoring engine are not included in this package.

## Environment variables

- `BOT_TOKEN`
- `DATABASE_URL`
- `WEBHOOK_SECRET` (random secret, at least 16 characters)
- `ADMIN_TELEGRAM_IDS` (numeric Telegram user IDs, comma-separated)

Render provides `RENDER_EXTERNAL_URL` and `PORT` for the webhook service.
