BPSC Nexus Group Quiz — Advisory Lock Fix

Files:
- bot.py
- group_quiz.py

Fix:
PostgreSQL pg_advisory_xact_lock(int, bigint) is not a valid overload. The two group-quiz lock sites now use the supported single-bigint overload with hashtextextended(), namespaced by group chat ID. This preserves per-group locking without casting Telegram's 64-bit chat IDs down to 32-bit integers.

Deployment:
1. Replace both files in the GitHub repository with these files.
2. Commit and push.
3. Redeploy the existing Render bot service.
4. Do not reset or delete the database.

Checks: Python syntax compilation passed for both files; ZIP integrity checked after packaging. Live PostgreSQL/Telegram integration was not run in this environment.
