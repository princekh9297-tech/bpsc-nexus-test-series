# BPSC Nexus — Independent Local Runtime

This build can run in Telegram polling mode without Render. Use it when Render's monthly usage allowance is exhausted.

## Important operating requirements

- Telegram still requires an internet connection.
- The bot process must be running on an always-on device/server.
- The current quiz engine uses PostgreSQL. Set `DATABASE_URL` to the existing Neon PostgreSQL database to retain the current question bank, tests, attempts and group history.
- This is Render-independent runtime, not an internet-free/offline Telegram service.
- Only one process may poll the bot token at a time. Before starting local polling, stop/pause the Render bot process or ensure its webhook/polling process is no longer active. Do not run two active instances with the same token.

## Android tablet / Termux

1. Install Termux from a trusted source (prefer F-Droid).
2. Open Termux and run:

   ```bash
   pkg update && pkg upgrade -y
   pkg install python git clang rust libffi openssl postgresql pkg-config -y
   ```

3. Copy the BPSC Nexus project folder to Termux storage, or clone your private repository.
4. Enter the project directory.
5. Install dependencies:

   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt

   If the psycopg binary wheel is unavailable on your Android/Termux build, install the Termux `postgresql` package (above), then install the C implementation with `pip install \"psycopg[c,pool]>=3.2,<4.0\"`.
   ```

6. Copy `.env.example` to `.env` and fill in:
   - `BOT_TOKEN`: Telegram bot token from BotFather
   - `DATABASE_URL`: your existing Neon PostgreSQL connection string (keep it inside quotes in `.env`, especially if it contains `&`)
   - `ADMIN_TELEGRAM_IDS`: comma-separated Telegram numeric IDs of authorized bot admins
   - `BOT_MODE=polling`

7. Start the bot:

   ```bash
   chmod +x run_local.sh
   ./run_local.sh
   ```

8. Keep Termux open and prevent Android battery optimization from stopping it. For a long-running process, use Termux:WakeLock and keep the tablet charging.

## Linux / VPS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env
chmod +x run_local.sh
./run_local.sh
```

For a 24/7 service, use a Linux `systemd` service or a process supervisor and an always-on host.

## Returning to Render

The project still supports `BOT_MODE=webhook` for Render. Render must have `RENDER_EXTERNAL_URL`, `WEBHOOK_SECRET`, `BOT_TOKEN`, `DATABASE_URL` and `ADMIN_TELEGRAM_IDS` configured. Set `BOT_MODE=webhook` in Render if you want to force webhook mode.

## Quiz platform permissions

- Only Telegram IDs in `ADMIN_TELEGRAM_IDS` can create, import, edit, delete and publish tests in the admin question-bank console.
- Students can attempt published personal quizzes.
- Group users can schedule already-published quizzes with `/schedulequiz`.
- The user who scheduled a future group quiz can cancel it with `/cancelschedule`; a Telegram group admin can cancel any scheduled quiz in that group.
- Starting, pausing, resuming and stopping an immediate live group quiz remains restricted to Telegram group admins.
- Membership/payment and Mini App features are no longer part of the active bot interface. Existing legacy database tables are not dropped.

## Data safety

This patch does not delete or reset your Neon database. The bot continues using the existing PostgreSQL question bank and quiz tables.
