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


New in this update — Group-specific branding + live quiz progress
- Every poll question is prefixed with the configured group brand, question number (Qx/N), and timer duration.
- One persistent live-progress message is created and edited throughout the quiz; it shows progress percentage, unique participants, responses on the current question, and seconds per question. It is not reposted for every question.
- Progress edits are coalesced to reduce Telegram flood-limit risk. Poll answer storage and scoring remain unchanged.
- Each authorized group can configure its own label with /quizbrand Your Academy Name. Only that group’s Telegram admins can change it.
- Use /quizbrand with no arguments to view the current label; /quizbrand reset restores the Telegram group title. Branding is snapshotted at quiz launch so a running quiz remains consistent.
- Existing groups get the new columns through additive CREATE/ALTER IF NOT EXISTS migrations; no database reset is required.

Validation: Python syntax compilation and ZIP integrity checked. Live Telegram/Render/PostgreSQL integration has not been run in this environment. Test /quizbrand, progress edits, both Learning and Exam modes, and the final report flow in a test group before production use.


New in this update — branded HTML report identity
- Group and Personal HTML report covers use the group's explicitly saved /quizbrand name as the primary identity.
- If no custom brand is saved, the HTML report identity falls back to “BPSC Nexus” (not the Telegram group title).
- Both reports retain “Powered by BPSC Nexus” in the footer, with the group brand shown separately.
- Group report caption and downloaded HTML filenames also use the configured brand where available.
- Branding is snapshotted per quiz session; new quizzes created after this update correctly store NULL when no custom brand has been configured, so report fallback works as specified. Poll/progress display can still use the Telegram group title when custom branding is not configured.
- No new database migration or reset is required; the existing brand_name column is reused.

Validation for this update: Python syntax compilation passed. Live Telegram/Render/PostgreSQL integration has not been run.
