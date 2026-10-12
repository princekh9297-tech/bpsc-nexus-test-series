BPSC Nexus — Group Quiz Recovery Fix

Replace the existing group_quiz.py with this file.
Do not reset or delete the PostgreSQL database.
Commit and push the replacement to GitHub, then redeploy the Telegram bot service on Render.

What this patch does:
- Uses a per-group PostgreSQL advisory transaction lock so two admins cannot create concurrent quiz sessions for the same group.
- Reconciles legacy active/paused rows before starting or scheduling a quiz.
- Finalizes exhausted sessions and advances past already-closed questions.
- Cancels orphaned active sessions that have no current question, and prevents duplicate legacy active rows from blocking a fresh quiz.
- Automatically retires due active/paused sessions older than 24 hours, while preserving future-dated scheduled sessions.
- Keeps completed-session report retry behavior intact.

Validation performed: Python syntax compilation and static checks only. No live Telegram or PostgreSQL integration test was run.
