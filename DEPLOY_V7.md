# V7 Deployment Notes

1. Back up the existing GitHub repository before replacing files.
2. Copy the contents of this package into the existing repository, preserving the existing project layout.
3. Keep existing Render environment variables, especially `BOT_TOKEN`, `DATABASE_URL`, `WEBHOOK_SECRET`, `ADMIN_TELEGRAM_IDS`, and `MINI_APP_URL`.
4. Do not reset the PostgreSQL database or drop existing tables. No destructive migration is included.
5. Deploy the bot service and Mini App service from the same repository as before.
6. After the bot restarts, its public command menu should show `/start`, `/mypremium`, `/tutorial`, and `/help`. `/admin` remains registered but is intended only for configured admins.
7. Student quiz attempts and group quiz polls are disabled in Telegram chat. Students should use the Mini App for quiz browsing and attempts.
8. Battle Arena remains a Coming Soon UI. The Arena backend/API implementation has not been modified.

## Local checks performed

- `python -m py_compile bot.py miniapp.py question_bank.py group_quiz.py`
- `node --check` against extracted inline JavaScript
- Duplicate HTML `id` scan
- ZIP integrity check

These are local checks only; the package has not been deployed to or verified against the live Render services.
