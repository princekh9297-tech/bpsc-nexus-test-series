BPSC NEXUS — 1v1 BATTLE ARENA UPDATE

Files to replace in the same GitHub repository:
- index.html
- miniapp.py

Keep bot.py and all existing modules/assets unchanged.

Render services:
- Mini App service: python miniapp.py
- Bot service: python bot.py (no change required)

What this update adds:
- Battle Arena is no longer marked Coming Soon.
- Subject selection from available verified question-bank subjects.
- Configurable 5/10/15/20/30/50 question matches.
- Configurable per-question timer or no timer.
- Quick Match, create private invite room, join by invite code.
- Recent matches and rating/win/loss stats.
- Existing arena tables are preserved; two additive columns are added automatically at startup.

Deployment:
1. Back up the current GitHub files first.
2. Replace only index.html and miniapp.py with these versions.
3. Commit/push to the same branch used by Render.
4. Allow the Mini App service to redeploy. Bot service does not need a code change.
5. Open the Mini App inside Telegram, open Battle Arena, test with two separate Telegram accounts.

Important current limitations:
- Quick Match pairs a waiting room when a second player joins; it is not a continuously polling queue with automatic waiting-room updates. Use Refresh/Recent Battles to re-open status.
- Timer countdown runs in the client; this update does not yet implement a strict server-side per-question deadline. Do not describe it as fully anti-cheat hardened until server-side timing and reconnect/resume tests are completed.
- Question selection requires `question_bank.status='ready'`, matching the existing Arena foundation.
