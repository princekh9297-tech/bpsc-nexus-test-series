"""BPSC Nexus native Telegram quiz bot.

The bot has no Mini App, payment, membership-pass, or AI-generation dependency.
Administrators manage the question bank; users attempt published quizzes and
may schedule published quizzes in Telegram groups.
"""
from __future__ import annotations

import asyncio
import logging
import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

import group_quiz as gq
import question_bank as qb

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bpsc_nexus_bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
BOT_MODE = os.environ.get(
    "BOT_MODE", "webhook" if RENDER_EXTERNAL_URL else "polling"
).strip().lower()
ADMIN_TELEGRAM_IDS = {
    item.strip()
    for item in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",")
    if item.strip().isdigit()
}
pool: AsyncConnectionPool | None = None


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and str(user.id) in ADMIN_TELEGRAM_IDS


def is_private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


def admin_only(update: Update) -> bool:
    return is_private(update) and is_admin(update)


async def ensure_schema() -> None:
    """Ensure the small user table required by the native Telegram bot."""
    if pool is None:
        raise RuntimeError("Database pool is not available")
    statements = [
        """CREATE TABLE IF NOT EXISTS app_users (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            telegram_id BIGINT UNIQUE NOT NULL,
            first_name TEXT,
            username TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS first_name TEXT",
        "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS username TEXT",
    ]
    async with pool.connection() as conn:
        for statement in statements:
            await conn.execute(statement)
        await conn.execute("SELECT id,telegram_id,first_name,username FROM app_users LIMIT 0")
    logger.info("Core user schema verified; existing legacy billing tables were left untouched.")


async def post_init(application: Application) -> None:
    global pool
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing. Use your existing PostgreSQL/Neon database.")
    if BOT_MODE not in {"polling", "webhook"}:
        raise RuntimeError("BOT_MODE must be 'polling' or 'webhook'.")
    if BOT_MODE == "webhook":
        if not RENDER_EXTERNAL_URL.startswith("https://"):
            raise RuntimeError("Webhook mode requires RENDER_EXTERNAL_URL.")
        if len(WEBHOOK_SECRET) < 16:
            raise RuntimeError("Webhook mode requires WEBHOOK_SECRET (at least 16 characters).")
    if not ADMIN_TELEGRAM_IDS:
        logger.warning("ADMIN_TELEGRAM_IDS is empty; admin question-bank access is disabled.")

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

    # Serialize schema migrations so a restart cannot run concurrent DDL.
    migration_lock_key = 0x425053434D4947
    async with pool.connection() as migration_lock_conn:
        await migration_lock_conn.execute("SELECT pg_advisory_lock(%s)", (migration_lock_key,))
        await migration_lock_conn.commit()
        try:
            await ensure_schema()
            await qb.ensure_schema(pool)
            await gq.ensure_schema(pool)
        finally:
            await migration_lock_conn.execute("SELECT pg_advisory_unlock(%s)", (migration_lock_key,))
            await migration_lock_conn.commit()

    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username
    application.bot_data["db_pool"] = pool
    if application.bot_data.get("group_quiz_scheduler_task") is None:
        application.bot_data["group_quiz_scheduler_task"] = asyncio.create_task(
            gq.scheduler(application, pool)
        )

    await application.bot.set_my_commands(
        [
            BotCommand("start", "Open BPSC Nexus quizzes"),
            BotCommand("quiz", "Attempt a published personal quiz"),
            BotCommand("tests", "Browse published tests"),
            BotCommand("myanalysis", "Get your private group quiz analysis"),
            BotCommand("tutorial", "How to use BPSC Nexus"),
            BotCommand("help", "Show commands and help"),
            BotCommand("admin", "Admin question-bank console"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Show group quiz commands"),
            BotCommand("startquiz", "Start a group quiz (group admins)"),
            BotCommand("schedulequiz", "Schedule a published quiz (any member)"),
            BotCommand("cancelschedule", "Cancel your scheduled quiz"),
            BotCommand("pausequiz", "Pause a live quiz (group admins)"),
            BotCommand("resumequiz", "Resume a quiz (group admins)"),
            BotCommand("stopquiz", "Stop a quiz (group admins)"),
            BotCommand("leaderboard", "Show group rankings"),
            BotCommand("quizstatus", "Show group quiz status"),
            BotCommand("help", "Show group commands"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )
    logger.info("BPSC Nexus quiz engine initialized in %s mode.", BOT_MODE)


async def post_shutdown(application: Application) -> None:
    global pool
    task = application.bot_data.get("group_quiz_scheduler_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if pool is not None:
        await pool.close()
        pool = None


async def save_user(update: Update) -> None:
    user = update.effective_user
    if user is None or pool is None:
        return
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO app_users(telegram_id,first_name,username)
               VALUES(%s,%s,%s)
               ON CONFLICT(telegram_id) DO UPDATE
               SET first_name=EXCLUDED.first_name,username=EXCLUDED.username""",
            (user.id, user.first_name, user.username),
        )


