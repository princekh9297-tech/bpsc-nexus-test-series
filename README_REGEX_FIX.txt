BPSC Nexus — PostgreSQL regex migration fix

Replace only question_bank.py at the same path in the repository with this file.

Cause:
The question-layout backfill migration used PostgreSQL regex repetition bounds {0,500}.
PostgreSQL's regex engine rejects repetition bounds above its supported limit, causing
psycopg.errors.InvalidRegularExpression during startup.

Fix:
Reduced those two regex bounds to {0,250}. Other application files do not need to be
replaced for this specific deployment crash.

Deploy:
1. Replace question_bank.py only.
2. Commit and push to GitHub.
3. Let Render redeploy.
4. Confirm startup completes, then test statement and match-list rendering.

No database reset is needed. This fix does not modify question content or user attempts.
A live Render/PostgreSQL deployment test has not been performed.
