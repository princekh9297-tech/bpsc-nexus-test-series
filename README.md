# BPSC Nexus | Prelims Test Series Bot (Free Web Service starter)

Python Telegram bot using Telegram webhooks, Neon PostgreSQL, and Render Web Service.

## Current features

- `/start`: registers or updates a student's Telegram profile.
- `/tests`: lists up to 30 published tests.
- `/help`: lists available commands.
- Neon connection check at startup.
- Webhook mode for deployment as a Render Web Service.

Interactive exam attempts, timers, answer submission, marking, and analytics are not implemented yet.

## Database

Use your dedicated Neon project. If you already created the six tables in `schema.sql`, do not rerun the schema unnecessarily.

## GitHub and Render

1. Upload these updated files to your existing private GitHub repository, replacing the old versions.
2. In Render, create a **Web Service** from that repository and select the **Free** instance if offered.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Add these environment variables in Render:
   - `BOT_TOKEN`: token from @BotFather
   - `DATABASE_URL`: Neon PostgreSQL connection string; keep SSL enabled
   - `WEBHOOK_SECRET`: a random secret of at least 16 characters. Render's Blueprint may generate this automatically.
6. Render automatically supplies `RENDER_EXTERNAL_URL` and `PORT` to deployed web services.
7. Deploy, inspect logs, then test `/start` and `/tests` in Telegram.

If using the Render Blueprint, review the generated environment variables before deploying. Do not place secrets in source files or GitHub.

## Free-tier caveats

Render free web services can sleep after 15 minutes without incoming HTTP traffic and can restart. Telegram webhook delivery may be delayed during cold starts, so this is for a prototype rather than guaranteed always-on service. Check current Render limits and billing settings.

## Security

- Keep bot and database credentials out of source control.
- If a token is exposed, revoke it with BotFather.
- Back up questions and important records.
