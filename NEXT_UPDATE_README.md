# BPSC Nexus Mini App — next update patch

## Files to update in Git
- `miniapp.py`
- `question_bank.py`
- `static/index.html`
- `static/quix-logo.png`
- `NEXT_UPDATE_MIGRATION.sql` (manual migration reference; startup also creates the schema idempotently)

## Implemented in this patch
1. **QUIX foundation** — new home-screen feature card using the transparent QUIX logo; dedicated QUIX page and category filters for Daily Dose, Fact Drop, Bihar Special, Last-Minute Revision, Quick Revision, Quick PYQ and Mini Tests; admin create/publish/delete flow; mini-test posts can link to an already-published quiz.
2. **Dedicated quiz route** — all existing quiz launchers continue through the central `startQuiz()` function, which opens the existing quiz UI in a fixed full-screen internal Mini App route. Existing quiz styles and controls are retained. Exit prompts are shown; attempt answers and exam deadline remain server-side, and the active attempt ID is retained in session storage for resume after accidental reload.
3. **Question Intelligence foundation** — deterministic question-type detection expanded with additional types; existing structured question data is rendered for assertion/reason, statement, matching, pair, chronology/sequence and passage layouts. Classification confidence/source/review metadata, admin review queue and admin override endpoint are added.
4. **Home dashboard** — compact combined study/quiz progress, Continue Learning activity context, QUIX feature card and three-item schedule timeline. The existing full planner remains accessible from “View Planner”.

## Important scope notes
- This is a first implementation layer, not the full previously discussed AI-powered MCQ engine. No external AI classifier is invoked; classification is deterministic with an admin override/review queue. Multi-correct answer storage/scoring, full support for every one of the 15 approved question families, linked shared-passage groups, classification history/restore, and AI fallback remain follow-up work.
- QUIX currently supports admin-authored text content and links Mini Test cards to existing published quizzes. Rich image/table/map authoring, scheduling, analytics, and bulk publishing are not included in this patch.
- Home progress is derived from existing task and completed-attempt APIs. It counts questions in completed tests, not live per-question answer events.
- Run the app against a staging database first. `miniapp.py` runs the schema additions at startup; the SQL file is provided for controlled/manual deployment.

## Validation performed
- Python syntax compilation for `miniapp.py` and `question_bank.py`.
- Node.js syntax check of all inline JavaScript in `static/index.html`.
- Static route and component presence checks.
- Live Telegram Mini App, PostgreSQL migration execution, and end-to-end attempt tests require the deployed environment and were not run here.
