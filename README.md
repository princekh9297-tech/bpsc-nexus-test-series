# BPSC Nexus — Dual Quiz System

This update extends the current BPSC Nexus Python Telegram bot and PostgreSQL question bank with two separate quiz experiences:

1. **Personal CBT Mini App** — a responsive bilingual web interface inside Telegram for independent practice and timed exams.
2. **Group Quiz Polls** — native Telegram quiz polls posted one at a time in a group, with a configurable voting interval, per-user scoring, leaderboard and aggregate results.

The existing HTML/JSON/CSV import flow, question verification, import history, question bank, quiz builder and Premium Pass/admin membership features remain in place. Photo/PDF extraction remains deferred and is not part of the active workflow.

## What's included

- `bot.py` — existing Telegram bot plus Mini App launch button, group quiz command and poll-answer handling.
- `question_bank.py` — permanent question bank, import preview, deduplication, verification, quiz builder and existing personal Telegram quiz flow.
- `group_quiz.py` — persistent sequential group-poll scheduler and leaderboard calculations.
- `miniapp.py` — authenticated API and static frontend server for personal CBT.
- `static/index.html` — responsive CBT UI with timer, question palette, answer navigation, mark-for-review, clear response, submit confirmation and result review.
- `requirements.txt` — runtime dependencies for both services.
- `render.yaml` — existing bot service definition with the new optional `MINI_APP_URL` setting.
- `render-miniapp.yaml` — optional Blueprint definition for the separate Mini App service.
- `question_bank_template.csv`, `question_bank_template.json` — bulk import templates.
- `premium_pass_migration.sql` — compatibility migration reference. Normal startup performs idempotent migrations automatically.

## 1. Update the existing Telegram bot service

1. Back up the current GitHub repository.
2. Copy/replace the packaged `bot.py`, `question_bank.py`, `group_quiz.py`, `requirements.txt`, `README.md` and `render.yaml` into the repository. Add `miniapp.py`, the `static/` directory, `render-miniapp.yaml`, and the two import templates.
3. Keep the existing Render values for `BOT_TOKEN`, `DATABASE_URL`, `WEBHOOK_SECRET` and `ADMIN_TELEGRAM_IDS`. Do not paste secrets into GitHub.
4. Deploy the bot service. Its startup migration creates the group-quiz tables and ensures the question-bank schema. It does not drop existing tables.
5. Confirm `/admin`, bulk imports, question review, quiz creation, quiz publishing and Premium Pass management still work before launching group quizzes.

The bot can run group quizzes without the Mini App URL configured. The Mini App button appears once `MINI_APP_URL` is set to the new service's public HTTPS URL. Startup also configures Telegram's built-in slash-command menu for private chats and groups. Group menu commands are `/groupquiz`, `/help`, and `/start`.

## 2. Deploy the personal Mini App as a separate Render Web Service

The Mini App must have its own public HTTPS endpoint. Do not change the existing bot service's start command to `python miniapp.py`; the bot and Mini App are separate services.

Create a second Render Web Service using the same GitHub repository and root directory:

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python miniapp.py`
- **Environment variables:** copy the exact existing `BOT_TOKEN` and `DATABASE_URL` values from the bot service.
- **Health check path:** `/healthz` (if configurable).

Alternatively, use `render-miniapp.yaml` as the service definition for a separate Blueprint deployment. After deployment, copy the Mini App service's actual HTTPS URL.

Then open the existing bot service's Render Environment and set:

- `MINI_APP_URL` = the Mini App service's public HTTPS URL (for example, `https://your-actual-miniapp-service.onrender.com`).

Redeploy the bot service after setting `MINI_APP_URL`. Do not use an example URL unless it matches the actual service address.

## 3. How students use personal CBT

1. Open the bot privately and tap **Open Personal CBT**.
2. Choose a published quiz.
3. Choose Practice or Exam mode.
4. Navigate using Previous/Next or the question palette. Mark questions for review or clear a response.
5. Submit the test to see correct/incorrect/unattempted counts, score, accuracy and explanations.

The API validates Telegram Mini App `initData` on the server using the bot token and rejects invalid or stale launch data. The frontend does not receive correct answers before submission in Exam mode. A timed exam's deadline is checked server-side, not only by the browser timer.

## 4. How group admins run a group quiz

1. Add the bot to the Telegram group and ensure it is allowed to send polls. Promote it with poll/post permissions if the group restricts those actions.
2. Publish a quiz from the private admin panel's **Question Bank & Quiz Studio**.
3. In the destination group, a group administrator runs `/groupquiz`.
4. Choose a published quiz, then choose a voting window: 15 seconds, 30 seconds, 1 minute, 2 minutes or 5 minutes.
5. The bot posts one native quiz poll, closes it at the configured window and then posts the next question.
6. At the end, the bot posts aggregate statistics and a leaderboard (1 point correct, −1/3 incorrect; unanswered questions score 0).

Group polls are non-anonymous because individual scores require identifying each voter's answer. Telegram displays its native quiz feedback after a user answers; the bot cannot force Telegram to display a custom explanation panel in every situation. The explanation is attached to each poll. The scheduler stores session progress in PostgreSQL so it can recover after ordinary restarts. If a crash occurs during the exact send-poll/database-write boundary, the session pauses rather than blindly sending a potentially duplicate question; start a new session after checking the group.

## 5. Bulk question import

Use `/admin` privately → **Question Bank & Quiz Studio** → **Import HTML / JSON / CSV**. Select a default subject, send the file, review the preview and confirm the import.

- JSON supports a root array or arrays under `questions`, `items`, `data`, `results`, `mcqs` or `records`.
- CSV should use the supplied header template.
- HTML support covers common embedded question JSON and recognizable question/option markup. Custom HTML layouts can vary; convert unsupported files to JSON/CSV.
- Questions with missing or invalid answer keys remain pending verification and cannot be used in scored quizzes until reviewed.
- Importing does not rewrite the source wording or invent answers. Duplicate questions are detected by normalized question and option content.
- Import batch rollback removes only questions not already referenced by a quiz.

## 6. Data safety and operational notes

- Existing `app_users`, `premium_passes`, `pass_catalogue`, published legacy tests and their rows are not dropped or intentionally rewritten. The question bank and group quiz sessions use separate tables.
- Both services must use the same `DATABASE_URL`; otherwise they will not share questions or attempts.
- Keep `BOT_TOKEN` and `DATABASE_URL` only in Render environment variables. Never embed either in `static/index.html` or commit them to GitHub.
- Telegram Mini App authentication depends on the exact bot token and a valid Telegram launch. Opening the service URL directly in a normal browser will not authenticate a user.
- The free Render plan may sleep. Timed tests can still be interrupted by restarts; reliable scheduled group delivery needs an always-on service/worker. The persistent scheduler helps recover due sessions after the bot wakes but cannot send while the process is asleep.
- Test with a private test group and non-paying test users before broad release. Verify Premium Pass management, imports, personal results, group scoring, poll closure, and restart recovery.

## Suggested release test

1. Import a small CSV with 5 verified questions.
2. Build and publish a 5-question quiz.
3. Run it in personal Practice mode and Exam mode.
4. Run `/groupquiz` in a test group with a 15-second interval and two test accounts.
5. Confirm answers, explanations, leaderboard and aggregate counts.
6. Check Render logs and confirm existing membership records are unchanged.
