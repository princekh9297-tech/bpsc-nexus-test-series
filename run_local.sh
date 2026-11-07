#!/usr/bin/env bash
# Start BPSC Nexus independently of Render (Android Termux / Linux).
set -e
cd "$(dirname "$0")"

if [ ! -f ".env" ]; then
  echo "Missing .env. Copy .env.example to .env and add BOT_TOKEN, DATABASE_URL and ADMIN_TELEGRAM_IDS."
  exit 1
fi

set -a
. ./.env
set +a
export BOT_MODE=polling

python bot.py
