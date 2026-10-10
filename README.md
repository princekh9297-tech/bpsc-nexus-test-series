# BPSC Nexus Telegram Bot — Question Bank + Quiz Delivery

This update keeps the existing Premium Pass/admin membership features and adds a permanent categorized question bank with bulk import and in-bot quizzes. Photo/PDF extraction is intentionally deferred and is not exposed in the menu or command handlers in this phase.

## Phase 1 features

- **HTML / HTM / JSON / CSV bulk import** directly through the authorized admin's private chat with the bot. Imports are local parsers; no AI API calls are needed.
- **Import preview and confirmation** showing recognized rows, ready questions, pending verification, likely duplicates and invalid rows before any permanent insert.
- **Permanent question bank** with subject, topic, source, attribution, bilingual question/explanation fields, options, answer key, status and import-batch reference.
- **Safe duplicate detection** based on normalized question and options. Suspected duplicates are skipped at database insertion; identical questions are not silently duplicated.
- **Answer verification queue** for imported questions without a valid answer key. Pending questions cannot be used in scored quizzes until verified.
- **Forward or paste a formatted MCQ** into the bot as an admin to preview and save it. The expected format is a question line, options such as `A. ...`, `B. ...`, and optional `Answer: B` / `Explanation: ...` lines.
- **Question management** with search, details, edit, archive and delete-if-unused. Questions referenced by quizzes are protected from deletion.
- **Import history and rollback**. Rollback removes questions from that batch only when no quiz references them; questions already used by a quiz are retained.
- **Quiz builder** to choose a title, subject and 1–100 verified questions. Quizzes are built from existing bank records; the same question can be reused in multiple quizzes.
- **Student quiz delivery in private chat** using `/quiz` or **Take a Quiz**. Practice mode provides immediate answer feedback/explanations. Exam mode hides correctness until submission, applies a timer and shows result analysis, answer review and explanations after submission.
- **Scoring**: +1 for correct, −1/3 for incorrect and 0 for not attempted. Exam duration defaults to 60 minutes in this first release.
- **Automatic database migration** for the new tables. No manual SQL execution is required; the app applies idempotent `CREATE TABLE/INDEX IF NOT EXISTS` statements at startup.
- **Admin-only import and management** are checked server-side using `ADMIN_TELEGRAM_IDS` and private-chat access.

## Import file formats

### JSON

Accepted as a root array or an object containing a `questions`, `items`, `data`, `results`, `mcqs`, or `records` array. Common question, option, answer and explanation field names are supported. Example:

```json
{
  "questions": [
    {
      "question_en": "What is the capital of Bihar?",
      "question_hi": "बिहार की राजधानी क्या है?",
      "options": {"A": "Patna", "B": "Gaya", "C": "Delhi", "D": "Pune"},
      "correct_option": "A",
      "explanation_en": "Patna is the capital of Bihar.",
      "subject": "Bihar Special",
      "topic": "Geography",
      "source": "Example source"
    }
  ]
}
```

### CSV

Use a header row. Recommended columns: `question_en`, `question_hi`, `option_a`, `option_b`, `option_c`, `option_d`, optional `option_e`, `correct_answer`, `explanation_en`, `explanation_hi`, `subject`, `topic`, `source`, `source_url`, `attribution`. `question`, `optionA`-style aliases and several common answer-key aliases are also supported.

### HTML / HTM

The importer supports quiz data embedded in common JSON script blocks or JavaScript `questions`/`quizData` arrays, plus common question-card markup with recognizable question and option classes. HTML layouts vary; if a page's structure cannot be recognized reliably, export its questions to the supplied JSON/CSV shape instead of assuming every website's HTML is identical.

## Deploy to the existing Render service

1. Back up the current GitHub repository.
2. Add/replace **`bot.py`**, **`question_bank.py`**, **`requirements.txt`**, and **`README.md`** from this package. Keep your existing `render.yaml` and database connection/environment values.
3. In Render, keep `BOT_TOKEN`, `DATABASE_URL`, `WEBHOOK_SECRET`, and `ADMIN_TELEGRAM_IDS` unchanged. `GEMINI_API_KEY` is not required for this phase; photo/PDF extraction is disabled.
4. Commit and push the files. Render should install the reduced requirements and redeploy.
5. Open the bot privately, use `/admin`, then **Question Bank & Quiz Studio → Import HTML / JSON / CSV**. Choose a default subject, send the file, review the preview and confirm.
6. Review pending answer keys, then build and publish a quiz. Students can use `/quiz` in their private chat.

## Data safety and limitations

- Existing app users, Premium Pass rows, pass catalogue and existing published tests are not dropped or rewritten. The new question bank uses separate tables.
- The question bank is permanent and separate from quizzes. Quiz references prevent accidental deletion of questions already in use.
- Importing does not rewrite wording or invent missing answers. Questions without a valid answer remain pending verification.
- HTML parsing cannot guarantee support for every custom website layout; JSON or CSV is the most predictable exchange format.
- This phase delivers an interactive Telegram quiz in private chat, not a browser-based visual CBT dashboard. Exam timers are enforced by the running bot process; an in-progress attempt can be interrupted by a hosting restart.
- Photo/PDF extraction, AI-generated tagging, multi-select bulk editing and advanced charts are intentionally deferred.
- Keep secrets out of GitHub. Render's free service may sleep, and a deployment restart interrupts live exam sessions.
