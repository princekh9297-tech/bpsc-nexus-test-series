# BPSC Nexus Advanced Quiz System — v2 changes

## Added
- Import Telegram quiz polls in the bot's private chat as an authorized admin; the bot previews before saving. Polls without a known answer key remain pending verification.
- Quiz Manager: edit title, total duration, positive/negative scoring, publication status, and question membership; add or remove questions from the verified bank.
- Question Bank Quality Audit: reports ready/pending/archived totals, records without explanations, unclassified records, and subject coverage.
- Shareable Telegram deep links for published quizzes using `/start quiz_<id>`.
- Personal CBT recent-results history and reopenable detailed reports.
- Group quiz builder from the verified question bank: mixed-subject or subject-specific pool, 5–100 question selection (or use the entire pool when fewer than five exist), Learning or Exam Simulation, and per-question voting windows.
- Group Exam Simulation uses regular polls and reveals the correct answer/explanation after the voting window closes.
- Future group quiz scheduling with `/groupquiz_schedule <quiz_id> <YYYY-MM-DDTHH:MMZ> [15|30|60|120|300] [learning|exam]`; scheduled time is UTC.
- `/grouprank` cumulative leaderboard for completed quizzes in the current group.
- Best-effort group-level repeat avoidance when building fresh bank quizzes.

## Explicitly excluded
- PDF-to-quiz generation and PDF/photo question extraction remain disabled.

## Verification
- Python syntax compilation passed for `bot.py`, `question_bank.py`, `group_quiz.py`, and `miniapp.py`.
- Frontend JavaScript syntax check passed with Node.js.
- Live Telegram and PostgreSQL integration tests were not run in this environment; deploy to a staging service and verify using a test group first.


## Premium Mini App redesign (2026-10-12)
- Added a five-tab mobile-first shell: Home, Test Series, Create, Analytics, Profile.
- Added four user-selectable palettes: Graphite Blue, Midnight Violet, Emerald Pro, and Ivory Cobalt. Theme selection persists in localStorage.
- Added home shortcuts and a profile theme selector.
- Preserved existing API routes, CBT flows, question-bank imports, group quiz engine, and database schema.
- PDF-to-quiz remains excluded.
- Validation is syntax/static only; live Telegram and PostgreSQL integration testing remains required.
