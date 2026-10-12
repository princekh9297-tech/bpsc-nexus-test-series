# Global Leaderboard — Test Report

## Passed in this workspace

- `python -m py_compile bot.py miniapp.py question_bank.py group_quiz.py leaderboard.py`
- JavaScript syntax check (`node --check`) for the Mini App's final inline script.
- UTC week/month helper checks: 2026-10-12 resolves to ISO week `2026-W42` and month `2026-10`; 2026-01-01 resolves to `2026-W01` and `2026-01`.
- Static source inspection confirmed that leaderboard routes are behind the existing `/api/` Telegram `initData` middleware and admin routes check `ADMIN_TELEGRAM_IDS`.
- The ledger uses a unique `source_key`; summaries update only after a new ledger insert.

## Not run / not verified

- Python module import/integration test could not run because `psycopg` is not installed in the execution environment (`ModuleNotFoundError: No module named 'psycopg'`).
- No live PostgreSQL database was connected, so SQL migrations, query execution, advisory locks, backfill and ranking results have not been exercised against a real database.
- No live Telegram Bot API or Render deployment test was possible.
- Cross-account privacy, duplicate submission, daily challenge ranking, group completion ingestion, admin exclusion and regression tests against payment/membership flows still require deployment testing.

Do not treat this package as production-verified until those integration tests pass on a staging or production-equivalent database.
