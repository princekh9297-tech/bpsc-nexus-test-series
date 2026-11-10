# Quiz Studio Correction Report

## Implemented
- Private student quiz delivery now uses Telegram native quiz polls (`send_poll`, `type="quiz"`, non-anonymous).
- Private poll answers are routed through `PollAnswerHandler`, mapped to attempt/question IDs, persisted, and advanced sequentially.
- Telegram quiz polls carry the bilingual explanation so Telegram reveals the correct answer and explanation immediately after voting.
- Private attempts use Practice Mode only; the mode selector no longer offers Exam Mode.
- Group quiz setup no longer offers Exam Mode, and group questions are always sent as native Telegram quiz polls.
- Poll imports in the admin private chat now validate the supported 2–5 option range and require explicit destination-subject selection.
- Plain-text MCQ detection supports A), A., A-, Option A and common answer/explanation labels, including Hindi labels.
- Private option shuffling is now applied to the actual poll option order while preserving correct-answer mapping.
- Quiz Studio settings display only the controls currently applied by the private poll engine; option shuffle can be toggled.
- Added `bank_quiz_attempt_polls` schema for durable poll-to-attempt mapping.

## Important constraints
- Native Telegram polls do not support arbitrary back-navigation. Private Practice Mode is sequential.
- The private native-poll flow does not currently enforce a total quiz duration or per-question timeout. The Quiz Studio settings now disclose this rather than implying that those timers work.
- A process restart during a private attempt is not yet a seamless resume: the answer is persisted, but in-memory navigation state is lost. The student is prompted to start a new attempt.
- Existing historical `exam` database rows and legacy group-session enum values are retained for compatibility; new private attempts and new group quiz launches use Practice behaviour.

## Validation performed
- Python syntax compilation passed for `question_bank.py`, `group_quiz.py`, `bot.py`, `main.py`, and `leaderboard.py`.
- Static checks confirmed private native poll creation, poll-answer dispatch, subject selection for poll imports, option-count validation, and group quiz-poll delivery.
- Live Telegram API, database migration, and end-to-end group/private tests require deployment credentials and a running bot/database; they were not executable in this offline workspace.

## Deployment checklist
1. Back up the production PostgreSQL database.
2. Deploy the updated source. `question_bank.ensure_schema()` creates the new poll mapping table on startup.
3. Test admin poll import in a private chat: send a Telegram quiz poll, select a subject, inspect preview, save.
4. Test private student quiz: start a published quiz, answer each native poll, verify immediate Telegram feedback and automatic next question.
5. Test group quiz: start a short range, confirm each question is a native quiz poll, and verify poll answers/leaderboard.
6. Verify scoring, duplicate answer updates, and historical attempts before announcing availability.


## Priority update: Subject deletion
- Added an individual Delete action to each subject/collection in Admin → Quiz Studio → Subjects & Collections.
- The delete flow shows dependency counts for questions (including archived), quizzes, historical attempt snapshots, and optional test-series references.
- Permanent deletion is allowed only when the subject is empty and has no historical/test-series dependencies.
- Populated subjects are protected from destructive deletion; the UI offers Merge or Archive instead.
- The confirmation handler rechecks dependencies inside a database transaction immediately before deletion to reduce race-condition risk.
- Subject selector cache is refreshed after successful deletion.
- Existing bulk "Delete empty subjects" remains available.

## Multi-select subject/collection cleanup
- Added checkbox-style multi-selection to the Manage Subjects & Collections list; selection is retained while paging through the list.
- Added bulk archive for selected subjects/collections. It requires a preview and explicit confirmation; questions, quizzes and historical attempts are preserved.
- Added bulk permanent deletion for selected EMPTY subjects/collections only. The preview separates eligible empty items from blocked items with linked questions, quizzes, historical attempt snapshots, or test-series references.
- Bulk delete rechecks all dependencies inside the confirmation transaction to prevent deleting an item that became populated after preview.
- Selection is cleared after successful bulk archive/delete; a manual clear-selection action is also available.
- Validation: Python compilation and ZIP integrity checks passed. Live PostgreSQL/Telegram testing was not performed.
