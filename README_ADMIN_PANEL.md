# BPSC Nexus — Admin Panel Refinement (Admin-only patch)

This package contains only the two Python files needed for the Telegram admin-panel update:
- `bot.py`
- `question_bank.py`

No Mini App files, database migrations, schema resets, or Battle Arena code are included.

## New button-led admin tools
- Add One Question: select subject, send a formatted MCQ, preview it, then confirm save.
- Delete Questions: browse all, ready, pending, or archived questions with pagination.
- Per-question permanent delete with a confirmation screen; removes the question from quiz sets first.
- Bulk Delete by IDs: press the button, enter comma/space-separated question IDs, review the list, and confirm.
- Delete Archived Questions: bulk cleanup with confirmation.
- Existing search/edit/archive, import/undo, quiz management, pending verification, and quality audit remain available.

## Deletion behavior
Permanent deletion removes matching `bank_quiz_questions` links and answer-level `bank_quiz_attempt_answers` rows that would otherwise prevent deletion. Aggregate attempt totals remain, but answer-level history for deleted questions is removed. The confirmation screen explains that deletion cannot be undone.

## Apply safely
1. Back up the current GitHub repository first.
2. Replace only `bot.py` and `question_bank.py` with these files.
3. Keep existing Render environment variables and the same PostgreSQL database.
4. Do not reset the database or run a destructive migration.
5. Deploy and test the admin panel in a private chat as an authorized admin.

## Checks performed
- Python compilation passed for both files.
- Callback paths and button-driven flows were checked statically.
- Live Telegram/Render/database testing has not been performed in this environment.
