# BPSC Nexus Test Series — Premium Pass update

This update adds admin-only Premium Pass commands to the webhook-based Telegram bot.

## Important prerequisite

Run `premium_pass_migration.sql` in the dedicated BPSC Nexus Neon project's SQL Editor. It adds `tests.is_premium` and creates `premium_passes`. It does not delete existing records.

## Update GitHub files

Replace `bot.py`, `requirements.txt`, `render.yaml`, and `.env.example` with the versions in this package. Keep the repository private. Do not upload secrets.

## Render environment variables

Keep existing `BOT_TOKEN`, `DATABASE_URL`, and `WEBHOOK_SECRET`. Add:

- `ADMIN_TELEGRAM_IDS`: your numeric Telegram user ID. Multiple administrators can be comma-separated, for example `123456789,987654321`.

Never put a bot token or database password in source code. The previously exposed bot token should be revoked in BotFather and replaced in Render before deployment.

## Commands

- `/start` — register/update the user
- `/tests` — list published tests available to the user
- `/mypremium` — check own pass
- `/grantpremium <Telegram ID or known @username> permanent`
- `/grantpremium <Telegram ID or known @username> 30`
- `/revokepremium <Telegram ID or known @username>`
- `/premiumstatus <Telegram ID or known @username>`

A username can only be resolved if the person has interacted with the bot before, because Telegram does not provide a general API to look up arbitrary usernames. Prefer numeric Telegram IDs.

## Marking tests as premium

In Neon SQL Editor, for example:

```sql
UPDATE tests SET is_premium = TRUE WHERE id = 123;
```

Only do this after creating the test and verifying its ID. To make a test public again:

```sql
UPDATE tests SET is_premium = FALSE WHERE id = 123;
```

The `/tests` catalogue filters premium tests by membership. This starter does not yet implement question-level delivery or the complete exam engine; those are separate development stages.

## Deployment

1. Run the migration.
2. Replace the code files in GitHub and commit.
3. Add `ADMIN_TELEGRAM_IDS` to Render Environment.
4. Deploy latest commit.
5. Test `/start`, `/mypremium`, and admin commands.
6. Confirm a non-admin account cannot execute grant/revoke commands.
