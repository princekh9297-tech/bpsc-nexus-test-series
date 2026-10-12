import asyncio
import json
import logging
import os
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from telegram.ext import PollAnswerHandler
import question_bank as qb
import group_quiz as gq

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
MINI_APP_URL = os.environ.get("MINI_APP_URL", "").rstrip("/")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ADMIN_TELEGRAM_IDS = {
    item.strip()
    for item in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",")
    if item.strip().isdigit()
}
pool: AsyncConnectionPool | None = None

VAULTS = {
    "bpsc_pyq": "BPSC PYQ",
    "ghatnachakra_pyq": "Ghatnachakra PYQ",
    "upsc_pyq": "UPSC PYQ",
}
PASS_NAMES = {
    "bpsc_pyq": "BPSC PYQ Pass",
    "ghatnachakra_pyq": "Ghatnachakra PYQ Pass",
    "upsc_pyq": "UPSC PYQ Pass",
    "dual_vault": "Dual Vault Pass",
    "all_access": "All-Access Pass",
    "legacy": "Legacy Premium Pass",
}
DURATIONS = {"7": 7, "30": 30, "90": 90, "180": 180}


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and str(user.id) in ADMIN_TELEGRAM_IDS


def is_private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


async def ensure_schema() -> None:
    """Idempotent migration for existing Premium Pass schemas and Quiz Creator."""
    if pool is None:
        raise RuntimeError("Database pool is not available")

    statements = [
        # The catalogue is the canonical source of pass IDs. Create it before premium_passes.
        """CREATE TABLE IF NOT EXISTS pass_catalogue (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            pass_code TEXT NOT NULL,
            pass_name TEXT NOT NULL,
            description TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE
        )""",
        "ALTER TABLE pass_catalogue ADD COLUMN IF NOT EXISTS pass_code TEXT",
        "ALTER TABLE pass_catalogue ADD COLUMN IF NOT EXISTS pass_name TEXT",
        "ALTER TABLE pass_catalogue ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE pass_catalogue ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pass_catalogue_pass_code ON pass_catalogue(pass_code)",
        """INSERT INTO pass_catalogue (pass_code, pass_name, description, is_active) VALUES
            ('bpsc_pyq', 'BPSC PYQ Pass', 'Access to the BPSC PYQ vault', TRUE),
            ('ghatnachakra_pyq', 'Ghatnachakra PYQ Pass', 'Access to the Ghatnachakra PYQ vault', TRUE),
            ('upsc_pyq', 'UPSC PYQ Pass', 'Access to the UPSC PYQ vault', TRUE),
            ('dual_vault', 'Dual Vault Pass', 'Access to two selected vaults', TRUE),
            ('all_access', 'All-Access Pass', 'Access to all supported vaults', TRUE),
            ('legacy', 'Legacy Premium Pass', 'Migrated legacy entitlement', TRUE)
            ON CONFLICT (pass_code) DO NOTHING""",
        """CREATE TABLE IF NOT EXISTS premium_passes (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
            pass_id BIGINT NOT NULL REFERENCES pass_catalogue(id),
            granted_by_telegram_id BIGINT NOT NULL,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            revoked_by_telegram_id BIGINT,
            note TEXT,
            pass_code TEXT NOT NULL DEFAULT 'legacy',
            vault_keys TEXT[] NOT NULL DEFAULT ARRAY['general']::TEXT[],
            CHECK (expires_at IS NULL OR expires_at > granted_at),
            CHECK ((revoked_at IS NULL AND revoked_by_telegram_id IS NULL) OR
                   (revoked_at IS NOT NULL AND revoked_by_telegram_id IS NOT NULL))
        )""",
        "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS first_name TEXT",
        "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS username TEXT",
        "ALTER TABLE tests ADD COLUMN IF NOT EXISTS is_premium BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE tests ADD COLUMN IF NOT EXISTS vault_key TEXT NOT NULL DEFAULT 'general'",
        "ALTER TABLE tests ADD COLUMN IF NOT EXISTS created_by_telegram_id BIGINT",
        "ALTER TABLE tests ADD COLUMN IF NOT EXISTS creator_chat_id BIGINT",
        "ALTER TABLE tests ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'catalogue'",
        "ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS user_id BIGINT",
        "ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS pass_id BIGINT",
        "ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS granted_by_telegram_id BIGINT",
        "ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        "ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
        "ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ",
        "ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS revoked_by_telegram_id BIGINT",
        "ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS note TEXT",
        "ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS pass_code TEXT NOT NULL DEFAULT 'legacy'",
        "ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS vault_keys TEXT[] NOT NULL DEFAULT ARRAY['general']::TEXT[]",
        # Preserve any older/custom pass codes in the catalogue before filling required pass_id.
        """INSERT INTO pass_catalogue (pass_code, pass_name, description, is_active)
           SELECT DISTINCT pp.pass_code, INITCAP(REPLACE(pp.pass_code, '_', ' ')),
                  'Migrated existing pass type', TRUE
           FROM premium_passes pp
           WHERE pp.pass_code IS NOT NULL
           ON CONFLICT (pass_code) DO NOTHING""",
        """UPDATE premium_passes pp
           SET pass_id = pc.id
           FROM pass_catalogue pc
           WHERE pp.pass_id IS NULL AND pc.pass_code = pp.pass_code""",
        # Any unmatched legacy row receives the seeded legacy catalogue entry.
        """UPDATE premium_passes pp SET pass_id = pc.id
           FROM pass_catalogue pc
           WHERE pp.pass_id IS NULL AND pc.pass_code = 'legacy'""",
        "ALTER TABLE premium_passes ALTER COLUMN pass_id SET NOT NULL",
        """DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'premium_passes_pass_id_fkey'
              AND conrelid = 'premium_passes'::regclass
          ) THEN
            ALTER TABLE premium_passes
              ADD CONSTRAINT premium_passes_pass_id_fkey
              FOREIGN KEY (pass_id) REFERENCES pass_catalogue(id);
          END IF;
        END $$""",
        "CREATE INDEX IF NOT EXISTS idx_premium_passes_user ON premium_passes(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_premium_passes_expiry ON premium_passes(expires_at) WHERE revoked_at IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_tests_vault_key ON tests(vault_key)",
    ]
    async with pool.connection() as conn:
        for statement in statements:
            await conn.execute(statement)
        # Fail fast with useful logs if the deployed schema is incompatible.
        await conn.execute("SELECT id, telegram_id, first_name, username FROM app_users LIMIT 0")
        await conn.execute(
            "SELECT id, user_id, pass_id, granted_by_telegram_id, granted_at, expires_at, "
            "revoked_at, revoked_by_telegram_id, pass_code, vault_keys FROM premium_passes LIMIT 0"
        )
        await conn.execute("SELECT id, pass_code, pass_name, is_active FROM pass_catalogue LIMIT 0")
        await conn.execute(
            "SELECT id, title, is_published, is_premium, vault_key, created_by_telegram_id, "
            "creator_chat_id, visibility FROM tests LIMIT 0"
        )
    logger.info("Database migration verified: pass catalogue, pass_id relationship, and Question Bank schema.")


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
    await ensure_schema()
    await qb.ensure_schema(pool)
    await gq.ensure_schema(pool)
    application.bot_data["db_pool"] = pool
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Open BPSC Nexus Mini App"),
            BotCommand("mypremium", "View membership status"),
            BotCommand("tutorial", "How to use BPSC Nexus"),
            BotCommand("help", "Open the quick tutorial"),
        ], scope=BotCommandScopeAllPrivateChats())
        # Quiz-taking commands are intentionally not exposed in groups; attempts belong in the Mini App.
        await application.bot.set_my_commands([
            BotCommand("start", "Open BPSC Nexus"),
            BotCommand("tutorial", "How to use BPSC Nexus"),
            BotCommand("help", "Open the quick tutorial"),
        ], scope=BotCommandScopeAllGroupChats())
    except Exception:
        logger.exception("Could not register Telegram command menus")
    logger.info("Database connection verified; Premium Pass, Question Bank, and Group Quiz migrations applied automatically.")


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


