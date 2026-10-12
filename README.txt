BPSC Nexus Mini App — Quiz Builder 500 Fix

Root cause from Render traceback:
AttributeError: 'AsyncConnection' object has no attribute 'executemany'

Fix:
In create_custom_quiz(), call executemany() on an async cursor (conn.cursor()), not directly on the psycopg 3 AsyncConnection.

Deployment:
1. Replace only the existing miniapp.py in the Mini App service repository with this miniapp.py.
2. Keep all other files and environment variables unchanged.
3. Commit and push; let Render redeploy.
4. Open the Mini App and try creating a custom quiz again.

No database reset or schema change is required for this specific error.
Validation performed: Python syntax compilation passed. No live Render/database test was performed.
