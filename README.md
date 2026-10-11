# BPSC Nexus — Dual Quiz System

This update extends the current BPSC Nexus Python Telegram bot and PostgreSQL question bank with two separate quiz experiences:

1. **Personal CBT Mini App** — a responsive bilingual web interface inside Telegram for independent practice and timed exams.
2. **Group Quiz Polls** — native Telegram quiz polls posted one at a time in a group, with a configurable voting interval, per-user scoring, leaderboard and aggregate results.

The existing HTML/JSON/CSV import flow, question verification, import history, question bank, quiz builder and Premium Pass/admin membership features remain in place. Photo/PDF extraction remains deferred and is not part of the active workflow.

## What's included

- `bot.py` — existing Telegram bot plus Mini App launch button, forwarded Telegram poll import, quiz deep links, group quiz/scheduling/leaderboard commands and poll-answer handling.
- `question_bank.py` — permanent question bank, HTML/JSON/CSV and Telegram poll imports with confirmation preview, deduplication, verification queue, bank quality audit, quiz editor (title/scoring/duration/question membership), and the existing personal Telegram quiz flow.
- `group_quiz.py` — persistent sequential group-poll scheduler and leaderboard calculations.
- `miniapp.py` — authenticated API and static frontend server for personal CBT, including private completed-attempt history.
- `static/index.html` — responsive CBT UI with a custom quiz builder, subject/topic filters, question count, random/sequential/unseen/incorrect selection, Practice/Exam/Revision/Rapid-fire presets, scoring settings, total and per-question timers, shuffled options, saved-on-device setup, question palette, mark-for-review, answer clearing, detailed result review and recent result history.
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

The bot can run group quizzes without the Mini App URL configured. The Mini App button appears once `MINI_APP_URL` is set to the new service's public HTTPS URL.

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

The API validates Telegram Mini App `initData` on the server using the bot token and rejects invalid or stale launch data. The frontend does not receive correct answers before submission in Exam mode. The overall exam deadline is checked server-side; the optional per-question countdown is a client-side pacing aid and should not be treated as a tamper-proof proctoring control.

## 4. How group admins run a group quiz

1. Add the bot to the Telegram group and ensure it is allowed to send polls. Promote it with poll/post permissions if the group restricts those actions.
2. Publish a quiz from the private admin panel's **Question Bank & Quiz Studio**.
3. In the destination group, a group administrator runs `/groupquiz` (also available from Telegram's command menu).
4. Choose **Build a quiz from question bank** or select an existing published quiz. The builder supports mixed-subject or subject-specific question pools and 5–100 questions.
5. Choose Learning mode or Exam simulation, then a voting window of 15 seconds, 30 seconds, 1 minute, 2 minutes or 5 minutes per question.
6. The bot posts one poll at a time. Learning mode uses Telegram quiz polls, which reveal the correct answer after voting. Exam simulation uses regular polls and posts the answer/explanation only after the voting window closes.
7. At the end, the bot posts aggregate statistics and a leaderboard (1 point correct, −1/3 incorrect; unanswered questions score 0).

Group polls are non-anonymous because individual scores require identifying each voter's answer. The scheduler stores session progress and copied question content in PostgreSQL. If a crash occurs during the exact send-poll/database-write boundary, the session pauses rather than blindly sending a potentially duplicate question; check the group before starting another session.

## 5. Bulk question import

Use `/admin` privately → **Question Bank & Quiz Studio** → **Import HTML / JSON / CSV**. Select a default subject, send the file, review the preview and confirm the import.

- JSON supports a root array or arrays under `questions`, `items`, `data`, `results`, `mcqs` or `records`.
- CSV should use the supplied header template.
- HTML support covers common embedded question JSON and recognizable question/option markup. Custom HTML layouts can vary; convert unsupported files to JSON/CSV.
- Questions with missing or invalid answer keys remain pending verification and cannot be used in scored quizzes until reviewed.
- Importing does not rewrite the source wording or invent answers. Duplicate questions are detected by normalized question and option content.
- Import batch rollback removes only questions not already referenced by a quiz.


### Group bank builder and scheduled quizzes

- `/groupquiz` → **Build from Question Bank** → choose a subject or mixed subjects → choose a question count → choose Learning or Exam simulation → choose the voting window.
- Group bank-built sets are sampled from verified questions and avoid questions used in completed quizzes in that group where possible; if the unused pool is too small, the selector falls back to the eligible bank rather than blocking the quiz.
- `/groupquiz_schedule <quiz_id> <YYYY-MM-DDTHH:MMZ> [15|30|60|120|300] [learning|exam]` schedules a published quiz for a future UTC time. Example: `/groupquiz_schedule 12 2026-10-12T18:00Z 30 exam`. Schedule at least two minutes ahead.
- `/grouprank` displays cumulative standings across completed quizzes in the current group.

### Importing a Telegram poll

Send or forward a Telegram quiz poll to the bot in a private chat as an authorized admin. The bot previews it and waits for confirmation before saving. Quiz polls with an answer key can be imported as verified records; regular polls without a visible answer key remain pending review. PDF-to-quiz generation is intentionally excluded.

### Quiz Manager

In **Admin → Question Bank & Quiz Studio**, open **Manage Quizzes** to edit quiz title, duration, scoring, publication status, and question membership. Existing attempts keep their own question snapshots; changes apply to future attempts. **Bank Quality Audit** reports pending, unclassified and explanation-deficient records.

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
