BPSC Nexus — Auto Question Layout Engine Update

Files to replace in your existing repository:
1. miniapp.py
2. question_bank.py
3. static/index.html

What this update does:
- Detects and stores question layout metadata during supported question-bank imports.
- On Mini App startup, adds the new metadata columns with additive migrations and backfills existing question-bank records without requiring re-upload.
- Synchronizes layout metadata into existing attempt snapshots, preserving question text, options, correct answers, explanations, question IDs, attempts and scores.
- Applies responsive visual layouts in the Mini App for recognizable match-the-following, statement-based, assertion-reason, pair-based, chronology and passage questions.
- Includes a runtime fallback detector for older or ambiguous records.
- Keeps the existing quiz engine, answer submission/scoring, Practice/Exam modes, leaderboard, test-series hub, premium access and Battle Arena code paths in place.

Deployment:
1. Back up the current three files (do not back up/replace/reset the database as a deployment step).
2. Replace only the three files above at the exact paths.
3. Commit and push to GitHub; let the Mini App Render service redeploy.
4. Check Render logs for successful startup/schema verification.
5. Test a standard MCQ, match-the-following question, statement question and assertion-reason question in the Mini App.

No question re-upload is required. The additive schema/backfill runs through question_bank.ensure_schema() at startup. No destructive SQL or database reset is included.

Notes:
- Automatic detection is heuristic and cannot perfectly infer structure from every badly flattened or ambiguous source. Unrecognized questions retain the standard MCQ layout; source-provided structured fields are preferred.
- The current package does not add a separate admin override screen for changing a detected type.
- Syntax and classifier smoke tests passed in the build workspace. No live Render/PostgreSQL integration test was possible here; verify after deployment.
