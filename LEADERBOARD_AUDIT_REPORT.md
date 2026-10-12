# BPSC Nexus Global Leaderboard — Audit Report

## Baseline used

This package is built from the newest project files available in the current workspace: the focused Batch 1/2 update (including the universal question importer, membership page, and current Mini App frontend), plus the existing `group_quiz.py`, Render configuration, schema, payment migration and ULUK asset. The earlier named full V8 ZIP was not present as a local archive, so the available source files were used rather than asking for another upload. Battle Arena's API and panel behavior were not changed; its existing “Coming Soon” presentation remains.

## Existing score storage

- **Mini App CBT:** `bank_quiz_attempts` stores owner Telegram ID, quiz ID, start/submission timestamps, score and correct/incorrect/skipped counts. `bank_quiz_attempt_question_snapshots` preserves the question and answer key used for the attempt; `bank_quiz_attempt_answers` stores responses. `miniapp.py` validates Telegram Mini App `initData` and grades from server-side snapshots using the quiz's `positive_marks` and `negative_marks`.
- **Telegram question-bank quiz engine:** `question_bank.py` writes into the same `bank_quiz_attempts` and `bank_quiz_attempt_answers` tables. Its submit and timeout handlers calculate score server-side. The leaderboard adapter uses the shared attempt ID as an idempotency key so a shared row cannot count twice.
- **Telegram group quizzes:** `group_quiz_sessions`, `group_quiz_session_questions`, and `group_quiz_answers` store completed sessions, question snapshots/correct answers, and individual user votes. On session completion, participant results are validated from stored answers and answer keys and ingested once per `(session, Telegram user)`.
- **Daily Challenge:** the inspected project did not have a separate official challenge registry. This release adds an admin-managed mapping from a published quiz to a UTC challenge date. The Daily tab stays empty until an admin configures a challenge. Per user, only the best eligible attempt for that challenge/date ranks.
- **Niva:** no separate authoritative Niva scoring/submission handler was identified in the available files. If Niva writes into the shared `bank_quiz_attempts` flow, those completed attempts are supported; a separate scoring store is not automatically included.
- **Legacy schema:** `schema.sql` also contains older `tests`/`attempts` tables. The active Mini App uses the `bank_*` tables, so the new leaderboard integrates those active paths rather than inventing results from the legacy tables.

## Ranking policy

Each completed assessment is normalized to a 100-point scale: `score / maximum positive marks * 100`. Weekly, monthly, subject-wise and lifetime leaderboards sum normalized points, avoiding direct raw-score comparison between tests with different lengths/marking schemes. Raw score, maximum score, correct/incorrect/skipped counts, accuracy and time remain separately stored. Tie-breaks are higher aggregate accuracy, lower average time per question, earlier first verified submission, then stable Telegram ID ordering. Week boundaries and calendar-month boundaries use UTC. Daily Challenge uses the best normalized result for a user on the configured challenge date, preventing repeat attempts from accumulating unlimited daily points.

## Data safety

The migration is additive and `CREATE/ALTER ... IF NOT EXISTS`; it does not drop or reset existing tables. The central attempt ledger uses a unique source key. Summary rows update only when the ledger insert is new. Admin exclusions update eligibility, write an audit record, and rebuild summaries while retaining the original source attempt row. Public ranking responses expose display names and aggregate ranking metrics only, not Telegram IDs, usernames, payment data or private response history.

## Known limitations

- Group results are integrated at completed-session level; users with no recorded answer row are not known participants and cannot be ranked for that session.
- Daily Challenge is admin-configured by published quiz and UTC date; it is not inferred from quiz titles.
- Current subject-wise rankings rely on the subject stored on the quiz. Mixed quizzes tagged “All subjects” appear in All subjects, not fabricated subject buckets.
- The runtime uses the available `ADMIN_TELEGRAM_IDS` allowlist for admin endpoints. Set it on the Mini App Render service as well as the bot service for challenge management and exclusion controls.
- Production database behavior and live Render deployment could not be verified from this workspace.
