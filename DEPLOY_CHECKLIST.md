# BPSC Nexus update — deployment checklist

## Files and layout

Keep `bot.py`, `group_quiz.py`, `question_bank.py`, `miniapp.py`, `requirements.txt`, the Render YAML files, and migration/template files in the repository root. Keep the Mini App frontend at `static/index.html`. The updated `miniapp.py` also falls back to root `index.html` if `static/index.html` is absent.

## Bot service

1. Commit the updated project files and deploy the existing `bpsc-nexus-test-series` service.
2. Preserve existing Render environment values: `BOT_TOKEN`, `DATABASE_URL`, `WEBHOOK_SECRET`, `ADMIN_TELEGRAM_IDS`, and `MINI_APP_URL`.
3. On startup, the bot registers its slash-command menus for private chats and groups. Private menu: `/start`, `/tests`, `/quiz`, `/mypremium`, `/groupquiz`, `/admin`, `/help`. Group menu: `/groupquiz`, `/help`, `/start`.
4. Check Render logs for `Telegram command menus configured for private chats and groups.` and for any startup/database migration errors.

## Mini App service

1. Deploy the separate Mini App service with start command `python miniapp.py`.
2. Copy the same `BOT_TOKEN` and `DATABASE_URL` values as the bot service.
3. Verify `https://YOUR-MINIAPP-HOST/healthz` returns JSON with `"status":"ok"`.
4. Open the Mini App from the Telegram bot, not by visiting the root URL in an ordinary browser; quiz APIs require Telegram launch authentication.

## Group quiz smoke test

1. Publish a quiz containing at least one ready/verified question.
2. Add the bot to a test group and grant it permission to send polls. Use a Telegram group administrator account to run `/groupquiz`.
3. If multiple bots are present, try `/groupquiz@YOUR_BOT_USERNAME` explicitly.
4. The bot should show a quiz picker. If no response arrives at all, inspect Render logs and verify the service is running and its webhook is healthy; also check BotFather group privacy settings and the exact bot username.
5. Choose a quiz and short interval, then verify poll posting, poll closure, next-question delivery, and final results.

## Safety

Do not commit bot tokens, database URLs, or webhook secrets. The source package intentionally contains no credentials. Run the SQL migration only against the intended BPSC Nexus database if startup migration cannot run; do not run it against an unrelated database.
