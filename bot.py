import asyncio
import logging
import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from telegram.ext import PollAnswerHandler
import question_bank as qb
import group_quiz as gq
import intel

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
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


async def ensure_schema() -> None:
    """Idempotent migration: only the user table is needed by the bot itself.

    Older tables from previous versions (premium_passes, pass_catalogue,
    bpsc_payment_requests, tests ...) are intentionally left untouched in the
    database; the bot simply no longer reads or writes them.
    """
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
    logger.info("Database migration verified: app_users table.")


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
    if not ADMIN_TELEGRAM_IDS:
        logger.warning("ADMIN_TELEGRAM_IDS is empty; all admin panel access will be denied.")

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

    # Serialize every startup migration across Render processes/instances.
    # This is a session-level lock: it remains held while the individual
    # migration functions use their own pooled connections and is released
    # automatically by PostgreSQL if this process/connection disappears.
    migration_lock_key = 0x425053434D4947  # Stable BPSC Nexus schema-migration lock.
    async with pool.connection() as migration_lock_conn:
        logger.info("Waiting for the shared database migration lock...")
        await migration_lock_conn.execute(
            "SELECT pg_advisory_lock(%s)", (migration_lock_key,)
        )
        await migration_lock_conn.commit()
        try:
            logger.info("Database migration lock acquired; applying startup migrations.")
            await ensure_schema()
            await qb.ensure_schema(pool)
            await gq.ensure_schema(pool)
            await intel.ensure_schema(pool)
            logger.info("All startup migrations completed successfully.")
        finally:
            # Commit the unlock query as well; the session-level lock survives
            # transaction boundaries until pg_advisory_unlock is called.
            await migration_lock_conn.execute(
                "SELECT pg_advisory_unlock(%s)", (migration_lock_key,)
            )
            await migration_lock_conn.commit()
            logger.info("Database migration lock released.")
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username
    application.bot_data["db_pool"] = pool
    # Start the persistent group-poll scheduler once the shared database is ready.
    if application.bot_data.get("group_quiz_scheduler_task") is None:
        application.bot_data["group_quiz_scheduler_task"] = asyncio.create_task(gq.scheduler(application, pool))
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Start BPSC Nexus"),
            BotCommand("quiz", "Take a quiz in this chat"),
            BotCommand("myanalysis", "Get your private group quiz analysis"),
            BotCommand("tutorial", "How to use BPSC Nexus"),
            BotCommand("help", "Show commands and help"),
        ], scope=BotCommandScopeAllPrivateChats())
        await application.bot.set_my_commands([
            BotCommand("start", "Show group quiz commands"),
            BotCommand("startquiz", "Create/start a group quiz (admins)"),
            BotCommand("pausequiz", "Pause the current group quiz (admins)"),
            BotCommand("resumequiz", "Resume a paused group quiz (admins)"),
            BotCommand("stopquiz", "Stop and finalize quiz with results (admins)"),
            BotCommand("schedulequiz", "Schedule a group quiz (admins)"),
            BotCommand("leaderboard", "Show group leaderboard"),
            BotCommand("quizstatus", "Show current group quiz status"),
            BotCommand("quizbrand", "Set this group’s quiz branding (admins)"),
            BotCommand("help", "Show group commands"),
        ], scope=BotCommandScopeAllGroupChats())
    except Exception:
        logger.exception("Could not register Telegram command menus")
    logger.info("Database connection verified; Question Bank and Group Quiz migrations applied automatically.")


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
            """
            INSERT INTO app_users (telegram_id, first_name, username)
            VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id) DO UPDATE
            SET first_name = EXCLUDED.first_name,
                username = EXCLUDED.username
            """,
            (user.id, user.first_name, user.username),
        )














