BPSC Nexus focused update — replace/add only these files

REPLACE at repository root:
- bot.py
- miniapp.py
- question_bank.py

ADD/REPLACE under static/:
- static/index.html (the current Mini App frontend from the supplied root index.html)
- static/membership.html (membership catalogue page)
- static/phonepe_qr.jpg (same QR asset needed by membership page)

Important:
- Keep existing Render environment variables and DATABASE_URL. Do not reset PostgreSQL.
- Both bot.py and miniapp.py include additive/idempotent schema setup for the 12 individual products. Existing product records are updated for current display name/price without automatically reactivating retired products.
- The universal HTML parser never executes uploaded JavaScript. It parses JSON-compatible embedded data only.
- Import batches can be reviewed/deleted individually. Questions already referenced by quizzes are archived to preserve attempt history; unreferenced questions from that batch are deleted. Other batches are untouched.
- Revision API endpoints are owner-scoped by verified Telegram identity. Wrong/skipped answers are saved on submission; bookmarks can be added/removed.
- Please deploy to a staging service/database first and run the supplied acceptance checklist before production.
