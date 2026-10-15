BPSC NEXUS — BATTLE ARENA DEPLOYMENT PACKAGE

Contents:
- index.html: latest supplied/refined frontend including Admin Console visibility patch
- miniapp.py: backend from the available project files (included for reference/consistency)

Deployment notes:
1. Back up the current GitHub files before replacing anything.
2. This package is built from the latest HTML available in this session.
3. Keep bot.py unchanged; the Mini App service uses `python miniapp.py`.
4. Deploy to a staging/preview branch first if possible. Test admin login, each admin API, and 1v1 battle with two Telegram accounts before production.
5. This package does not independently verify Render environment variables, database schema, admin authorization, Telegram profile photo APIs, or end-to-end multiplayer behavior. If the Admin Console still appears blank after login, inspect browser console and Mini App backend logs; frontend HTML alone cannot fix backend/API authorization failures.
