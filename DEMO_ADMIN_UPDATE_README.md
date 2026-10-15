# BPSC Nexus Mini App — 2-Day Demo & Admin Console Update

## Included

- First-visit demo entitlement with a default 48-hour window.
- Premium demo welcome modal and a demo-status banner in the Mini App.
- Server-side demo expiry checks for paid question banks and paid test series.
- Admin-only Mini App console exposed as a dedicated 🔐 Admin bottom-navigation tab and a header shortcut (authorization uses `ADMIN_TELEGRAM_IDS`).
- Option A emoji system applied consistently to the main navigation: 🏠 Home, 📚 Series, 📰 Intel, 📝 Create, 📊 Stats, 🏆 Ranks, 👤 Profile, 🔐 Admin.
- Admin quick links to the existing series-management, leaderboard-moderation, Intel, and membership screens.
- Admin controls to enable/disable new demos, change the duration, and grant/restart a demo for a Telegram user ID.
- One-change access controls for each question-bank subject/collection (`Free` / `Paid`), quiz access overrides (`Inherit` / `Free` / `Paid`), and test-series access (`Free` / `Paid`).
- Audit rows for demo settings/grants and bank/quiz/series access changes.
- Random question order and randomized option mapping on every new quiz attempt, with answer keys remapped in attempt snapshots so grading remains correct.
- Static frontend and membership assets copied to `static/`, including the logo referenced by the splash screen.

## Deployment

1. Back up the current GitHub repository before replacing files.
2. Replace `miniapp.py` and the entire `static/` directory with the versions in this bundle. The Mini App serves `static/index.html` first; updating only the repository-root `index.html` will not change the deployed UI when `static/index.html` exists. Keep `bot.py`, `question_bank.py`, `leaderboard.py`, `group_quiz.py`, `intel.py`, `intel_worker.py`, `schema.sql`, and `requirements.txt` intact unless you have separate changes to them.
3. Deploy the existing `bpsc-nexus-miniapp` Render web service (`python miniapp.py`). Do not reset Neon or run a destructive migration. Additive schema changes run at service startup.
4. Ensure `BOT_TOKEN`, `DATABASE_URL`, and `ADMIN_TELEGRAM_IDS` are configured in the Mini App service. Use the same comma-separated admin Telegram IDs already used by the bot.
5. Open the Mini App as an administrator. Go to **Admin Console → Question-bank access** and set the banks you want to monetize to `Paid`. Existing banks default to `Free` after this additive migration; this avoids unexpectedly locking existing content.
6. Keep `render-intel-cron.yaml` / PIB cron deployment on hold as requested. The Intel code was not redesigned as part of this update.

## Demo behavior

- The 48-hour timer starts on the first authenticated `/api/access` request, which is called when a user opens the Mini App.
- The timer is tied to the Telegram user ID in Neon, not browser storage; clearing app cache does not restart it.
- After expiry, free banks remain available. Paid banks require an active all-access entitlement or an admin-granted entitlement. The admin console can restart a demo for a specific Telegram user ID.
- Changing the demo duration affects new demos; existing trial expiry timestamps are not rewritten.
- To test with a real user, use a test Telegram account. Admin accounts are not assigned a demo automatically.

## Scope note

This update adds the demo, access/pricing controls, and dashboard to the Mini App. Important scope boundary: the new Admin tab centralizes the Mini App admin controls already implemented, and links to other existing admin-capable Mini App screens. Bulk question imports, full question editing/deletion, pass grants/revocations/member management, and payment approval/rejection are still Telegram-admin workflows in this build; they need dedicated Mini App API endpoints and forms before they can be managed entirely in the Mini App.

## Checks performed

- Python syntax compilation for the source modules.
- JavaScript syntax check of the inline Mini App scripts.
- Static frontend copy and referenced logo asset checks.

A live Neon/Render test was not possible from this workspace. Test the service in staging before replacing the production deployment.