def main_user_keyboard(update: Update | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📝 Personal Quizzes", callback_data="play:list")],
        [InlineKeyboardButton("📊 My Group Analysis", callback_data="user:analysis")],
        [InlineKeyboardButton("❔ Help", callback_data="user:help")],
    ]
    if update is not None and is_admin(update):
        rows.insert(0, [InlineKeyboardButton("🛡 Admin · Question Bank", callback_data="qb:home")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if user is None or message is None or chat is None:
        return
    await save_user(update)

    if chat.type != "private":
        await message.reply_text(
            "🧩 *BPSC NEXUS · GROUP QUIZZES*\n\n"
            "/startquiz — Start a published/bank quiz (group admins)\n"
            "/schedulequiz — Schedule a published quiz (any group member)\n"
            "/cancelschedule — Cancel your future scheduled quiz\n"
            "/pausequiz · /resumequiz · /stopquiz — Manage a live quiz (group admins)\n"
            "/leaderboard — Group rankings\n"
            "/quizstatus — Current quiz status\n"
            "/help — Full command guide",
            parse_mode="Markdown",
        )
        return

    if context.args and context.args[0].startswith("gqa_"):
        try:
            session_id = int(context.args[0][4:])
        except ValueError:
            await message.reply_text("Invalid analysis link. Use /myanalysis for your latest report.")
            return
        await gq.send_personal_analysis(update, context, pool, session_id)
        return

    if context.args and context.args[0].startswith("quiz_"):
        try:
            quiz_id = int(context.args[0][5:])
        except ValueError:
            await message.reply_text("Invalid quiz link. Use /quiz to browse published tests.")
            return
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id,title FROM bank_quizzes WHERE id=%s AND is_published=TRUE",
                (quiz_id,),
            )
            quiz = await cur.fetchone()
        if not quiz:
            await message.reply_text("This quiz is no longer available.")
            return
        await message.reply_text(
            f"📚 {quiz['title']}\n\nChoose your quiz mode:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🧠 Practice", callback_data=f"play:mode:practice:{quiz_id}")],
                    [InlineKeyboardButton("⏱ Exam", callback_data=f"play:mode:exam:{quiz_id}")],
                    [InlineKeyboardButton("All published quizzes", callback_data="play:list")],
                ]
            ),
        )
        return

    await message.reply_text(
        f"📚 *BPSC NEXUS QUIZ PLATFORM*\n\n"
        f"Welcome, {user.first_name or 'Aspirant'}!\n\n"
        "Attempt published tests privately or participate in group quizzes. "
        "Only authorized administrators can create, import, edit and publish tests.",
        parse_mode="Markdown",
        reply_markup=main_user_keyboard(update),
    )


