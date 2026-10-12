BPSC Nexus — Premium Group Results Cleanup

Files:
- bot.py
- group_quiz.py

Changes:
- Executive Elite final summary is shorter and easier to scan on Telegram.
- Leaderboard remains a fixed-width, monospaced table.
- Summary and group HTML report delivery use atomic PostgreSQL claims, preventing concurrent scheduler/finalization calls from sending duplicate messages.
- Stale delivery claims are eligible for retry after 15 minutes if a worker dies mid-delivery; failed sends release their claim for retry.
- Only one group HTML analysis document is sent per quiz under normal successful delivery. It includes aggregate group statistics, question-wise analysis, correct answers and explanations, and excludes individual student data.
- Removed the separate Exam Mode answer-key message because the HTML report already contains answers/explanations, avoiding redundant post-quiz messages.
- `report_sent` becomes true only when both the group summary and group HTML report have been delivered.
- Existing personal /myanalysis flow, quiz completion/recovery, advisory locks, and other quiz features are preserved.

Deploy:
1. Replace both bot.py and group_quiz.py in the existing GitHub repository.
2. Commit/push and redeploy the existing Render bot service.
3. Do not reset the database. Startup migration adds the two delivery-claim timestamps.
4. Test a short quiz in both Learning and Exam modes; expect one summary and one HTML report in the group, with no extra standalone answer-key message.

Validation performed: Python syntax compilation, ZIP integrity, and static checks for claim guards/report delivery. Live Telegram/PostgreSQL integration has not been run in this environment.