async def reply_with_uluk(message, caption: str, reply_markup=None) -> None:
    """Send clean text until individually art-directed transparent sticker assets are approved."""
    await message.reply_text(caption, reply_markup=reply_markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text(
                "🧩 BPSC Nexus Group Quiz\n\n"
                "Group admins/owners: /startquiz to create a quiz, /pausequiz to pause, "
                "/resumequiz to continue, /stopquiz to complete early and publish results, "
                "/schedulequiz to schedule, /leaderboard for rankings, /quizstatus for status, and /quizbrand to customise this group’s branding.\n\n"
                "Use /help to see all group commands. Personal quizzes open in private chat."
            )
        return
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    await save_user(update)
    if context.args and context.args[0].startswith("gqa_"):
        try:
            session_id = int(context.args[0][4:])
        except ValueError:
            await message.reply_text("That personal analysis link is invalid. Use /myanalysis to get your latest report.")
            return
        await gq.send_personal_analysis(update, context, pool, session_id)
        return
    if context.args and context.args[0].startswith("quiz_"):
        try:
            quiz_id = int(context.args[0][5:])
        except ValueError:
            await message.reply_text("That quiz link is invalid. Use /quiz to see available quizzes.")
            return
        await message.reply_text("Choose how you want to attempt this quiz:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start quiz", callback_data=f"play:start:{quiz_id}")]]))
        return
    await reply_with_uluk(
        message,
        f"Welcome to BPSC Nexus, {user.first_name or 'Aspirant'}! 🦉\n\nPractise BPSC quizzes right here in Telegram. Tap a button below or use /quiz.",
        reply_markup=main_user_keyboard(),
    )




def main_user_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Start a quiz", callback_data="play:list")],
        [InlineKeyboardButton("📘 Tutorial", callback_data="user:tutorial")],
    ])


async def personal_analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await gq.personal_analysis_command(update, context, pool)


async def tutorial_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not is_private(update):
        await message.reply_text("Please open a private chat with BPSC Nexus to take personal quizzes.")
        return
    await reply_with_uluk(
        message,
        "📘 BPSC Nexus — Quick Tutorial\n\n"
        "1. Send /quiz to see the published quizzes.\n"
        "2. Tap a quiz and choose Practice or Exam mode.\n"
        "3. Answer each question with the option buttons, then Submit to see your score.\n"
        "4. Join a group quiz in your study group and use /myanalysis for your private report.",
        reply_markup=main_user_keyboard(),
    )

async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text("Please open me in a private chat to take quizzes.")
        return
    if pool is None or update.effective_message is None:
        return
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id,title,subject,(SELECT COUNT(*) FROM bank_quiz_questions bqq WHERE bqq.quiz_id=bq.id) AS n FROM bank_quizzes bq WHERE is_published=TRUE AND (is_custom=FALSE OR custom_owner_telegram_id=%s) ORDER BY id DESC LIMIT 20", (update.effective_user.id,))
        rows = await cur.fetchall()
    if not rows:
        await update.effective_message.reply_text("No quizzes are published yet. Please check again later.")
        return
    lines = ["📚 Available quizzes", ""]
    buttons = []
    for item in rows:
        lines.append(f"#{item['id']} · {item['title']}\n{item['subject']} · {item['n']} questions")
        buttons.append([InlineKeyboardButton(f"Start #{item['id']} · {item['title'][:35]}", callback_data=f"play:start:{item['id']}")])
    await update.effective_message.reply_text("\n\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def question_bank_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update) or not is_admin(update):
        if update.effective_message and update.effective_message.document:
            await update.effective_message.reply_text("Question imports are available only to authorized admins in a private chat.")
        return
    handled = await qb.receive_file(update, context, pool, True)
    if not handled and update.effective_message and update.effective_message.document:
        doc = update.effective_message.document
        ext = (doc.file_name or "").rsplit(".", 1)[-1].lower()
        if ext in {"html", "htm", "json", "csv"}:
            await update.effective_message.reply_text("Open Admin Panel → Question Bank → Import, choose a default subject, then send the file.")


async def question_bank_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update) or not is_admin(update):
        return
    await qb.receive_poll(update, context, True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        message = update.effective_message
        if message:
            await message.reply_text(
                "🧩 BPSC Nexus — Group Commands\n\n"
                "/startquiz — create a quiz and choose its fixed question range (admins/owner)\n"
                "/pausequiz — pause the active quiz (current poll closes; resume continues at next question)\n"
                "/resumequiz — resume a paused quiz\n"
                "/stopquiz — stop early, mark completed, and publish analysis + leaderboard\n"
                "/schedulequiz <quiz_id> <YYYY-MM-DDTHH:MMZ> [15|30|60|120|300] [learning|exam] — schedule in UTC\n"
                "/leaderboard — cumulative leaderboard for this group\n"
                "/quizstatus — active/paused/scheduled quiz status\n"
                "/quizbrand <name> — customise branding on this group’s quiz polls; /quizbrand reset restores the group title\n"
                "Legacy aliases still work: /groupquiz, /groupquiz_schedule, /grouprank, /groupstatus, /groupstop\n"
                "/start — show this group guide\n\n"
                "Make the bot an administrator with permission to send messages and create polls."
            )
        return
    await tutorial_command(update, context)




# ---------- Admin panel UI ----------
def admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗃 Question Bank & Quiz Studio", callback_data="qb:home")],
        [InlineKeyboardButton("📡 Nexus Intel", callback_data="intel:home")],
    ])