async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text("Use /quiz in a private chat to attempt a personal quiz.")
        return
    if pool is None or update.effective_message is None:
        return
    await save_user(update)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT id,title,subject,
                      (SELECT COUNT(*) FROM bank_quiz_questions q WHERE q.quiz_id=b.id) AS n
               FROM bank_quizzes b
               WHERE is_published=TRUE
               ORDER BY published_at DESC NULLS LAST,id DESC
               LIMIT 30"""
        )
        rows = await cur.fetchall()
    if not rows:
        await update.effective_message.reply_text("No tests have been published yet.")
        return
    lines = ["📚 *Published personal quizzes*", ""]
    buttons = []
    for row in rows:
        lines.append(f"#{row['id']} · {row['title']}\n{row['subject']} · {row['n']} questions")
        buttons.append(
            [InlineKeyboardButton(f"Start #{row['id']} · {row['title'][:32]}", callback_data=f"play:start:{row['id']}")]
        )
    await update.effective_message.reply_text(
        "\n\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def tests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await quiz_command(update, context)


async def personal_analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await gq.personal_analysis_command(update, context, pool)


async def tutorial_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "📘 *BPSC Nexus Quick Guide*\n\n"
        "1. Use /quiz to browse published personal quizzes.\n"
        "2. Choose Practice for instant feedback or Exam for timed assessment.\n"
        "3. Use /schedulequiz in a group to schedule a published quiz. Any member can schedule.\n"
        "4. Use /leaderboard to view group rankings.\n"
        "5. Administrators use /admin to create, import, edit, delete and publish tests.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await message.reply_text(
            "🧩 *BPSC Nexus Group Commands*\n\n"
            "/startquiz — Start a quiz (group admins)\n"
            "/schedulequiz <quiz_id> <YYYY-MM-DDTHH:MMZ> [15|30|60|120|300] [learning|exam] — Schedule a quiz (any member)\n"
            "/cancelschedule [session_id] — Cancel your scheduled quiz\n"
            "/pausequiz · /resumequiz · /stopquiz — Group-admin controls\n"
            "/leaderboard — Group rankings\n"
            "/quizstatus — Current quiz status",
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            "📚 *BPSC Nexus Commands*\n\n"
            "/start — Main menu\n"
            "/quiz — Attempt a published quiz\n"
            "/tests — Browse published tests\n"
            "/myanalysis — Private group quiz analysis\n"
            "/tutorial — Usage guide\n"
            "/admin — Admin question-bank console\n"
            "/help — This message",
            parse_mode="Markdown",
        )


def admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗃 Question Bank & Quiz Studio", callback_data="qb:home")]]
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text("Open a private chat with the bot to use the admin console.")
        return
    if not is_admin(update):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ This console is restricted to authorized administrators.")
        return
    context.user_data.clear()
    await update.effective_message.reply_text(
        "🛡️ *BPSC NEXUS · ADMIN CONSOLE*\n\n"
        "Create, import, verify, edit, archive, delete and publish tests here.\n"
        "Only published tests are visible to students.",
        parse_mode="Markdown",
        reply_markup=admin_home_keyboard(),
    )


async def question_bank_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update) or not is_admin(update):
        if update.effective_message and update.effective_message.document:
            await update.effective_message.reply_text("Only an authorized admin can import question-bank files.")
        return
    await qb.receive_file(update, context, pool, True)


async def question_bank_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update) or not is_admin(update):
        return
    await qb.receive_poll(update, context, True)


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Group setup prompts (range, timer, scoring) are handled before admin private-chat text.
    if await gq.group_quiz_text_input(update, context):
        return
    if not is_private(update) or not is_admin(update):
        return
    await qb.admin_text_callback(update, context, pool, True)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""

    if data.startswith("user:"):
        await query.answer()
        if data == "user:help":
            await query.message.reply_text("Use /quiz to attempt published tests. Use /help for all commands.")
        elif data == "user:analysis":
            await personal_analysis_command(update, context)
        return

    if data.startswith("gq:"):
        await gq.route_callback(update, context, pool)
        return

    if data.startswith(("qb:", "play:")):
        handled = await qb.route_callback(update, context, pool, admin_only(update))
        if not handled:
            await query.answer("This quiz action is no longer available.", show_alert=True)
        return

    await query.answer("Unknown action.", show_alert=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in the environment or .env file.")
    if not DATABASE_URL:
        raise SystemExit("Set DATABASE_URL to your existing PostgreSQL/Neon database.")
    if BOT_MODE not in {"polling", "webhook"}:
        raise SystemExit("BOT_MODE must be either 'polling' or 'webhook'.")
    if BOT_MODE == "webhook":
        if not RENDER_EXTERNAL_URL.startswith("https://"):
            raise SystemExit("Webhook mode requires RENDER_EXTERNAL_URL.")
        if len(WEBHOOK_SECRET) < 16:
            raise SystemExit("Webhook mode requires WEBHOOK_SECRET (at least 16 characters).")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("quiz", quiz_command))
    application.add_handler(CommandHandler("tests", tests_command))
    application.add_handler(CommandHandler("myanalysis", personal_analysis_command))
    application.add_handler(CommandHandler("tutorial", tutorial_command))
    application.add_handler(CommandHandler("admin", admin_command))

    application.add_handler(CommandHandler("startquiz", gq.group_quiz_command))
    application.add_handler(CommandHandler("pausequiz", gq.group_pause_command))
    application.add_handler(CommandHandler("resumequiz", gq.group_resume_command))
    application.add_handler(CommandHandler("stopquiz", gq.group_stop_command))
    application.add_handler(CommandHandler("schedulequiz", gq.group_quiz_schedule_command))
    application.add_handler(CommandHandler("cancelschedule", gq.group_quiz_cancel_schedule_command))
    application.add_handler(CommandHandler("leaderboard", gq.group_leaderboard_command))
    application.add_handler(CommandHandler("quizstatus", gq.group_status_command))
    application.add_handler(CommandHandler("quizbrand", gq.group_brand_command))
    application.add_handler(CommandHandler("groupquiz", gq.group_quiz_command))
    application.add_handler(CommandHandler("groupquiz_schedule", gq.group_quiz_schedule_command))
    application.add_handler(CommandHandler("grouprank", gq.group_leaderboard_command))
    application.add_handler(CommandHandler("groupstatus", gq.group_status_command))
    application.add_handler(CommandHandler("groupstop", gq.group_stop_command))

    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(PollAnswerHandler(gq.group_poll_answer))
    application.add_handler(MessageHandler(filters.Document.ALL, question_bank_file_handler))
    application.add_handler(MessageHandler(filters.POLL, question_bank_poll_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_input))
    application.add_error_handler(error_handler)

    if BOT_MODE == "webhook":
        logger.info("Starting BPSC Nexus in Render webhook mode.")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=f"{RENDER_EXTERNAL_URL}/telegram",
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=False,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Starting BPSC Nexus in independent polling mode (Render not required).")
        application.run_polling(
            drop_pending_updates=False,
            allowed_updates=Update.ALL_TYPES,
            close_loop=True,
        )


if __name__ == "__main__":
    main()
