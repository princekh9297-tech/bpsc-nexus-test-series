# Leaderboard migration and deployment

## Before deployment

1. Keep the current PostgreSQL database and `DATABASE_URL`; do not reset, drop, or recreate the database.
2. Keep the existing Render services and start commands (`python bot.py` and `python miniapp.py`).
3. Ensure `ADMIN_TELEGRAM_IDS` is set on the Mini App service to the same comma-separated Telegram admin IDs used by the bot. This is needed for daily-challenge configuration and audited exclusions; public leaderboard reads still use verified Telegram `initData`.

## Migration

No manual destructive migration is required. On startup, `question_bank.py`/`miniapp.py` and `group_quiz.py` call the idempotent `leaderboard.ensure_schema()` routine. It creates the central ledger, indexed period summaries, daily challenge registry and audit table, and adds the missing summary column if the tables already exist.

A one-time backfill uses PostgreSQL advisory locks and migration markers. It reads only completed bank attempts with a valid time range, positive maximum marks and non-negative answer counts, and completed group sessions with stored answer keys. Unique source keys prevent duplicate ledger inserts if services restart or run concurrently. Existing rows are retained. Old attempts with no reliable aggregate score or invalid metadata are not fabricated into rankings.

## Deploy

1. Copy the ZIP contents into the existing repository, preserving paths.
2. Commit and deploy the bot and Mini App services from the same repository revision.
3. Check `/healthz` on the Mini App service and inspect Render logs for schema/startup errors.
4. Open the Mini App through Telegram and select **Ranks**.
5. As an authorized admin, configure a published quiz and UTC date in the Daily Challenge section.
6. Complete one Mini App test, one Telegram question-bank quiz and one group quiz. Verify that each completed submission appears once.
7. Test with two separate Telegram accounts. Confirm each sees only their own My Rank and only public aggregate leaderboard entries.

No additional paid service or AI API is introduced. The summaries are incrementally maintained; the only historical rebuild is the one-time backfill or an administrator-triggered recalculation after exclusion.
