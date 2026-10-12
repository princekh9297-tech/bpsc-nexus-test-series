# BPSC Nexus Mini App — Premium Redesign + Planner + Arena MVP

This package preserves the existing bot, group quiz engine, question bank module, quiz APIs and shared PostgreSQL setup. The `bot.py`, `group_quiz.py`, and `question_bank.py` files are carried forward unchanged from the latest supplied project; the main changes are in `miniapp.py` and `static/index.html`.

## Included

- Branded splash screen with the requested “Made with Pure 💗” and “BpscNexus × P r I n C e” identity. It stays for about 3.5 seconds on a fresh webview session, can be skipped, and does not replay during the same session.
- Four themes: Midnight Elite, Emerald Royale, Cosmic Amethyst, and Ivory Prestige. Theme preferences sync to the authenticated Telegram user in PostgreSQL, with local fallback.
- Personal Study Planner: custom tasks, optional due dates, completion checkboxes, overdue indicators, filters (all/today/upcoming/completed), delete action, and links from quiz cards via “Add to My Tasks”. Submitting a linked quiz automatically completes its linked task.
- Battle Arena MVP: 1v1 invite rooms, 6-character invite codes, matchmaking queue, server-selected question set, one-time answer submission, server-side scoring, result view and basic rating/win/loss/draw counters.
- Existing Test Series, quiz builder, practice/exam flows, result analysis, and theme/profile panels remain in place.
- Additive database tables are created with `CREATE TABLE IF NOT EXISTS`; no existing tables are dropped or reset.

## Deployment

1. Back up the current deployment repository and database as a precaution.
2. Replace the Mini App service's `miniapp.py` with this package's `miniapp.py`.
3. Replace `static/index.html` with this package's `static/index.html` (the `static/` directory must sit beside `miniapp.py`).
4. Keep `BOT_TOKEN`, `DATABASE_URL`, the current Render start command, and the shared database configuration unchanged.
5. Deploy the Mini App service. On startup, the service applies only additive `CREATE TABLE IF NOT EXISTS` statements.
6. Open the Mini App from the Telegram bot and test theme persistence, adding/completing/deleting planner tasks, adding a quiz to tasks, submitting that quiz, creating an invite duel, joining with a second Telegram account, and completing a match.

## Arena MVP limitations — read before announcing it as a finished competitive system

- Matchmaking is queue-based and the initial MVP refreshes match state when the user reopens/refreshes a room; it is not yet a WebSocket-based live arena with synchronized countdowns or live opponent progress.
- The first release implements 1v1 only. Squad battles, tournaments, seasonal ranks, anti-abuse monitoring, sophisticated Elo-style ratings, and push notifications are not included in this package.
- The backend validates participation and records each answer once, and it calculates scores server-side. Before high-stakes competitive launches, add automated integration tests, rate limits, room expiry/abandonment rules, server-authoritative per-question deadlines, reconnect testing, and independent security review.
- No live Telegram, PostgreSQL, or Render integration test was possible in this packaging pass. Python/JavaScript syntax and package integrity checks should be completed before deployment; still perform the listed live checks in staging.

## Data safety

All new planner and arena records are keyed to authenticated Telegram user IDs. The existing quiz bank, attempts, bot data, and shared database tables are not dropped. Take a database backup before any deployment, as you should for every production release.


## V7 refinement and user-access policy

V7 is a refinement release, not a feature expansion. It improves spacing, typography hierarchy, question/option readability, focus states, touch targets, planner controls, bottom navigation, and reduced-motion handling. The five themes and current Mini App workflows are retained.

For ordinary students, the Telegram bot now exposes only `/start`, `/mypremium`, `/tutorial`, and `/help`. Quiz browsing and attempts, including group quiz polls, are routed exclusively through the Mini App. `/admin` and admin-only question-bank import/management remain available to configured administrators as an operational exception because those maintenance screens are not yet in the Mini App. Battle Arena remains a static Coming Soon screen; Arena backend code and API routes were not modified.

No database reset or destructive migration is included. Before deployment, back up the repository, replace the packaged application files, keep existing Render environment variables, and confirm the bot service has the correct `MINI_APP_URL`. This package has passed local syntax checks but has not been verified against the live Render service.
