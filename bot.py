import logging
import os
from datetime import datetime, timezone

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
ADMIN_TELEGRAM_IDS = {
    item.strip()
    for item in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",")
    if item.strip()
}
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
    if not ADMIN_TELEGRAM_IDS:
        logger.warning("ADMIN_TELEGRAM_IDS is empty; all admin commands will be denied.")

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


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and str(user.id) in ADMIN_TELEGRAM_IDS


async def require_admin(update: Update) -> bool:
    if is_admin(update):
        return True
    message = update.effective_message
    if message:
        await message.reply_text("⛔ This command is restricted to authorized administrators.")
    return False


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


async def resolve_user_id(identifier: str) -> int | None:
    """Resolve a numeric Telegram ID, or a username previously seen by this bot."""
    if pool is None:
        return None
    clean = identifier.strip()
    if clean.startswith("@"):
        clean = clean[1:]
    if clean.isdigit():
        telegram_id = int(clean)
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO app_users (telegram_id)
                VALUES (%s)
                ON CONFLICT (telegram_id) DO NOTHING
                """,
                (telegram_id,),
            )
        return telegram_id

    if not clean:
        return None
    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT telegram_id
            FROM app_users
            WHERE LOWER(username) = LOWER(%s)
            LIMIT 1
            """,
            (clean,),
        )
        row = await cursor.fetchone()
    return int(row["telegram_id"]) if row else None


async def get_user_pk(telegram_id: int) -> int | None:
    if pool is None:
        return None
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM app_users WHERE telegram_id = %s",
            (telegram_id,),
        )
        row = await cursor.fetchone()
    return int(row["id"]) if row else None


async def has_active_premium(telegram_id: int) -> bool:
    if pool is None:
        return False
    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM premium_passes pp
                JOIN app_users u ON u.id = pp.user_id
                WHERE u.telegram_id = %s
                  AND pp.revoked_at IS NULL
                  AND (pp.expires_at IS NULL OR pp.expires_at > NOW())
            ) AS active
            """,
            (telegram_id,),
        )
        row = await cursor.fetchone()
    return bool(row and row["active"])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    await save_user(update)
    premium = await has_active_premium(user.id)
    status = "✅ Premium Pass active" if premium else "Standard access"
    await message.reply_text(
        "Welcome to BPSC Nexus | Prelims Test Series!\n\n"
        f"Membership: {status}\n\n"
        "Use /tests to browse available tests.\n"
        "Use /mypremium to check your Premium Pass.\n"
        "Use /help to see available commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "BPSC Nexus Test Series — commands\n\n"
        "/start — Register and open the bot\n"
        "/tests — Browse published tests\n"
        "/mypremium — Check your Premium Pass\n"
        "/help — Show this help\n\n"
        "Admin commands:\n"
        "/grantpremium <Telegram ID or known @username> <permanent|days>\n"
        "/revokepremium <Telegram ID or known @username>\n"
        "/premiumstatus <Telegram ID or known @username>\n\n"
        "A username can only be resolved if the account has previously interacted with this bot."
    )


async def mypremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if await has_active_premium(user.id):
        if pool is None:
            await message.reply_text("Unable to check membership right now.")
            return
        async with pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT pp.expires_at
                FROM premium_passes pp
                JOIN app_users u ON u.id = pp.user_id
                WHERE u.telegram_id = %s
                  AND pp.revoked_at IS NULL
                  AND (pp.expires_at IS NULL OR pp.expires_at > NOW())
                ORDER BY pp.granted_at DESC
                LIMIT 1
                """,
                (user.id,),
            )
            row = await cursor.fetchone()
        if row and row["expires_at"] is None:
            await message.reply_text("💎 Your Premium Pass is active (permanent until revoked).")
        elif row:
            expiry = row["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            await message.reply_text(f"💎 Your Premium Pass is active until {expiry}.")
        else:
            await message.reply_text("No active Premium Pass found.")
    else:
        await message.reply_text("You currently have standard access. No active Premium Pass was found.")


async def tests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if pool is None:
        await message.reply_text("The test catalogue is temporarily unavailable. Please try again shortly.")
        return

    premium = await has_active_premium(user.id)
    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, title, description, test_type, duration_minutes, total_marks, is_premium
            FROM tests
            WHERE is_published = TRUE
              AND (is_premium = FALSE OR %s = TRUE)
            ORDER BY id DESC
            LIMIT 30
            """,
            (premium,),
        )
        tests = await cursor.fetchall()
        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS locked_count
            FROM tests
            WHERE is_published = TRUE AND is_premium = TRUE
            """
        )
        locked_row = await cursor.fetchone()

    if not tests:
        text = "No tests are published for your access level yet."
    else:
        lines = ["Available published tests", ""]
        for test in tests:
            kind = str(test["test_type"]).replace("_", " ").title()
            marker = "💎 " if test["is_premium"] else ""
            lines.append(
                f'{marker}#{test["id"]} — {test["title"]}\n'
                f'Type: {kind} | Duration: {test["duration_minutes"]} min '
                f'| Marks: {test["total_marks"]}'
            )
            if test["description"]:
                lines.append(str(test["description"])[:250])
            lines.append("")
        text = "\n".join(lines)

    locked_count = int(locked_row["locked_count"]) if locked_row else 0
    if not premium and locked_count:
        text += (
            f"\n💎 {locked_count} premium test(s) require a Premium Pass. "
            "Contact the administrator for access."
        )
    text += "\nInteractive test attempts are a separate development stage."
    await message.reply_text(text)


