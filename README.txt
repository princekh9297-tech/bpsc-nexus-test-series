BPSC Nexus Group Quiz — Executive Elite Update

Files:
- bot.py
- group_quiz.py

Included:
- Executive Elite Unicode typography for quiz launch, started confirmation, and final results.
- Fixed-width leaderboard with rank, student, score, accuracy, correct, wrong, and skipped columns.
- Launch announcement message ID persisted in PostgreSQL.
- The launch announcement is deleted only after the started confirmation has been edited/sent and the first Telegram poll is successfully published and persisted.
- If Telegram deletion fails because of permissions/message state, the quiz continues and a warning is logged.
- Serializes all startup schema migrations with one session-level PostgreSQL advisory lock, preventing concurrent Render processes from deadlocking on AccessExclusive table locks. The lock is released after all migrations finish and is automatically released if the DB connection drops.
- Preserves personal /myanalysis deep-link flow, per-user report isolation, group-level HTML report without individual data, completion/recovery logic, report retry flags, and the per-group quiz advisory-lock fix.

Deploy:
1. Replace both bot.py and group_quiz.py in the existing GitHub repository.
2. Commit and push; redeploy the existing Render bot service.
3. Do not reset the database. Existing schema migration adds launch-announcement tracking columns.
4. Ensure the bot has permission to delete its own group messages if you want the launch announcement to disappear.

Validation: Python syntax compilation, migration-lock static checks, and ZIP integrity checks passed. Live Telegram/PostgreSQL integration is not available in this build environment; test startup and a short quiz after redeploy.
