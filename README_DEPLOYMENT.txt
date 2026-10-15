BPSC NEXUS MINI APP — ADMIN + DEMO REGISTRATION UPDATE

Files in this patch:
- index.html: Mini App frontend; adds an always-visible Admin entry point, demo registration gate, demo registration dashboard, CSV export, and quick test-series deletion.
- miniapp.py: backend endpoints and additive PostgreSQL schema migration for demo registrations.

DEPLOYMENT
1. Back up the current index.html and miniapp.py in your Mini App repository.
2. Replace those two files with the versions in this ZIP. Keep question_bank.py, leaderboard.py, intel.py, requirements.txt and other existing project files unchanged.
3. Commit and push to GitHub; allow Render to redeploy the Mini App service.
4. Keep the existing DATABASE_URL and BOT_TOKEN. Admin login still relies on the existing Render variables:
   ADMIN_LOGIN_ID
   ADMIN_LOGIN_PASSWORD
   ADMIN_LOGIN_TELEGRAM_ID
   ADMIN_TELEGRAM_IDS (must include ADMIN_LOGIN_TELEGRAM_ID)
   ADMIN_SESSION_SECRET (recommended)
5. The new demo-registration table is created additively by ensure_access_schema on first use; it does not delete existing tables or trial records.

FEATURES
- Admin entry is always visible, but all admin APIs remain server-authorized.
- Admin dashboard retains the existing payment, member/pass, imports, question manager, cleanup, pricing/access and leaderboard tools.
- Demo registration requires a Telegram username or numeric ID before a new demo starts. The actual Telegram account ID is obtained from Telegram's signed Mini App launch data and is the identity used for access control; the submitted contact is stored separately for admin review.
- Admin > Demo registrations includes registered users, active/expired demo status, 2/7/30-day filters, copy ID, and CSV export.
- Test series list in the Admin Console includes a delete action with confirmation. Existing backend route is reused.

VALIDATION
- Python syntax checked with py_compile.
- Frontend inline JavaScript syntax checked with Node.
- Live Render/API/database integration was not tested from this environment. Deploy and verify admin login, registration, trial start, and test-series actions on the live service.
