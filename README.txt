BPSC Nexus — Group Quiz Startup Speed Fix

Replace the existing group_quiz.py with the included file. No database reset is required.
Commit/push to GitHub and redeploy the Telegram bot service on Render.

Performance changes:
- First question is published immediately after the launch confirmation; the scheduler remains as a recovery fallback.
- Removed the artificial five-second delay before a session becomes due.
- Batch-inserts question-range snapshots and session question snapshots to reduce serial database round-trips for large quizzes.
- Reduced scheduler polling interval to 0.75 seconds for faster progression/recovery.
- Preserves the one-active-quiz-per-group advisory-lock/reconciliation safeguards and completed-report recovery logic.

Validation: Python syntax compilation and ZIP integrity checks only. Live Telegram/PostgreSQL performance was not tested.
