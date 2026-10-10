# BPSC Nexus Telegram Bot — Premium Pass + Quiz Creator

This package is a GitHub-ready update for the existing Python Telegram webhook bot. It retains the private, allowlisted Premium Pass admin panel and adds a photo/PDF Quiz Creator Studio.

## What is included

- **Safer automatic database migration:** creates `premium_passes` if missing, creates/verifies the pass catalogue, backfills missing pass IDs for legacy rows, and makes every new grant resolve the selected pass to a real pass_catalogue.id before inserting. It preserves existing passes and logs the exact exception when a grant fails. No manual SQL paste should be required for these additions.
- **Premium Pass admin panel:** `/admin`, admin ID allowlist, grant/manage/revoke passes, and multiple independent passes.
- **Quiz Creator Studio:** photo/screenshot and PDF input; Gemini extraction to JSON; saved drafts; question preview; uncertain answers are not guessed and cannot be published; Telegram quiz-poll publishing; draft listing and discard.
- **Trial limits:** non-premium users can extract up to 5 questions per quiz, PDFs up to 5 pages, and 2 creations per day. An active Premium Pass unlocks up to 50 questions and 30-page PDFs. Group publishing is gated to users with an active pass and verified group-admin status.
- **File safety:** maximum 10 MB upload; source must be a photo or PDF. Only users who select Create from Photo/PDF have the next upload processed.

## Deploy to existing Render service

1. Back up your current GitHub files.
2. Replace `bot.py`, `requirements.txt`, `premium_pass_migration.sql`, and `README.md` with these files. Keep your existing `render.yaml`. Keep your existing `render.yaml` unless you intentionally want to change the service.
3. In Render Environment, keep the existing `BOT_TOKEN`, `DATABASE_URL`, `WEBHOOK_SECRET`, and `ADMIN_TELEGRAM_IDS` values unchanged. Add `GEMINI_API_KEY` from Google AI Studio. Optionally set `GEMINI_MODEL` (default `gemini-2.5-flash`). Never paste secrets into GitHub.
4. Commit/push the files and let Render redeploy. The startup migration creates/seeds `pass_catalogue`, fills missing `premium_passes.pass_id` values, enforces the catalogue relationship, and adds the Quiz Creator tables automatically.
5. In Telegram, open the bot privately and send `/start`, then tap **Create Quiz from Photo/PDF**. Choose Photo or PDF, then send the file.
6. Test the pass grant flow using a test account before granting paid access to real customers. Check Render logs if it fails; the new log records the underlying DB exception type and detail.

## Important behavior and limitations

- Gemini API usage may incur charges under your Google AI account and is subject to that account's quotas. Configure spending/usage limits in Google AI Studio/Cloud as available.
- Extracted questions are saved as drafts. The preview shows the first three questions and flags uncertain answers. The bot blocks publication if an answer is missing or marked low-confidence; verify the answer key before publishing.
- Publishing sends one Telegram quiz poll per question. This is not yet a full timed CBT engine, a payment gateway, or a complete group-subscription billing system.
- In this version, any active student Premium Pass unlocks the larger Quiz Creator limits. Group publishing also requires an active pass and verified group-admin status. Group-specific paid subscriptions and reusable, categorized question libraries can be built as the next stage.
- Render's free service can sleep. For dependable scheduled quizzes and production usage, use an always-on hosting plan.

## Required environment variables

- `BOT_TOKEN`
- `DATABASE_URL`
- `WEBHOOK_SECRET` (random string, at least 16 characters)
- `ADMIN_TELEGRAM_IDS` (comma-separated numeric Telegram IDs)
- `GEMINI_API_KEY` (required for photo/PDF extraction)
- `GEMINI_MODEL` (optional; defaults to `gemini-2.5-flash`)
