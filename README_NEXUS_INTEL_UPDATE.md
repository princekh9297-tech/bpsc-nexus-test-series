# BPSC Nexus Intel — Patch for existing `bpscnexustestbot` repository

This is an additive feature update for the EXISTING BPSC Nexus bot/Mini App repository. It is NOT a new bot or a separate replacement project.

## Files in this patch
- `bot.py` — updated existing bot; adds Nexus Intel controls to the existing admin flow.
- `miniapp.py` — updated existing Mini App API service; adds approved-only Intel endpoints.
- `index.html` — updated existing Mini App frontend; adds the Intel/Current Affairs view.
- `intel.py` — new Intel module and additive PostgreSQL schema setup.
- `intel_worker.py` — new scheduled PIB ingestion/processing worker.
- `render-intel-cron.yaml` — a reference service definition for the additional worker only.

## Apply to your existing repository
1. Back up your repository and PostgreSQL database.
2. Open your existing GitHub repository `bpscnexustestbot`.
3. Replace the existing `bot.py`, `miniapp.py`, and `index.html` with the corresponding files in this ZIP. Add `intel.py` and `intel_worker.py` at the repository root.
4. Do NOT delete or replace `group_quiz.py`, `question_bank.py`, `leaderboard.py`, `schema.sql`, `requirements.txt`, `render.yaml`, or `render-miniapp.yaml`.
5. Keep your existing Render bot and Mini App services and their environment variables. The bot and Mini App must continue using your existing `DATABASE_URL`.
6. Configure a separate Render Cron Job for `python intel_worker.py` using `render-intel-cron.yaml` as a reference. Merge/add the cron service; do not replace your existing `render.yaml` with the reference file.
7. Set `PIB_FEED_URL` only after verifying the official PIB RSS/Atom endpoint. Set `DATABASE_URL` to the same existing database on the worker. Set `BOT_TOKEN` and `INTEL_TELEGRAM_CHAT_ID` if you want Telegram publishing. AI draft generation additionally needs `INTEL_AI_BASE_URL`, `INTEL_AI_API_KEY`, and `INTEL_AI_MODEL`.
8. Run `python -m py_compile bot.py miniapp.py intel.py intel_worker.py` before deployment.
9. Deploy to a staging service/database first if available. Check admin approval, approved-only Mini App visibility, and existing membership/quiz/Battle Arena behavior before production rollout.

## Safety and boundaries
- This patch is for the existing `bpscnexustestbot` repository; it does not create a second Telegram bot.
- It uses additive `intel_*` tables and does not intentionally drop or reset existing tables.
- It does not intentionally change membership entitlements or Battle Arena backend logic.
- Admin actions rely on the existing admin allowlist. Keep `ADMIN_TELEGRAM_IDS` unchanged.
- Drafts must be approved before publication. AI output is not fact-checked merely because it was generated.
- No live database migration or deployment is performed by creating this ZIP.
