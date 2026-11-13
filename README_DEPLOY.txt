BPSC Nexus — Subject Catalogue + Telegram Admin Access Restoration

Files in this package:
- static/index.html: restores automatic Telegram identity/access flow for admins; removes the separate admin ID/password prompt; keeps non-admin users restricted; retains the subject catalogue frontend refresh fix.
- miniapp.py: retains the subject catalogue backend normalization fix so imported subject names match registered subjects despite case/whitespace differences. No admin-authentication code was changed in this file.

Deployment:
1. Replace static/index.html and miniapp.py in the GitHub repository with these versions.
2. Commit and push; allow Render to deploy.
3. No database migration or new environment variables are required.

Authentication behaviour:
- The Mini App sends Telegram WebApp initData as before.
- The backend verifies Telegram identity and determines admin status using ADMIN_TELEGRAM_IDS.
- Admin Console content is shown only when /api/access/status returns is_admin=true.
- Regular Telegram users remain restricted. No separate Admin ID/password login is used in the Mini App.

Validation performed:
- All non-empty inline JavaScript blocks in static/index.html passed node --check.
- Backend Python compilation is not included in this frontend-only authentication correction; miniapp.py is carried forward unchanged from the subject-catalogue fix package.
- Live Telegram/Render verification still requires deployment.