async def grantpremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    message = update.effective_message
    actor = update.effective_user
    if message is None or actor is None or pool is None:
        return
    if len(context.args) != 2:
        await message.reply_text(
            "Usage: /grantpremium <Telegram ID or known @username> <permanent|days>\n"
            "Examples:\n/grantpremium 123456789 permanent\n/grantpremium 123456789 30"
        )
        return

    identifier, duration = context.args
    if duration.lower() == "permanent":
        expires_at = None
        duration_label = "permanent until revoked"
    else:
        try:
            days = int(duration)
        except ValueError:
            days = 0
        if days < 1 or days > 3650:
            await message.reply_text("Duration must be 'permanent' or a number of days from 1 to 3650.")
            return
        expires_at = None
        duration_label = f"{days} days"

    target = await resolve_user_id(identifier)
    if target is None:
        await message.reply_text(
            "I couldn't resolve that username. Use a numeric Telegram user ID, or ask the user to start this bot first."
        )
        return
    target_pk = await get_user_pk(target)
    if target_pk is None:
        await message.reply_text("Could not create or find the user's database record.")
        return

    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE premium_passes
                SET revoked_at = NOW(), revoked_by_telegram_id = %s
                WHERE user_id = %s AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > NOW())
                """,
                (actor.id, target_pk),
            )
            if duration.lower() == "permanent":
                await conn.execute(
                    """
                    INSERT INTO premium_passes (user_id, granted_by_telegram_id, expires_at)
                    VALUES (%s, %s, NULL)
                    """,
                    (target_pk, actor.id),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO premium_passes (user_id, granted_by_telegram_id, expires_at)
                    VALUES (%s, %s, NOW() + (%s * INTERVAL '1 day'))
                    """,
                    (target_pk, actor.id, days),
                )
    await message.reply_text(
        f"✅ Premium Pass granted.\nTelegram ID: {target}\nDuration: {duration_label}"
    )


async def revokepremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    message = update.effective_message
    actor = update.effective_user
    if message is None or actor is None or pool is None:
        return
    if len(context.args) != 1:
        await message.reply_text("Usage: /revokepremium <Telegram ID or known @username>")
        return
    target = await resolve_user_id(context.args[0])
    if target is None:
        await message.reply_text("User not found. Use a numeric Telegram user ID or a known username.")
        return
    target_pk = await get_user_pk(target)
    if target_pk is None:
        await message.reply_text("User not found in the database.")
        return
    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            UPDATE premium_passes
            SET revoked_at = NOW(), revoked_by_telegram_id = %s
            WHERE user_id = %s AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > NOW())
            """,
            (actor.id, target_pk),
        )
        count = cursor.rowcount
    await message.reply_text(
        f"Revoked {count} active Premium Pass record(s) for Telegram ID {target}."
        if count else f"No active Premium Pass found for Telegram ID {target}."
    )


async def premiumstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    message = update.effective_message
    if message is None or pool is None:
        return
    if len(context.args) != 1:
        await message.reply_text("Usage: /premiumstatus <Telegram ID or known @username>")
        return
    target = await resolve_user_id(context.args[0])
    if target is None:
        await message.reply_text("User not found. Use a numeric Telegram user ID or a known username.")
        return
    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT pp.granted_at, pp.expires_at, pp.revoked_at, pp.granted_by_telegram_id
            FROM premium_passes pp
            JOIN app_users u ON u.id = pp.user_id
            WHERE u.telegram_id = %s
            ORDER BY pp.granted_at DESC
            LIMIT 1
            """,
            (target,),
        )
        row = await cursor.fetchone()
    if row is None:
        await message.reply_text(f"No Premium Pass history for Telegram ID {target}.")
    elif row["revoked_at"] is not None:
        await message.reply_text(f"Latest pass for {target} was revoked at {row['revoked_at']}.")
    elif row["expires_at"] is not None and row["expires_at"] <= datetime.now(timezone.utc):
        await message.reply_text(f"Latest pass for {target} expired at {row['expires_at']}.")
    else:
        expiry = "Permanent until revoked" if row["expires_at"] is None else str(row["expires_at"])
        await message.reply_text(
            f"Latest Premium Pass for {target}\nStatus: ACTIVE\nExpiry: {expiry}\n"
            f"Granted by: {row['granted_by_telegram_id']}"
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in the hosting environment.")
    if not DATABASE_URL:
        raise SystemExit("Set DATABASE_URL in the hosting environment.")
    if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
        raise SystemExit("Set WEBHOOK_SECRET to a random secret of at least 16 characters.")

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
    application.add_handler(CommandHandler("mypremium", mypremium_command))
    application.add_handler(CommandHandler("grantpremium", grantpremium_command))
    application.add_handler(CommandHandler("revokepremium", revokepremium_command))
    application.add_handler(CommandHandler("premiumstatus", premiumstatus_command))
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
