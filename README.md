# BPSC Nexus — Telegram quiz bot (Phase 1)

A Telegram-only quiz bot for BPSC preparation. No Mini App, no payments: every quiz is free for every user.

## What it does now
- **Personal quizzes** in private chat: `/quiz` → pick a quiz → Practice or Exam → answer with buttons → score and review.
- **Group quizzes** in any group: `/startquiz`, `/pausequiz`, `/resumequiz`, `/stopquiz`, `/schedulequiz`, `/leaderboard`, `/quizstatus`, `/quizbrand` (group admins only). Any group can use them; the bot registers the group automatically.
- **Admin workspace** (`/admin`, private chat, `ADMIN_TELEGRAM_IDS` only): compact Quiz Studio with Create Quiz, Manage Quizzes, Question Bank, Subjects & Collections, and Tools & Maintenance. Personal quizzes support automatic random or manual question selection, including mixed-subject pools. Group quizzes continue through `/startquiz` and `/schedulequiz` in the target group.
- **Telegram menu button**: startup explicitly resets the chat menu button to Telegram Commands, removing any previously configured Web App/Mini App menu button.
- **Reset Question Bank** (`/admin` → Quiz Studio → Tools & Maintenance → ☢️ Reset Question Bank): shows counts, then requires you to type `RESET`. Permanently deletes questions, quizzes, personal attempts, group quiz history and the leaderboard. Keeps users, subjects, group names/branding and Nexus Intel news.
- `/myanalysis`: private analysis of your group quiz performance.

## Environment variables (Render → Environment)
| Name | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | yes | from @BotFather |
| `DATABASE_URL` | yes | Neon/Postgres connection string |
| `WEBHOOK_SECRET` | yes | random string, 16+ chars |
| `ADMIN_TELEGRAM_IDS` | yes | comma-separated numeric Telegram IDs |
| `RENDER_EXTERNAL_URL` | auto | set by Render |

Removed in Phase 1 (you can delete them from Render): `MINI_APP_URL`, `ADMIN_LOGIN_ID`, `ADMIN_LOGIN_PASSWORD`, `ADMIN_LOGIN_TELEGRAM_ID`, `ADMIN_SESSION_SECRET`.

## Deploy
1. Back up your current repo, then replace its files with this folder.
2. Delete the separate Mini App web service on Render (not needed any more).
3. Redeploy the bot service. Startup migrations are additive; old tables (passes, payments, tests) stay in the database untouched and are simply unused.
4. In Telegram, send `/start`, `/quiz`, then `/admin` to confirm.

## Files
`bot.py` entry point · `question_bank.py` question bank + personal quiz · `group_quiz.py` group polls/scheduler · `leaderboard.py` · `intel.py`, `intel_worker.py` news feed (optional cron: `render-intel-cron.yaml`).
