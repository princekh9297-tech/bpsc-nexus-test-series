# BPSC Nexus Premium Mini App redesign

## Included
- Branded splash: “Made with Pure 💗” / “BpscNexus × P r I n C e” with a short, skippable fade-in.
- Four themes: Midnight Elite (`graphite`), Emerald Royale (`emerald`), Cosmic Amethyst (`amethyst`), Ivory Prestige (`ivory`).
- Theme persistence per verified Telegram user through the authenticated Mini App API; localStorage is used as a fallback.
- A small additive PostgreSQL preference table is created idempotently on Mini App startup. Existing tables are not dropped or reset.
- Previous CBT routes and UI are retained; PDF-to-quiz is not included.

## Deploy
1. Replace the matching files in the existing repository, especially `static/index.html` and `miniapp.py`.
2. Keep the current Render environment variables, including `BOT_TOKEN` and `DATABASE_URL`.
3. Deploy the Mini App service first, then test `/healthz`, open the app from Telegram, switch themes, close/reopen the app and confirm the theme persists.
4. Test a practice attempt and an exam attempt before public release.

## Validation performed
- Python compilation for bot.py, miniapp.py, group_quiz.py, question_bank.py.
- JavaScript syntax check and HTML ID reference checks.
- ZIP integrity check.

Live Telegram/PostgreSQL integration and real-device performance testing still need to be performed in the deployed environment.