async def resolve_user_id(identifier: str) -> int | None:
    """Resolve a numeric Telegram ID or a username already known to this bot."""
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
        cur = await conn.execute(
            "SELECT telegram_id FROM app_users WHERE LOWER(username) = LOWER(%s) LIMIT 1",
            (clean,),
        )
        row = await cur.fetchone()
    return int(row["telegram_id"]) if row else None


async def user_summary(telegram_id: int) -> dict | None:
    if pool is None:
        return None
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, telegram_id, first_name, username FROM app_users WHERE telegram_id = %s",
            (telegram_id,),
        )
        return await cur.fetchone()


async def active_passes_for_user(telegram_id: int) -> list[dict]:
    if pool is None:
        return []
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT pp.id, pp.pass_code, pp.vault_keys, pp.granted_at, pp.expires_at,
                   pp.revoked_at, pp.granted_by_telegram_id, u.telegram_id
            FROM premium_passes pp
            JOIN app_users u ON u.id = pp.user_id
            WHERE u.telegram_id = %s
              AND pp.revoked_at IS NULL
              AND (pp.expires_at IS NULL OR pp.expires_at > NOW())
            ORDER BY pp.granted_at DESC
            """,
            (telegram_id,),
        )
        return list(await cur.fetchall())


def pass_label(pass_code: str, vault_keys: list[str] | None = None) -> str:
    if pass_code == "dual_vault" and vault_keys:
        names = [VAULTS.get(key, key) for key in vault_keys if key != "*"]
        return "Dual Vault Pass" + (f" ({' + '.join(names)})" if names else "")
    if pass_code == "all_access":
        return "All-Access Pass"
    return PASS_NAMES.get(pass_code, pass_code.replace("_", " ").title())


def duration_label(expires_at) -> str:
    if expires_at is None:
        return "Permanent"
    return "Until " + expires_at.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


async def has_vault_access(telegram_id: int, vault_key: str) -> bool:
    if pool is None:
        return False
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM premium_passes pp
                JOIN app_users u ON u.id = pp.user_id
                WHERE u.telegram_id = %s
                  AND pp.revoked_at IS NULL
                  AND (pp.expires_at IS NULL OR pp.expires_at > NOW())
                  AND (%s = ANY(pp.vault_keys) OR '*' = ANY(pp.vault_keys))
            ) AS allowed
            """,
            (telegram_id, vault_key),
        )
        row = await cur.fetchone()
    return bool(row and row["allowed"])


