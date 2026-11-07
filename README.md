# BPSC NEXUS — Telegram Quiz Bot

A native Telegram quiz bot with private personal quizzes, native group quiz polls, and an admin-managed bilingual question bank. This package intentionally excludes Mini App, payment, membership-pass, and AI question-generation components.

## Included

- `bot.py` — Telegram application, commands, permissions, database lifecycle.
- `question_bank.py` — admin question bank, guided bilingual question entry, imports, test creation, personal quizzes and review.
- `group_quiz.py` — native Telegram group quiz polls, scheduling, per-question timing, answer explanations and group reports.
- `leaderboard.py` — score ingestion and leaderboard reporting.
- `requirements.txt`, `.env.example`, `render.yaml`, `run_local.sh` — runtime and deployment configuration.

## Confirmed quiz experience

### Personal quizzes

- Custom inline-button answers; no native poll in private quizzes.
- One question per new Telegram message. Previous question messages remain visible.
- No countdown or Next button between questions; the next question is sent automatically after answer feedback.
- Practice mode: correct answers show a collapsed explanation button; incorrect or skipped answers show the explanation automatically.
- Exam mode: answers are withheld until the attempt ends.
- Final performance report is sent automatically, including score, correct, incorrect, unattempted, attempted count, accuracy and elapsed time.
- `Review All Answers` displays the selected answer, correct answer and explanations.

### Group quizzes

- Native Telegram quiz polls, with bilingual English/Hindi question content and options.
- Fixed branding: **BPSC NEXUS**.
- Full question card before each poll, including subject/topic/exam-series labels and structured layouts for standard, statement-based, matching and assertion–reason questions.
- Poll duration is selected by the quiz organiser. A default duration can be used for all questions, with individual overrides by original test question number.
- Poll closes automatically; Telegram reveals the correct option and the bot immediately publishes a separate bilingual explanation message before the next question.
- Poll responses are non-anonymous so individual results can be recorded.
- Group members can schedule published quizzes. Immediate start/pause/resume/stop controls remain group-admin restricted.

### Admin question bank

- Admin access is restricted to Telegram numeric IDs in `ADMIN_TELEGRAM_IDS`.
- Guided manual question entry supports 4 or 5 bilingual options and all four question layouts.
- English and Hindi question text, options and explanations are required for publication.
- Manual duplicate detection warns the admin and offers an explicit save-anyway or discard choice.
- Tests are drafted, populated, reviewed and then manually published. Incomplete bilingual tests cannot be published.

## Database safety

- The bot uses the existing PostgreSQL/Neon database through `DATABASE_URL`.
- Startup performs additive, idempotent schema setup for the quiz engine.
- **This package does not reset, truncate or delete existing user accounts, tests, question banks, attempts, group sessions, results or leaderboards.**
- No destructive database reset script is included. Back up the database before deploying any future data-cleanup operation.

## Configure

Copy `.env.example` to `.env` and set:

- `BOT_TOKEN`
- `DATABASE_URL` — use the existing Neon connection string.
- `ADMIN_TELEGRAM_IDS` — comma-separated Telegram numeric IDs.
- `BOT_MODE=polling` for Termux/Linux, or `BOT_MODE=webhook` for a webhook host.

Install and run:

```bash
python -m pip install -r requirements.txt
chmod +x run_local.sh
./run_local.sh
```

See `LOCAL_SETUP.md` for Android/Termux and Render deployment notes.

## Important runtime rule

Only one active process should consume a bot token at a time. Stop the Render process before starting local polling with the same token. Telegram and Neon both require an internet connection.