def admin_only(update: Update) -> bool:
    return is_private(update) and is_admin(update)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text("For security, the admin panel is available only in a private chat with the bot.")
        return
    if not is_admin(update):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ This panel is available only to authorized administrators.")
        return
    context.user_data.pop("admin_flow", None)
    await update.effective_message.reply_text("🛡️ BPSC Nexus Admin Panel\nChoose an action:", reply_markup=admin_home_keyboard())


async def show_admin_home(query) -> None:
    await query.edit_message_text("🛡️ BPSC Nexus Admin Panel\nChoose an action:", reply_markup=admin_home_keyboard())























async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    if data.startswith("intel:"):
        if not admin_only(update):
            await query.answer("Not authorized.", show_alert=True)
            return
        await intel.route_callback(update, context, pool, admin_only)
        return
    if await gq.route_callback(update, context, pool):
        return
    # Personal quiz buttons (play:*) and admin question-bank buttons (qb:*).
    handled = await qb.route_callback(update, context, pool, admin_only(update))
    if handled:
        return
    # Admin callback data is always re-authorized; hiding the buttons is not the security boundary.
    if data.startswith("adm:"):
        if not admin_only(update):
            await query.answer("Not authorized.", show_alert=True)
            return
        await query.answer()
        if data == "adm:home":
            context.user_data.pop("admin_flow", None)
            await show_admin_home(query)
        elif data == "adm:cancel":
            context.user_data.pop("admin_flow", None)
            await query.edit_message_text("Cancelled.", reply_markup=admin_home_keyboard())
        else:
            await query.edit_message_text("Unknown action. Return to the Admin Panel.", reply_markup=admin_home_keyboard())
        return

    if data == "user:tutorial":
        await query.answer()
        await tutorial_command(update, context)
    else:
        await query.answer("This action is no longer available.", show_alert=True)


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or not message.text or user is None:
        return
    # Group-quiz range/timer/scoring prompts are handled before the owner-only admin text flow.
    # The handler returns True only when this message belongs to an active group-quiz setup.
    if await gq.group_quiz_text_input(update, context):
        return
    # Ignore ordinary user text completely; admin text prompts work only in private chat.
    if not admin_only(update):
        return
    await qb.admin_text_callback(update, context, pool, True)


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
    application.add_handler(CommandHandler("quiz", quiz_command))
    application.add_handler(CommandHandler("myanalysis", personal_analysis_command))
    application.add_handler(CommandHandler("tutorial", tutorial_command))
    application.add_handler(CommandHandler("admin", admin_command))
    # New one-word group command names; old aliases remain registered for backwards compatibility.
    application.add_handler(CommandHandler("startquiz", gq.group_quiz_command))
    application.add_handler(CommandHandler("pausequiz", gq.group_pause_command))
    application.add_handler(CommandHandler("resumequiz", gq.group_resume_command))
    application.add_handler(CommandHandler("stopquiz", gq.group_stop_command))
    application.add_handler(CommandHandler("schedulequiz", gq.group_quiz_schedule_command))
    application.add_handler(CommandHandler("leaderboard", gq.group_leaderboard_command))
    application.add_handler(CommandHandler("quizstatus", gq.group_status_command))
    application.add_handler(CommandHandler("quizbrand", gq.group_brand_command))
    application.add_handler(CommandHandler("groupquiz", gq.group_quiz_command))
    application.add_handler(CommandHandler("groupquiz_schedule", gq.group_quiz_schedule_command))
    application.add_handler(CommandHandler("grouprank", gq.group_leaderboard_command))
    application.add_handler(CommandHandler("groupstatus", gq.group_status_command))
    application.add_handler(CommandHandler("groupstop", gq.group_stop_command))
    application.add_handler(CallbackQueryHandler(admin_callback))
    application.add_handler(PollAnswerHandler(gq.group_poll_answer))
    application.add_handler(MessageHandler(filters.Document.ALL, question_bank_file_handler))
    application.add_handler(MessageHandler(filters.POLL, question_bank_poll_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_input))
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