async def reply_with_uluk(message, caption: str, reply_markup=None) -> None:
    """Send ULUK mascot branding where the bundled asset is available; fall back safely."""
    mascot_path = Path(__file__).resolve().parent / "static" / "uluk.png"
    if mascot_path.is_file():
        try:
            with mascot_path.open("rb") as mascot:
                await message.reply_photo(photo=mascot, caption=caption, reply_markup=reply_markup)
            return
        except Exception:
            logger.exception("Could not send ULUK mascot image; falling back to text")
    await message.reply_text(caption, reply_markup=reply_markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text("Please open me in a private chat to use the bot.")
        return
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    await save_user(update)
    if context.args and context.args[0].startswith("quiz_"):
        await message.reply_text("This quiz is available only inside the BPSC Nexus Mini App. Open the app below.", reply_markup=main_user_keyboard())
        return
    await reply_with_uluk(
        message,
        f"Welcome to BPSC Nexus, {user.first_name or 'Aspirant'}! 🦉\n\nMeet ULUK, your BPSC Nexus mascot. Your quizzes, planner, results and study tools are available in the Mini App.",
        reply_markup=main_user_keyboard(),
    )


def main_user_keyboard() -> InlineKeyboardMarkup:
    keyboard = []
    if MINI_APP_URL.startswith("https://"):
        keyboard.append([InlineKeyboardButton("🚀 Open BPSC Nexus Mini App", web_app=WebAppInfo(url=MINI_APP_URL))])
    else:
        keyboard.append([InlineKeyboardButton("🚀 Open Mini App", callback_data="user:openapp")])
    keyboard.extend([
        [InlineKeyboardButton("💎 My Membership", callback_data="user:premium")],
        [InlineKeyboardButton("📘 Tutorial", callback_data="user:tutorial")],
    ])
    return InlineKeyboardMarkup(keyboard)


async def tutorial_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not is_private(update):
        await message.reply_text("Please open a private chat with BPSC Nexus to launch the Mini App and view the tutorial.")
        return
    await reply_with_uluk(
        message,
        "📘 BPSC Nexus — Quick Tutorial · ULUK\n\n"
        "1. Tap Open Mini App to enter your study workspace.\n"
        "2. Use Test Series to attempt published quizzes.\n"
        "3. Use Create to build a quiz from the available question bank.\n"
        "4. Use the Study Planner to manage tasks.\n"
        "5. Check Analytics for submitted quiz results and use Profile to change themes.\n\n"
        "Quiz attempts are available only inside the Mini App.",
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
    await tutorial_command(update, context)

async def mypremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        return
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    await save_user(update)
    passes = await active_passes_for_user(user.id)
    if not passes:
        await message.reply_text(
            "💎 My Membership\n\nNo active Premium Pass was found on your account. Your standard Mini App access remains available.",
            reply_markup=main_user_keyboard(),
        )
        return
    lines = ["💎 Your active Premium Passes", ""]
    for item in passes:
        lines.append(f"• {pass_label(item['pass_code'], item['vault_keys'])}")
        lines.append(f"  {duration_label(item['expires_at'])}")
    await message.reply_text("\n".join(lines), reply_markup=main_user_keyboard())

async def tests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text("Please open me in a private chat to browse tests.")
        return
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    await save_user(update)
    if pool is None:
        await message.reply_text("The test catalogue is temporarily unavailable. Please try again shortly.")
        return
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT t.id, t.title, t.description, t.test_type, t.duration_minutes,
                   t.total_marks, t.is_premium, t.vault_key,
                   EXISTS (
                     SELECT 1 FROM premium_passes pp
                     JOIN app_users u ON u.id = pp.user_id
                     WHERE u.telegram_id = %s
                       AND pp.revoked_at IS NULL
                       AND (pp.expires_at IS NULL OR pp.expires_at > NOW())
                       AND (t.vault_key = ANY(pp.vault_keys) OR '*' = ANY(pp.vault_keys))
                   ) AS has_vault_access
            FROM tests t
            WHERE t.is_published = TRUE
            ORDER BY t.id DESC
            LIMIT 50
            """,
            (user.id,),
        )
        tests = list(await cur.fetchall())

    if not tests:
        await message.reply_text("No tests are published yet. Please check again later.")
        return
    lines = ["📚 Published tests", ""]
    keyboard = []
    for test in tests:
        entitled = bool(test["has_vault_access"])
        # Legacy is_premium flag is respected for tests that have not yet been assigned to a vault.
        unlocked = (not test["is_premium"]) or entitled
        vault_name = VAULTS.get(test["vault_key"], str(test["vault_key"]).replace("_", " ").title())
        marker = "✅" if unlocked else "🔒"
        lines.append(f"{marker} #{test['id']} — {test['title']}")
        lines.append(f"Vault: {vault_name} | {test['duration_minutes']} min | {test['total_marks']} marks")
        if test["description"]:
            lines.append(str(test["description"])[:180])
        if not unlocked:
            lines.append("Access required: matching Premium Pass")
        lines.append("")
    lines.append("Question attempts and answer submission are a separate development stage.")
    await message.reply_text("\n".join(lines))


# ---------- Admin panel UI ----------
def admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗃 Question Bank & Quiz Studio", callback_data="qb:home")],
        [InlineKeyboardButton("➕ Grant Pass", callback_data="adm:grant"),
         InlineKeyboardButton("👥 Manage Members", callback_data="adm:manage")],
        [InlineKeyboardButton("🗂 Pass Catalogue", callback_data="adm:catalogue"),
         InlineKeyboardButton("📊 Statistics", callback_data="adm:stats")],
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


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖ Cancel", callback_data="adm:cancel")]])


def duration_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("7 days", callback_data=f"adm:{prefix}:dur:7"),
         InlineKeyboardButton("30 days", callback_data=f"adm:{prefix}:dur:30")],
        [InlineKeyboardButton("90 days", callback_data=f"adm:{prefix}:dur:90"),
         InlineKeyboardButton("180 days", callback_data=f"adm:{prefix}:dur:180")],
        [InlineKeyboardButton("♾ Permanent", callback_data=f"adm:{prefix}:dur:permanent"),
         InlineKeyboardButton("✍️ Custom days", callback_data=f"adm:{prefix}:dur:custom")],
        [InlineKeyboardButton("⬅ Back to Admin Panel", callback_data="adm:home")],
    ])


def pass_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("BPSC PYQ", callback_data="adm:pass:bpsc_pyq")],
        [InlineKeyboardButton("Ghatnachakra PYQ", callback_data="adm:pass:ghatnachakra_pyq")],
        [InlineKeyboardButton("UPSC PYQ", callback_data="adm:pass:upsc_pyq")],
        [InlineKeyboardButton("🔀 Dual Vault (choose 2)", callback_data="adm:pass:dual_vault")],
        [InlineKeyboardButton("👑 All-Access", callback_data="adm:pass:all_access")],
        [InlineKeyboardButton("⬅ Back to Admin Panel", callback_data="adm:home")],
    ])


def dual_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for key, label in VAULTS.items():
        mark = "✅ " if key in selected else "▫️ "
        rows.append([InlineKeyboardButton(mark + label, callback_data=f"adm:dual:toggle:{key}")])
    rows.append([InlineKeyboardButton("Continue with selected vaults", callback_data="adm:dual:done")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="adm:grant:passmenu")])
    return InlineKeyboardMarkup(rows)


async def ask_target(query, context: ContextTypes.DEFAULT_TYPE, flow: str) -> None:
    context.user_data["admin_flow"] = {"step": flow}
    await query.edit_message_text(
        "Send the student's Telegram username (e.g. @student) or numeric Telegram ID.\n\n"
        "Username lookup works only if the student has previously started this bot. If not found, use their numeric ID.",
        reply_markup=cancel_keyboard(),
    )


async def begin_duration(query, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    flow = context.user_data.get("admin_flow", {})
    flow["step"] = f"{mode}_duration"
    context.user_data["admin_flow"] = flow
    await query.edit_message_text("Choose the pass duration:", reply_markup=duration_keyboard(mode))


async def lookup_target_for_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, identifier: str) -> bool:
    target = await resolve_user_id(identifier)
    if target is None:
        await update.effective_message.reply_text(
            "I couldn't find that username. Ask the student to press /start first, or enter their numeric Telegram ID."
        )
        return False
    person = await user_summary(target)
    if person is None:
        await update.effective_message.reply_text("Couldn't load this student from the database. Please try the numeric ID again.")
        return False
    context.user_data.setdefault("admin_flow", {})["target_id"] = target
    context.user_data["admin_flow"]["target_name"] = person.get("first_name") or "Student"
    context.user_data["admin_flow"]["target_username"] = person.get("username")
    return True


async def grant_confirmation(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("admin_flow", {})
    target_id = flow.get("target_id")
    pass_code = flow.get("pass_code")
    vault_keys = flow.get("vault_keys", [])
    duration = flow.get("duration")
    if not target_id or not pass_code or not vault_keys or duration is None:
        await query.edit_message_text("This grant session is incomplete. Please start again from the Admin Panel.", reply_markup=admin_home_keyboard())
        context.user_data.pop("admin_flow", None)
        return
    person = await user_summary(int(target_id))
    username = f" (@{person['username']})" if person and person.get("username") else ""
    expiry = "Permanent" if duration == "permanent" else f"{duration} days"
    label = pass_label(pass_code, vault_keys)
    flow["step"] = "grant_confirm"
    context.user_data["admin_flow"] = flow
    await query.edit_message_text(
        "Please confirm this membership grant:\n\n"
        f"Student: {flow.get('target_name', 'Student')}{username}\n"
        f"Telegram ID: {target_id}\n"
        f"Pass: {label}\n"
        f"Duration: {expiry}\n\n"
        "Existing separate passes will be preserved.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Grant", callback_data="adm:grant:confirm")],
            [InlineKeyboardButton("✖ Cancel", callback_data="adm:cancel")],
        ]),
    )


async def save_grant(actor_id: int, flow: dict) -> None:
    if pool is None:
        raise RuntimeError("Database is unavailable")
    target_id = int(flow["target_id"])
    pass_code = str(flow["pass_code"])
    vault_keys = list(flow["vault_keys"])
    duration = flow["duration"]
    expires_at = None if duration == "permanent" else datetime.now(timezone.utc) + timedelta(days=int(duration))
    async with pool.connection() as conn:
        # Resolve and insert in one transaction to avoid stale user summaries/FK races.
        cur = await conn.execute(
            "SELECT id FROM app_users WHERE telegram_id = %s FOR UPDATE", (target_id,)
        )
        target = await cur.fetchone()
        if target is None:
            raise RuntimeError(f"No app_users row exists for Telegram ID {target_id}")
        # pass_id is NOT NULL in the deployed Neon schema. Always resolve the
        # selected pass to its canonical catalogue ID before inserting.
        cur = await conn.execute(
            "SELECT id FROM pass_catalogue WHERE pass_code = %s AND is_active = TRUE",
            (pass_code,),
        )
        catalogue_row = await cur.fetchone()
        if catalogue_row is None:
            raise RuntimeError(f"Pass code {pass_code!r} is missing or inactive in pass_catalogue")
        await conn.execute(
            """INSERT INTO premium_passes
               (user_id, pass_id, granted_by_telegram_id, expires_at, note, pass_code, vault_keys)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (target["id"], catalogue_row["id"], actor_id, expires_at,
             "Granted via admin panel", pass_code, vault_keys),
        )


async def show_member(query, telegram_id: int) -> None:
    person = await user_summary(telegram_id)
    if person is None:
        await query.edit_message_text("Student not found in the database.", reply_markup=admin_home_keyboard())
        return
    display = person.get("first_name") or "Student"
    if person.get("username"):
        display += f" (@{person['username']})"
    passes = await active_passes_for_user(telegram_id)
    lines = [f"👤 {display}", f"Telegram ID: {telegram_id}", ""]
    keyboard = []
    if passes:
        lines.append("Active passes:")
        for item in passes:
            lines.append(f"• #{item['id']} {pass_label(item['pass_code'], item['vault_keys'])} — {duration_label(item['expires_at'])}")
            row = []
            if item["expires_at"] is not None:
                row.append(InlineKeyboardButton(f"Extend #{item['id']}", callback_data=f"adm:extend:{item['id']}"))
            row.append(InlineKeyboardButton(f"Revoke #{item['id']}", callback_data=f"adm:revoke:{item['id']}"))
            keyboard.append(row)
    else:
        lines.append("No active passes found.")
    keyboard.append([InlineKeyboardButton("➕ Grant another pass", callback_data=f"adm:grantto:{telegram_id}")])
    keyboard.append([InlineKeyboardButton("🔎 Find another student", callback_data="adm:manage")])
    keyboard.append([InlineKeyboardButton("⬅ Admin Panel", callback_data="adm:home")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_stats(query) -> None:
    if pool is None:
        await query.edit_message_text("Database unavailable.", reply_markup=admin_home_keyboard())
        return
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT COUNT(*) FILTER (WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())) AS active_passes,
                   COUNT(DISTINCT user_id) FILTER (WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())) AS active_students,
                   COUNT(*) FILTER (WHERE revoked_at IS NULL AND expires_at IS NOT NULL AND expires_at <= NOW()) AS expired_passes,
                   COUNT(*) FILTER (WHERE revoked_at IS NOT NULL) AS revoked_passes,
                   COUNT(*) FILTER (WHERE revoked_at IS NULL AND expires_at IS NULL) AS permanent_passes
            FROM premium_passes
            """
        )
        row = await cur.fetchone()
    await query.edit_message_text(
        "📊 Membership Statistics\n\n"
        f"Active students: {row['active_students']}\n"
        f"Active passes: {row['active_passes']}\n"
        f"Permanent active passes: {row['permanent_passes']}\n"
        f"Expired passes: {row['expired_passes']}\n"
        f"Revoked passes: {row['revoked_passes']}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Admin Panel", callback_data="adm:home")]]),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    # Legacy Telegram quiz buttons must never start or continue a quiz attempt.
    if data.startswith("play:"):
        await query.answer("Quiz attempts are available only inside the BPSC Nexus Mini App.", show_alert=True)
        return
    handled = await qb.route_callback(update, context, pool, admin_only(update))
    if handled:
        return
    if data.startswith("qc:"):
        await query.answer("Photo/PDF quiz creation is deferred to a later phase.", show_alert=True)
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
        elif data == "adm:grant":
            await ask_target(query, context, "grant_target")
        elif data == "adm:manage":
            await ask_target(query, context, "manage_target")
        elif data == "adm:catalogue":
            await query.edit_message_text(
                "🗂 Pass Catalogue\n\n"
                "• BPSC PYQ Pass — BPSC PYQ vault\n"
                "• Ghatnachakra PYQ Pass — Ghatnachakra vault\n"
                "• UPSC PYQ Pass — UPSC PYQ vault\n"
                "• Dual Vault Pass — choose any two vaults when granting\n"
                "• All-Access Pass — every mapped vault, including future vaults\n\n"
                "Available durations: 7, 30, 90, 180 days, permanent, or custom days.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Admin Panel", callback_data="adm:home")]]),
            )
        elif data == "adm:stats":
            await admin_stats(query)
        elif data == "adm:grant:passmenu":
            await query.edit_message_text("Choose a pass:", reply_markup=pass_keyboard())
        elif data.startswith("adm:pass:"):
            code = data.split(":", 2)[2]
            flow = context.user_data.get("admin_flow", {})
            if flow.get("step") not in {"grant_pass", "grant_target", "dual_vaults"} or not flow.get("target_id"):
                await query.edit_message_text("Grant session expired. Start again.", reply_markup=admin_home_keyboard())
                context.user_data.pop("admin_flow", None)
                return
            if code == "dual_vault":
                flow["step"] = "dual_vaults"
                flow["selected_vaults"] = []
                context.user_data["admin_flow"] = flow
                await query.edit_message_text("Select exactly two vaults for this Dual Vault Pass:", reply_markup=dual_keyboard([]))
            elif code in VAULTS:
                flow["pass_code"] = code
                flow["vault_keys"] = [code]
                flow["step"] = "grant_duration"
                context.user_data["admin_flow"] = flow
                await query.edit_message_text(f"Selected: {PASS_NAMES[code]}\nChoose duration:", reply_markup=duration_keyboard("grant"))
            elif code == "all_access":
                flow["pass_code"] = code
                flow["vault_keys"] = ["*"]
                flow["step"] = "grant_duration"
                context.user_data["admin_flow"] = flow
                await query.edit_message_text("Selected: All-Access Pass\nChoose duration:", reply_markup=duration_keyboard("grant"))
        elif data.startswith("adm:dual:toggle:"):
            key = data.split(":", 3)[3]
            flow = context.user_data.get("admin_flow", {})
            if flow.get("step") != "dual_vaults" or key not in VAULTS:
                await query.edit_message_text("Dual Vault selection expired. Start again.", reply_markup=admin_home_keyboard())
                return
            selected = list(flow.get("selected_vaults", []))
            if key in selected:
                selected.remove(key)
            elif len(selected) < 2:
                selected.append(key)
            flow["selected_vaults"] = selected
            context.user_data["admin_flow"] = flow
            await query.edit_message_text(f"Select exactly two vaults ({len(selected)}/2 selected):", reply_markup=dual_keyboard(selected))
        elif data == "adm:dual:done":
            flow = context.user_data.get("admin_flow", {})
            selected = list(flow.get("selected_vaults", []))
            if flow.get("step") != "dual_vaults" or len(selected) != 2:
                await query.edit_message_text(
                    f"Please select exactly two vaults ({len(selected)}/2 selected):",
                    reply_markup=dual_keyboard(selected),
                )
                return
            flow["pass_code"] = "dual_vault"
            flow["vault_keys"] = selected
            flow["step"] = "grant_duration"
            context.user_data["admin_flow"] = flow
            await query.edit_message_text(
                f"Selected: Dual Vault Pass ({' + '.join(VAULTS[k] for k in selected)})\nChoose duration:",
                reply_markup=duration_keyboard("grant"),
            )
        elif data.startswith("adm:grant:dur:"):
            value = data.rsplit(":", 1)[1]
            flow = context.user_data.get("admin_flow", {})
            if flow.get("step") != "grant_duration":
                await query.edit_message_text("Grant session expired. Start again.", reply_markup=admin_home_keyboard())
                return
            if value == "custom":
                flow["step"] = "grant_custom_days"
                context.user_data["admin_flow"] = flow
                await query.edit_message_text("Send the number of days (1–3650).", reply_markup=cancel_keyboard())
            else:
                flow["duration"] = "permanent" if value == "permanent" else DURATIONS.get(value)
                context.user_data["admin_flow"] = flow
                await grant_confirmation(query, context)
        elif data == "adm:grant:confirm":
            flow = context.user_data.get("admin_flow", {})
            if flow.get("step") != "grant_confirm":
                await query.edit_message_text("Grant session expired. Start again.", reply_markup=admin_home_keyboard())
                context.user_data.pop("admin_flow", None)
                return
            try:
                await save_grant(update.effective_user.id, flow)
            except Exception as exc:
                logger.exception("Unable to grant pass; database error type=%s detail=%s", type(exc).__name__, str(exc)[:700])
                await query.edit_message_text(
                    "❌ The pass could not be saved. I logged the exact database error on the server. "
                    "No success has been reported and existing passes were not intentionally changed. "
                    "Please check the latest Render log if this repeats.",
                    reply_markup=admin_home_keyboard(),
                )
                context.user_data.pop("admin_flow", None)
                return
            target_id = int(flow["target_id"])
            label = pass_label(flow["pass_code"], flow["vault_keys"])
            duration = "Permanent" if flow["duration"] == "permanent" else f"{flow['duration']} days"
            context.user_data.pop("admin_flow", None)
            await query.edit_message_text(
                f"✅ Pass granted successfully.\n\nStudent ID: {target_id}\nPass: {label}\nDuration: {duration}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 Manage this student", callback_data=f"adm:member:{target_id}")],
                    [InlineKeyboardButton("⬅ Admin Panel", callback_data="adm:home")],
                ]),
            )
        elif data.startswith("adm:grantto:"):
            target_id = int(data.rsplit(":", 1)[1])
            person = await user_summary(target_id)
            if person is None:
                await query.edit_message_text("Student not found.", reply_markup=admin_home_keyboard())
                return
            context.user_data["admin_flow"] = {
                "step": "grant_pass", "target_id": target_id,
                "target_name": person.get("first_name") or "Student",
                "target_username": person.get("username"),
            }
            await query.edit_message_text("Choose a pass for this student:", reply_markup=pass_keyboard())
        elif data.startswith("adm:member:"):
            target_id = int(data.rsplit(":", 1)[1])
            await show_member(query, target_id)
        elif data.startswith("adm:extend:") and data != "adm:extend:confirm" and ":dur:" not in data:
            pass_id = int(data.rsplit(":", 1)[1])
            context.user_data["admin_flow"] = {"step": "extend_duration", "pass_id": pass_id}
            await query.edit_message_text("Choose how much time to add to this pass:", reply_markup=duration_keyboard("extend"))
        elif data.startswith("adm:extend:dur:"):
            value = data.rsplit(":", 1)[1]
            flow = context.user_data.get("admin_flow", {})
            if flow.get("step") != "extend_duration":
                await query.edit_message_text("Extension session expired.", reply_markup=admin_home_keyboard())
                return
            if value == "custom":
                flow["step"] = "extend_custom_days"
                context.user_data["admin_flow"] = flow
                await query.edit_message_text("Send the number of days to add (1–3650).", reply_markup=cancel_keyboard())
            else:
                flow["extension"] = "permanent" if value == "permanent" else DURATIONS.get(value)
                context.user_data["admin_flow"] = flow
                await query.edit_message_text(
                    f"Confirm extension of pass #{flow['pass_id']} by {('permanent access' if flow['extension'] == 'permanent' else str(flow['extension']) + ' days')}?",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Confirm Extension", callback_data="adm:extend:confirm")],
                        [InlineKeyboardButton("✖ Cancel", callback_data="adm:cancel")],
                    ]),
                )
        elif data == "adm:extend:confirm":
            flow = context.user_data.get("admin_flow", {})
            if flow.get("step") != "extend_duration" or "extension" not in flow:
                await query.edit_message_text("Extension session expired.", reply_markup=admin_home_keyboard())
                return
            pass_id = int(flow["pass_id"])
            extension = flow["extension"]
            if pool is None:
                await query.edit_message_text("Database unavailable.", reply_markup=admin_home_keyboard())
                return
            async with pool.connection() as conn:
                if extension == "permanent":
                    cur = await conn.execute(
                        "UPDATE premium_passes SET expires_at = NULL WHERE id = %s AND revoked_at IS NULL RETURNING user_id",
                        (pass_id,),
                    )
                else:
                    cur = await conn.execute(
                        """UPDATE premium_passes SET expires_at = GREATEST(COALESCE(expires_at, NOW()), NOW()) + (%s * INTERVAL '1 day')
                           WHERE id = %s AND revoked_at IS NULL AND expires_at IS NOT NULL RETURNING user_id""",
                        (int(extension), pass_id),
                    )
                row = await cur.fetchone()
                if row is None and extension != "permanent":
                    await query.edit_message_text("This pass may be revoked or permanent already. No change was made.", reply_markup=admin_home_keyboard())
                    context.user_data.pop("admin_flow", None)
                    return
                if row is None:
                    await query.edit_message_text("Pass not found or already revoked.", reply_markup=admin_home_keyboard())
                    context.user_data.pop("admin_flow", None)
                    return
                cur = await conn.execute("SELECT telegram_id FROM app_users WHERE id = %s", (row["user_id"],))
                owner = await cur.fetchone()
            context.user_data.pop("admin_flow", None)
            await query.edit_message_text("✅ Pass updated successfully.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 Manage this student", callback_data=f"adm:member:{owner['telegram_id']}")],
                [InlineKeyboardButton("⬅ Admin Panel", callback_data="adm:home")],
            ]))
        elif data.startswith("adm:revoke:") and data != "adm:revoke:confirm":
            pass_id = int(data.rsplit(":", 1)[1])
            context.user_data["admin_flow"] = {"step": "revoke_confirm", "pass_id": pass_id}
            await query.edit_message_text(
                f"Revoke pass #{pass_id}? The student will lose access from this pass, but other passes remain active.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 Confirm Revoke", callback_data="adm:revoke:confirm")],
                    [InlineKeyboardButton("✖ Cancel", callback_data="adm:cancel")],
                ]),
            )
        elif data == "adm:revoke:confirm":
            flow = context.user_data.get("admin_flow", {})
            if flow.get("step") != "revoke_confirm":
                await query.edit_message_text("Revoke session expired.", reply_markup=admin_home_keyboard())
                return
            if pool is None:
                await query.edit_message_text("Database unavailable.", reply_markup=admin_home_keyboard())
                return
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "UPDATE premium_passes SET revoked_at = NOW(), revoked_by_telegram_id = %s WHERE id = %s AND revoked_at IS NULL RETURNING user_id",
                    (update.effective_user.id, int(flow["pass_id"])),
                )
                row = await cur.fetchone()
                if row:
                    cur = await conn.execute("SELECT telegram_id FROM app_users WHERE id = %s", (row["user_id"],))
                    owner = await cur.fetchone()
                else:
                    owner = None
            context.user_data.pop("admin_flow", None)
            if owner:
                await query.edit_message_text("✅ Pass revoked. Other passes for this student are unchanged.", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 Manage this student", callback_data=f"adm:member:{owner['telegram_id']}")],
                    [InlineKeyboardButton("⬅ Admin Panel", callback_data="adm:home")],
                ]))
            else:
                await query.edit_message_text("No active pass was changed.", reply_markup=admin_home_keyboard())
        else:
            await query.edit_message_text("Unknown action. Return to the Admin Panel.", reply_markup=admin_home_keyboard())
        return

    # User callbacks never expose admin actions.
    if data == "user:premium":
        await query.answer()
        await mypremium_command(update, context)
    elif data == "user:tutorial":
        await query.answer()
        await tutorial_command(update, context)
    elif data == "user:openapp":
        await query.answer()
        await query.message.reply_text("Set MINI_APP_URL to your Mini App's public HTTPS URL in the bot service settings, then reopen /start.")
    elif data in {"user:tests", "user:quizzes"}:
        await query.answer("Please open BPSC Nexus Mini App to access quizzes.", show_alert=True)
    else:
        await query.answer("This action is no longer available.", show_alert=True)


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or not message.text or user is None:
        return
    # Ignore ordinary user text completely; admin text prompts work only in private chat.
    if not admin_only(update):
        return
    handled = await qb.admin_text_callback(update, context, pool, True)
    if handled:
        return
    flow = context.user_data.get("admin_flow", {})
    step = flow.get("step")
    if step in {"grant_target", "manage_target"}:
        if not await lookup_target_for_flow(update, context, message.text.strip()):
            return
        target_id = int(context.user_data["admin_flow"]["target_id"])
        if step == "grant_target":
            context.user_data["admin_flow"]["step"] = "grant_pass"
            await message.reply_text("Choose a pass:", reply_markup=pass_keyboard())
        else:
            context.user_data.pop("admin_flow", None)
            await message.reply_text("Student membership details:")
            # Send a fresh management view because this step started from a typed identifier.
            await send_member_message(message, target_id)
    elif step == "grant_custom_days":
        try:
            days = int(message.text.strip())
        except ValueError:
            await message.reply_text("Enter a whole number from 1 to 3650.")
            return
        if not 1 <= days <= 3650:
            await message.reply_text("Enter a whole number from 1 to 3650.")
            return
        flow["duration"] = days
        context.user_data["admin_flow"] = flow
        await send_grant_confirmation_message(message, context)
    elif step == "extend_custom_days":
        try:
            days = int(message.text.strip())
        except ValueError:
            await message.reply_text("Enter a whole number from 1 to 3650.")
            return
        if not 1 <= days <= 3650:
            await message.reply_text("Enter a whole number from 1 to 3650.")
            return
        flow["extension"] = days
        flow["step"] = "extend_duration"
        context.user_data["admin_flow"] = flow
        await message.reply_text(
            f"Confirm adding {days} days to pass #{flow['pass_id']}?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Extension", callback_data="adm:extend:confirm")],
                [InlineKeyboardButton("✖ Cancel", callback_data="adm:cancel")],
            ]),
        )


async def send_grant_confirmation_message(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("admin_flow", {})
    person = await user_summary(int(flow["target_id"]))
    username = f" (@{person['username']})" if person and person.get("username") else ""
    expiry = "Permanent" if flow["duration"] == "permanent" else f"{flow['duration']} days"
    await message.reply_text(
        "Please confirm this membership grant:\n\n"
        f"Student: {flow.get('target_name', 'Student')}{username}\n"
        f"Telegram ID: {flow['target_id']}\n"
        f"Pass: {pass_label(flow['pass_code'], flow['vault_keys'])}\n"
        f"Duration: {expiry}\n\nExisting separate passes will be preserved.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Grant", callback_data="adm:grant:confirm")],
            [InlineKeyboardButton("✖ Cancel", callback_data="adm:cancel")],
        ]),
    )
    context.user_data["admin_flow"]["step"] = "grant_confirm"


async def send_member_message(message, telegram_id: int) -> None:
    person = await user_summary(telegram_id)
    if person is None:
        await message.reply_text("Student not found.")
        return
    display = person.get("first_name") or "Student"
    if person.get("username"):
        display += f" (@{person['username']})"
    passes = await active_passes_for_user(telegram_id)
    lines = [f"👤 {display}", f"Telegram ID: {telegram_id}", ""]
    keyboard = []
    for item in passes:
        lines.append(f"• #{item['id']} {pass_label(item['pass_code'], item['vault_keys'])} — {duration_label(item['expires_at'])}")
        row = []
        if item["expires_at"] is not None:
            row.append(InlineKeyboardButton(f"Extend #{item['id']}", callback_data=f"adm:extend:{item['id']}"))
        row.append(InlineKeyboardButton(f"Revoke #{item['id']}", callback_data=f"adm:revoke:{item['id']}"))
        keyboard.append(row)
    if not passes:
        lines.append("No active passes found.")
    keyboard.append([InlineKeyboardButton("➕ Grant another pass", callback_data=f"adm:grantto:{telegram_id}")])
    keyboard.append([InlineKeyboardButton("⬅ Admin Panel", callback_data="adm:home")])
    await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


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
    application.add_handler(CommandHandler("mypremium", mypremium_command))
    application.add_handler(CommandHandler("tutorial", tutorial_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(admin_callback))
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
