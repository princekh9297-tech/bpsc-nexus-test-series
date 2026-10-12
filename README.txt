BPSC Nexus — Multi-Coaching Test Series Base (additive update)

Files to replace in your existing Mini App repository:
1) miniapp.py
2) static/index.html

No bot.py, group_quiz.py, question_bank.py, requirements.txt, or database reset is required for this base. On startup, miniapp.py creates the new series/entitlement tables additively. Existing tables and rows are not dropped.

What this base includes:
- Premium neutral-branded Test Series Hub with UPSC/BPSC and format filters.
- Full-length, sectional and subject-wise series metadata.
- Admin-only series draft creation (ADMIN_TELEGRAM_IDS must include your Telegram ID on the Mini App service).
- Attach existing published quiz records to a series by quiz ID.
- Paid series are locked unless a valid entitlement exists; the API enforces this at attempt start.
- Admin-only entitlement grant/revoke endpoint is available for controlled testing until checkout/pricing are implemented.
- Existing quiz engine, question bank, themes, leaderboard, analytics, Battle Arena and bot remain unchanged.

Important current scope:
- This is the base catalogue/access layer. Bulk question ingestion from JSON/Excel/supported HTML and a dedicated publishing toggle/import preview are next phase; no importer is claimed as included in this package.
- Draft series are not visible to students until an administrator publishes them. For now, publishing can be done with a direct SQL update after content/rights review:
  UPDATE bpsc_nexus_test_series SET is_published=TRUE, updated_at=NOW() WHERE id=<SERIES_ID>;
- Admin grant endpoint: POST /api/admin/test-series/{series_id}/entitlement with {"telegram_id":123,"grant":true,"note":"manual test access"}. Revoke with grant=false.
- Attach test endpoint: POST /api/admin/test-series/{series_id}/tests with {"quiz_id":123,"position":0}.

Deploy by replacing only the two files above, commit/push, and wait for Render redeploy. Do not reset PostgreSQL. This package was syntax-checked locally; no live Render/database test was performed.
