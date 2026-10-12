BPSC Nexus — Nexus Magazine Report Design (Design 6)

Files:
- bot.py
- group_quiz.py

Implemented:
- Nexus Magazine visual system for both Group and Personal HTML reports: white editorial pages, red accent, serif headlines, numbered section headings, clean tables, mobile-responsive layout, and print/save-to-PDF support.
- Group HTML report includes group-wide metrics, score statistics, the full participant leaderboard, per-question answer key and explanations, response/accuracy statistics, and the hardest-question table.
- Telegram completion message is now a concise summary; leaderboard and detailed analysis are in the single attached Group HTML report.
- Personal HTML report uses the matching Nexus Magazine style and includes only the authenticated Telegram user's scorecard, their selected answers, correct answers, explanations, and response-time data where available.
- Existing atomic report-delivery claims, retry handling, and private /myanalysis flow are retained from the supplied baseline.

Deploy:
1. Back up the current bot.py and group_quiz.py.
2. Replace both files in the existing GitHub repository with these versions.
3. Commit/push and redeploy the existing Render bot service.
4. Do NOT reset or delete the PostgreSQL database.
5. Run a short test quiz in Learning and Exam modes. Verify the group receives one concise completion summary and one HTML report, and use two Telegram accounts to confirm each private report contains only that account's own responses.

Validation performed in this environment:
- Python syntax compilation passed for bot.py and group_quiz.py.
- ZIP integrity check performed after packaging.

Not performed:
- No live Telegram, Render, or PostgreSQL integration test was possible here. Please verify report delivery and privacy using a test group before relying on production results.
