# BPSC Nexus QuizBot Patch

This is a partial replacement package. It contains only files to replace or add in the root of the existing `bpsc-nexus-test-series-main` project.

## Replace existing files
- `bot.py`
- `group_quiz.py`
- `question_bank.py`
- `requirements.txt`
- `render.yaml`

## Add new files
- `.env.example`
- `run_local.sh`
- `LOCAL_SETUP.md`
- `CLEANUP_OLD_FILES.txt`

## What changed
- Native Telegram bot interface; no Mini App entry points.
- Removed active payment, pass, subscription, and membership-gate flows.
- No AI question generation.
- Only Telegram IDs listed in `ADMIN_TELEGRAM_IDS` can manage the question bank and create/import/edit/delete/publish tests.
- All students can attempt published quizzes in private chat.
- Personal quiz question and option order are randomized per attempt.
- Group quiz question and option order are randomized.
- Any group member can schedule an already-published test with `/schedulequiz`.
- The person who scheduled a future group quiz can cancel it with `/cancelschedule`; group admins can cancel any scheduled quiz.
- Immediate start/pause/resume/stop controls remain limited to Telegram group admins.
- Independent polling mode lets the bot run from Termux/Linux without Render.
- Existing Neon PostgreSQL database remains in use; no data reset or destructive migration.

## Apply
1. Stop the bot process before replacing files.
2. Back up the current project folder.
3. Extract these files into the existing project root and overwrite the five listed files.
4. Add the four new files.
5. Keep the existing question bank, `schema.sql`, `leaderboard.py`, and other required project modules.
6. Follow `LOCAL_SETUP.md` for Termux/Linux local polling.
7. If using Render, keep `BOT_MODE=webhook` and the webhook environment variables configured.

## Important limitation
“Offline” here means independent of Render. Telegram and the current Neon PostgreSQL database still require internet access. For local failover, stop the Render process before starting local polling; Telegram does not allow two simultaneous polling/webhook consumers for the same bot token.
