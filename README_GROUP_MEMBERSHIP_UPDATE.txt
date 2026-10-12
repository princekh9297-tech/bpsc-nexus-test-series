BPSC NEXUS — GROUP QUIZ MEMBERSHIP + PREVIOUS QUIZ VIEW FIXES

This package combines the group-quiz membership gate with the previously prepared in-quiz question layout refinement.

FILES INCLUDED
- bot.py: owner-only group membership commands and command registration
- group_quiz.py: group membership schema, authorization checks, group quiz commands and scheduler enforcement
- question_bank.py: previous question structure detection/backfill improvements
- static/index.html: previous in-quiz structured question renderer

IMPORTANT: Do not replace miniapp.py for this update. The question-layout refinement is designed to work with the existing Mini App API. Keep your current environment variables, especially BOT_TOKEN, DATABASE_URL, ADMIN_TELEGRAM_IDS, RENDER_EXTERNAL_URL, and WEBHOOK_SECRET. Do not reset or delete the database.

OWNER COMMANDS (send to the bot in PRIVATE CHAT)
1. /groupgrant <group_id> [optional group name]
   Example: /groupgrant -1001234567890 BPSC Batch A
   Get the numeric group ID from your user-info bot. The bot admin/owner must be listed in Render's ADMIN_TELEGRAM_IDS.
2. /grouprevoke <group_id>
   Deactivates that group's membership and cancels active/scheduled group quiz sessions for it.
3. /grouplist
   Lists groups with active quiz membership.

GROUP QUIZ COMMANDS (inside an authorized group)
- /groupquiz — group admins/creator can open the quiz builder
- /groupquiz_schedule <quiz_id> <YYYY-MM-DDTHH:MMZ> [15|30|60|120|300] [learning|exam] — schedule a published quiz; time is UTC
- /grouprank — cumulative group leaderboard
- /groupstatus — active/scheduled quiz status
- /groupstop — group admins/creator can cancel the quiz
- /help or /start — group command guide

ACCESS RULES
- No groups are authorized automatically after this migration. Run /groupgrant for each group that should have quiz access.
- Group quiz creation, scheduling, controls, status and leaderboard are blocked for groups without active membership.
- Telegram group admins/creator can operate quizzes only after the group has been granted membership.
- Only Telegram user IDs configured in ADMIN_TELEGRAM_IDS can grant/revoke/list memberships. Group admins cannot grant their own membership.
- Membership rows are stored in the existing PostgreSQL database using CREATE TABLE IF NOT EXISTS. No existing tables are dropped.

DEPLOY
1. Back up the current repository before replacing files.
2. Replace bot.py and group_quiz.py in the BOT service repository.
3. Replace question_bank.py and static/index.html in the Mini App service repository (or the shared repository, if both services use it). Preserve the static/ directory path.
4. Commit/push and allow the existing Render services to redeploy. Do not change service start commands or environment variables.
5. In private chat, use /groupgrant <group_id> for each authorized group.
6. In an authorized group, test /groupquiz. In a non-authorized group, the bot should deny quiz access.

VALIDATION
- Python compilation passed for bot.py, group_quiz.py, and question_bank.py.
- Inline JavaScript syntax validation passed for static/index.html.
- Live Telegram, Render, and PostgreSQL integration tests were not performed. Check Render logs and test with one authorized and one unauthorized test group before launch.
