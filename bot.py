"""BPSC Nexus Telegram navigation bot.

Quiz delivery, attempts, scoring, leaderboards and student analytics are handled
exclusively by the Telegram Mini App. This bot only exposes /start, /tutorial
and /miniapp as navigation/help commands.
"""

import logging
import os

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
    WebAppInfo,
)
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bpsc_nexus_bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
MINI_APP_URL = os.environ.get("MINI_APP_URL", "").strip()


async def miniapp_keyboard() -> InlineKeyboardMarkup | None:
    if not MINI_APP_URL.startswith("https://"):
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚀 Open BPSC Nexus Mini App", web_app=WebAppInfo(url=MINI_APP_URL))]]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    if chat.type != "private":
        await message.reply_text(
            "BPSC Nexus quizzes are available exclusively through the Mini App.\n"
            "Open a private chat with me and use /miniapp."
        )
        return

    await message.reply_text(
        "📚 *BPSC NEXUS*\n\n"
        "Your preparation workspace for practice, mock tests, progress and rankings.\n\n"
        "Use the button below to continue in the Mini App.\n\n"
        "Commands: /miniapp · /tutorial",
        parse_mode="Markdown",
        reply_markup=await miniapp_keyboard(),
        disable_web_page_preview=True,
    )


async def miniapp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    if chat.type != "private":
        await message.reply_text(
            "Please open a private chat with BPSC Nexus and use /miniapp to launch the workspace."
        )
        return

    keyboard = await miniapp_keyboard()
    if keyboard is None:
        await message.reply_text(
            "The Mini App URL is not configured yet. Please contact the administrator."
        )
        return

    await message.reply_text(
        "🚀 *BPSC Nexus Mini App*\nTap below to open your workspace.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def tutorial_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    await message.reply_text(
        "📘 *BPSC NEXUS — QUICK TUTORIAL*\n\n"
        "1. Open the Mini App using /miniapp.\n"
        "2. Sign in automatically through your Telegram account.\n"
        "3. Open the Question Bank and choose a subject or test.\n"
        "4. Select Practice Mode or Exam Mode where available.\n"
        "5. Submit your answers to view your result and performance.\n"
        "6. Visit your profile and leaderboard to review your progress.\n\n"
        "All quiz attempts and results are handled inside the Mini App. "
        "The Telegram bot is only for launching the app and providing help.",
        parse_mode="Markdown",
        reply_markup=await miniapp_keyboard() if update.effective_chat and update.effective_chat.type == "private" else None,
        disable_web_page_preview=True,
    )


async def post_init(application: Application) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    if not RENDER_EXTERNAL_URL.startswith("https://"):
        raise RuntimeError("RENDER_EXTERNAL_URL must be the HTTPS URL assigned by Render.")
    if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
        raise RuntimeError("Set WEBHOOK_SECRET to a random string of at least 16 characters.")
    if not MINI_APP_URL.startswith("https://"):
        logger.warning("MINI_APP_URL is missing or invalid; /miniapp will show a configuration message.")

    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    commands = [
        BotCommand("start", "Start BPSC Nexus"),
        BotCommand("tutorial", "How to use BPSC Nexus"),
        BotCommand("miniapp", "Open BPSC Nexus Mini App"),
    ]
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    logger.info("BPSC Nexus navigation bot initialized; quiz handlers are disabled.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in the hosting environment.")
    if not RENDER_EXTERNAL_URL.startswith("https://"):
        raise SystemExit("Set RENDER_EXTERNAL_URL to the HTTPS URL assigned by Render.")
    if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
        raise SystemExit("Set WEBHOOK_SECRET to a random string of at least 16 characters.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("tutorial", tutorial_command))
    application.add_handler(CommandHandler("miniapp", miniapp_command))
    application.add_error_handler(error_handler)

    logger.info("Starting BPSC Nexus navigation bot in webhook mode.")
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{RENDER_EXTERNAL_URL}/telegram",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    main()
