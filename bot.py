import logging
import os

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bpsc_nexus_bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
pool: AsyncConnectionPool | None = None


async def post_init(application: Application) -> None:
    global pool

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is missing.")
    if not RENDER_EXTERNAL_URL.startswith("https://"):
        raise RuntimeError("RENDER_EXTERNAL_URL must be the HTTPS URL assigned by Render.")
    if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
        raise RuntimeError("Set WEBHOOK_SECRET to a random string of at least 16 characters.")

    pool = AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={"row_factory": dict_row, "connect_timeout": 10},
    )
    await pool.open()
    await pool.wait()
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
    logger.info("Database connection verified.")


async def post_shutdown(application: Application) -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    if pool is not None:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO app_users (telegram_id, first_name, username)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE
                SET first_name = EXCLUDED.first_name,
                    username = EXCLUDED.username
                """,
                (user.id, user.first_name, user.username),
            )

    await message.reply_text(
        "Welcome to BPSC Nexus | Prelims Test Series!\n\n"
        "Use /tests to browse published tests.\n"
        "Use /help to see available commands.\n\n"
        "PYQs, practice tests and mock tests will appear here as they are published."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "BPSC Nexus Test Series — commands\n\n"
            "/start — Register and open the bot\n"
            "/tests — Browse published tests\n"
            "/help — Show this help\n\n"
            "Interactive attempts, timers and detailed results are planned for the next stage."
        )


async def tests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if pool is None:
        await message.reply_text("The test catalogue is temporarily unavailable. Please try again shortly.")
        return

    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, title, description, test_type, duration_minutes, total_marks
            FROM tests
            WHERE is_published = TRUE
            ORDER BY id DESC
            LIMIT 30
            """
        )
        tests = await cursor.fetchall()

    if not tests:
        await message.reply_text("No tests have been published yet. Please check again later.")
        return

    lines = ["Published tests", ""]
    for test in tests:
        kind = str(test["test_type"]).replace("_", " ").title()
        lines.append(
            f'#{test["id"]} — {test["title"]}\n'
            f'Type: {kind} | Duration: {test["duration_minutes"]} min '
            f'| Marks: {test["total_marks"]}'
        )
        if test["description"]:
            lines.append(str(test["description"])[:250])
        lines.append("")
    lines.append("Interactive test attempts will be added in the next development stage.")
    await message.reply_text("\n".join(lines))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in the hosting environment.")
    if not DATABASE_URL:
        raise SystemExit("Set DATABASE_URL in the hosting environment.")
    if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
        raise SystemExit("Set WEBHOOK_SECRET to a random string of at least 16 characters.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("tests", tests_command))
    application.add_error_handler(error_handler)

    logger.info("Starting BPSC Nexus Telegram bot in webhook mode.")
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{RENDER_EXTERNAL_URL}/telegram",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
