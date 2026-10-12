"""Persistent sequential Telegram group quiz engine for BPSC Nexus."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from html import escape as html_escape
from io import BytesIO
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import ContextTypes
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

log = logging.getLogger("bpsc_nexus_group_quiz")

DDL = [
    """CREATE TABLE IF NOT EXISTS group_quiz_sessions (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        chat_title TEXT,
        quiz_id BIGINT NOT NULL REFERENCES bank_quizzes(id) ON DELETE RESTRICT,
        quiz_title TEXT NOT NULL,
        started_by BIGINT NOT NULL,
        interval_seconds INTEGER NOT NULL CHECK (interval_seconds BETWEEN 5 AND 600),
        questions_count INTEGER NOT NULL,
        mode TEXT NOT NULL DEFAULT 'learning' CHECK (mode IN ('learning','exam')),
        current_index INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','failed','paused','cancelled')),
        next_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ
    )""",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'learning'",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS positive_marks NUMERIC(8,3) NOT NULL DEFAULT 1",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS negative_marks NUMERIC(8,3) NOT NULL DEFAULT 0.333",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS announcement_sent BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS report_sent BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS summary_sent BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS answer_key_sent BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS group_report_sent BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS summary_claimed_at TIMESTAMPTZ",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS group_report_claimed_at TIMESTAMPTZ",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS launch_announcement_message_id BIGINT",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS launch_announcement_deleted BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS brand_name TEXT",
    "ALTER TABLE group_quiz_sessions ADD COLUMN IF NOT EXISTS progress_message_id BIGINT",

    "CREATE INDEX IF NOT EXISTS idx_group_quiz_sessions_due ON group_quiz_sessions(status,next_at) WHERE status='active'",
    """CREATE TABLE IF NOT EXISTS group_quiz_session_questions (
        session_id BIGINT NOT NULL REFERENCES group_quiz_sessions(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        question_id BIGINT NOT NULL,
        question_en TEXT NOT NULL,
        question_hi TEXT,
        options JSONB NOT NULL,
        option_letters JSONB NOT NULL,
        correct_option TEXT NOT NULL,
        explanation_en TEXT,
        explanation_hi TEXT,
        poll_id TEXT,
        message_id BIGINT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','posting','open','closed','failed')),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(session_id,position),
        UNIQUE(poll_id)
    )""",
    """CREATE TABLE IF NOT EXISTS group_quiz_answers (
        session_id BIGINT NOT NULL,
        position INTEGER NOT NULL,
        telegram_id BIGINT NOT NULL,
        first_name TEXT,
        username TEXT,
        selected_letter TEXT,
        answered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(session_id,position,telegram_id),
        FOREIGN KEY(session_id,position) REFERENCES group_quiz_session_questions(session_id,position) ON DELETE CASCADE
    )""",
    "ALTER TABLE group_quiz_session_questions ADD COLUMN IF NOT EXISTS message_id BIGINT",
    "ALTER TABLE group_quiz_session_questions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE group_quiz_session_questions ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS idx_group_quiz_answers_session ON group_quiz_answers(session_id,telegram_id)",
    """CREATE TABLE IF NOT EXISTS group_quiz_memberships (
        chat_id BIGINT PRIMARY KEY,
        chat_title TEXT,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        granted_by BIGINT NOT NULL,
        granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        revoked_at TIMESTAMPTZ
    )""",
    "ALTER TABLE group_quiz_memberships ADD COLUMN IF NOT EXISTS brand_name TEXT",
    "CREATE INDEX IF NOT EXISTS idx_group_quiz_memberships_active ON group_quiz_memberships(is_active,chat_id)",
    """CREATE TABLE IF NOT EXISTS group_quiz_admin_reports (
        session_id BIGINT NOT NULL REFERENCES group_quiz_sessions(id) ON DELETE CASCADE,
        chunk_no INTEGER NOT NULL,
        report_text TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(session_id,chunk_no)
    )""",
]


async def ensure_schema(pool):
    async with pool.connection() as conn:
        for stmt in DDL:
            await conn.execute(stmt)


async def _group_is_authorized(pool, chat_id: int) -> bool:
    """Only explicitly granted groups may use BPSC Nexus group quiz features."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM group_quiz_memberships WHERE chat_id=%s AND is_active=TRUE",
            (int(chat_id),),
        )
        return await cur.fetchone() is not None


async def _require_group_membership(message, pool, chat) -> bool:
    if await _group_is_authorized(pool, chat.id):
        return True
    await message.reply_text(
        "🔒 This group does not have BPSC Nexus Quiz membership.\n"
        "Ask the BPSC Nexus owner to activate this group."
    )
    return False


async def _is_chat_admin(context, chat_id: int, user_id: int) -> bool:
    """Verify group admin/owner status using both Telegram API methods."""
    def normalize_status(member) -> str:
        status = getattr(member, "status", "")
        # Support plain strings and enum-like values across PTB versions.
        status = getattr(status, "value", status)
        return str(status or "").strip().lower().rsplit(".", 1)[-1]

    admin_statuses = {"administrator", "creator", "owner", "admin"}
    chat_id, user_id = int(chat_id), int(user_id)

    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if normalize_status(member) in admin_statuses:
            return True
        log.info("Admin check: get_chat_member returned status=%r for user=%s chat=%s",
                 normalize_status(member), user_id, chat_id)
    except Exception:
        log.exception("get_chat_member failed for user=%s chat=%s", user_id, chat_id)

    # Always check the administrator list as a second source, including when
    # get_chat_member returns a status that doesn't look like an admin.
    try:
        admins = await context.bot.get_chat_administrators(chat_id=chat_id)
        for admin in admins:
            admin_user = getattr(admin, "user", None)
            if admin_user and int(admin_user.id) == user_id:
                status = normalize_status(admin)
                # Presence in Telegram's administrator list is authoritative.
                if status in admin_statuses or not status:
                    return True
                log.warning("User %s appeared in admin list with unexpected status=%r in chat=%s",
                            user_id, status, chat_id)
    except Exception:
        log.exception("get_chat_administrators failed for user=%s chat=%s", user_id, chat_id)

    return False


async def _reconcile_chat_quizzes(conn, chat_id: int) -> int | None:
    """Repair stale sessions for one group and return the one live session, if any.

    The caller must hold the per-chat PostgreSQL advisory transaction lock.
    Future-dated active rows are scheduled sessions and are preserved.
    """
    cur = await conn.execute("""SELECT * FROM group_quiz_sessions
        WHERE chat_id=%s AND status IN ('active','paused') ORDER BY id DESC""", (int(chat_id),))
    sessions = await cur.fetchall()
    live_id = None
    for session in sessions:
        sid = int(session['id'])
        if session.get('status') == 'paused':
            # Paused sessions are never allowed to remain as hidden blockers for a new quiz.
            # A fresh launch supersedes them; they must not unexpectedly resume later.
            await conn.execute("UPDATE group_quiz_sessions SET status='cancelled',completed_at=COALESCE(completed_at,NOW()) WHERE id=%s AND status='paused'", (sid,))
            continue
        total = int(session.get('questions_count') or 0)
        position = int(session.get('current_index') or 0)
        if total <= 0 or position >= total:
            await conn.execute("UPDATE group_quiz_sessions SET current_index=GREATEST(current_index,questions_count),next_at=NOW() WHERE id=%s AND status='active'", (sid,))
            # Mark terminal now; scheduler's completed/report retry path generates results.
            await conn.execute("UPDATE group_quiz_sessions SET status='completed',completed_at=COALESCE(completed_at,NOW()) WHERE id=%s AND status='active'", (sid,))
            continue
        # Walk past already-closed questions left behind by older buggy versions.
        while position < total:
            qcur = await conn.execute("SELECT status FROM group_quiz_session_questions WHERE session_id=%s AND position=%s", (sid, position))
            qrow = await qcur.fetchone()
            if qrow and qrow['status'] == 'closed':
                position += 1
                continue
            break
        if position >= total:
            await conn.execute("UPDATE group_quiz_sessions SET current_index=questions_count,status='completed',completed_at=COALESCE(completed_at,NOW()),next_at=NOW() WHERE id=%s AND status='active'", (sid,))
            continue
        if not qrow:
            # No current question means this active row can never progress; quarantine it.
            await conn.execute("UPDATE group_quiz_sessions SET status='cancelled',completed_at=COALESCE(completed_at,NOW()) WHERE id=%s AND status='active'", (sid,))
            continue
        if qrow['status'] == 'failed':
            await conn.execute("UPDATE group_quiz_sessions SET status='failed',completed_at=COALESCE(completed_at,NOW()) WHERE id=%s AND status='active'", (sid,))
            continue
        if position != int(session['current_index']):
            await conn.execute("UPDATE group_quiz_sessions SET current_index=%s,next_at=NOW() WHERE id=%s AND status='active'", (position, sid))
        if live_id is None:
            live_id = sid
        else:
            # Legacy duplicate active rows: preserve the newest live session and retire older ones.
            await conn.execute("UPDATE group_quiz_sessions SET status='cancelled',completed_at=COALESCE(completed_at,NOW()) WHERE id=%s AND status='active'", (sid,))
    return live_id


async def group_brand_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a per-group quiz brand shown on every poll and the live progress card."""
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    pool = context.application.bot_data.get("db_pool")
    if not chat or not user or not message or pool is None:
        return
    if chat.type == ChatType.PRIVATE:
        await message.reply_text("Use /quizbrand inside the group whose quiz branding you want to customise.")
        return
    if not await _require_group_membership(message, pool, chat):
        return
    if not await _is_chat_admin(context, chat.id, user.id):
        await message.reply_text("Only this group's administrators can change its quiz branding.")
        return

    value = " ".join(context.args).strip()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT brand_name FROM group_quiz_memberships WHERE chat_id=%s AND is_active=TRUE", (int(chat.id),))
        row = await cur.fetchone()
        current = (row or {}).get("brand_name") or (chat.title or "Telegram Group")
        if not value:
            await message.reply_text(
                f"🏷 Current quiz branding: {current}\n\n"
                "Set a custom label with /quizbrand Your Academy Name\n"
                "Reset to the Telegram group name with /quizbrand reset"
            )
            return
        if value.lower() == "reset":
            await conn.execute("UPDATE group_quiz_memberships SET brand_name=NULL,chat_title=%s WHERE chat_id=%s AND is_active=TRUE", (chat.title or "Telegram Group", int(chat.id)))
            new_brand = chat.title or "Telegram Group"
        else:
            new_brand = value[:48].strip()
            await conn.execute("UPDATE group_quiz_memberships SET brand_name=%s,chat_title=%s WHERE chat_id=%s AND is_active=TRUE", (new_brand, chat.title or "Telegram Group", int(chat.id)))
    await message.reply_text(
        f"✅ Quiz branding updated: {new_brand}\n\n"
        "This group's name will appear on each quiz poll and its live progress card."
    )


async def group_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    pool = context.application.bot_data.get("db_pool")
    if not chat or not user or not message or pool is None:
        return
    if chat.type == ChatType.PRIVATE:
        await message.reply_text("Use /groupquiz inside the Telegram group where you want to run the quiz.")
        return
    if not await _require_group_membership(message, pool, chat):
        return
    if not await _is_chat_admin(context, chat.id, user.id):
        await message.reply_text("Only this group's administrators can start a group quiz.")
        return
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT q.id,q.title,q.subject,COUNT(qq.question_id) AS n
            FROM bank_quizzes q JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
            JOIN question_bank b ON b.id=qq.question_id AND b.status='ready'
            WHERE q.is_published=TRUE AND (q.is_custom=FALSE OR q.custom_owner_telegram_id=%s) GROUP BY q.id ORDER BY q.id DESC LIMIT 20""", (user.id,))
        quizzes = await cur.fetchall()
    buttons = [[InlineKeyboardButton("🧩 Choose Subject + Fixed Question Range", callback_data="gq:bank")]]
    buttons += [[InlineKeyboardButton(f"📚 #{r['id']} · {r['title'][:30]} ({r['n']} Q)", callback_data=f"gq:quiz:{r['id']}")] for r in quizzes]
    buttons.append([InlineKeyboardButton("Cancel", callback_data="gq:cancel")])
    await message.reply_text(
        "🧩 **BPSC Nexus Group Quiz**\n\n"
        "First choose a subject or published quiz, then select the exact question range.\n"
        "Example: 1–50 today, 51–100 next time. The full bank will NEVER launch without a confirmed range.",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def group_leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show cumulative standings for completed quizzes in this Telegram group."""
    chat=update.effective_chat; message=update.effective_message
    pool=context.application.bot_data.get("db_pool")
    if not chat or not message or pool is None: return
    if chat.type==ChatType.PRIVATE:
        await message.reply_text("Run /grouprank inside a Telegram group to see that group's cumulative leaderboard."); return
    if not await _require_group_membership(message, pool, chat): return
    async with pool.connection() as conn:
        cur=await conn.execute("""SELECT a.telegram_id,MAX(a.first_name) AS first_name,MAX(a.username) AS username,
            COUNT(DISTINCT a.session_id) AS quizzes,
            COUNT(*) FILTER (WHERE a.selected_letter=q.correct_option) AS correct,
            COUNT(*) FILTER (WHERE a.selected_letter IS NOT NULL AND a.selected_letter<>q.correct_option) AS incorrect,
            COUNT(*) FILTER (WHERE a.selected_letter IS NOT NULL) AS attempted,
            SUM(CASE WHEN a.selected_letter=q.correct_option THEN s.positive_marks WHEN a.selected_letter IS NOT NULL THEN -s.negative_marks ELSE 0 END) AS score
            FROM group_quiz_answers a JOIN group_quiz_session_questions q USING(session_id,position)
            JOIN group_quiz_sessions s ON s.id=a.session_id
            WHERE s.chat_id=%s AND s.status='completed'
            GROUP BY a.telegram_id ORDER BY SUM(CASE WHEN a.selected_letter=q.correct_option THEN s.positive_marks WHEN a.selected_letter IS NOT NULL THEN -s.negative_marks ELSE 0 END) DESC,correct DESC,incorrect ASC LIMIT 20""",(chat.id,))
        rows=await cur.fetchall()
    if not rows:
        await message.reply_text("No completed group quizzes with recorded answers yet."); return
    lines=[f"🏆 {chat.title or 'Group'} · Cumulative Quiz Leaderboard","","Scores use each quiz's configured marking scheme."]
    for rank,r in enumerate(rows,1):
        name=(r["first_name"] or r["username"] or f"Student {r['telegram_id']}").replace("\n"," ")[:32]
        correct=int(r["correct"] or 0); incorrect=int(r["incorrect"] or 0); score=float(r["score"] or 0)
        lines.append(f"{rank}. {name} — {score:.2f} pts · {correct}✓ {incorrect}✗ · {r['quizzes']} quizzes")
    await message.reply_text("\n".join(lines)[:4000])


async def group_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show this group's active/scheduled quiz; available to all group members."""
    chat = update.effective_chat; message = update.effective_message
    pool = context.application.bot_data.get("db_pool")
    if not chat or not message or pool is None:
        return
    if chat.type == ChatType.PRIVATE:
        await message.reply_text("Run /groupstatus inside the group to see its current quiz.")
        return
    if not await _require_group_membership(message, pool, chat):
        return
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT id,quiz_title,questions_count,current_index,mode,interval_seconds,next_at,started_by,status
            FROM group_quiz_sessions WHERE chat_id=%s AND status IN ('active','paused') ORDER BY id DESC LIMIT 1""", (chat.id,))
        row = await cur.fetchone()
    if not row:
        await message.reply_text("ℹ️ There is no active or scheduled group quiz right now. A group admin can start one with /groupquiz.")
        return
    starts = row["next_at"].strftime("%Y-%m-%d %H:%M UTC") if row["next_at"] else "soon"
    await message.reply_text(
        f"🧩 Group Quiz Status · #{row['id']}\n\n{row['quiz_title']}\n"
        f"Status: {'Paused' if row.get('status') == 'paused' else 'Active'}\n"
        f"Mode: {row['mode'].title()}\nQuestions: {row['questions_count']}\n"
        f"Progress: {min(int(row['current_index']) + 1, int(row['questions_count']))}/{row['questions_count']}\n"
        f"Voting window: {row['interval_seconds']} seconds\nNext activity: {starts}"
    )


async def group_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop an active group quiz early and finalize it as completed, with analysis."""
    chat = update.effective_chat; user = update.effective_user; message = update.effective_message
    pool = context.application.bot_data.get("db_pool")
    if not chat or not user or not message or pool is None:
        return
    if chat.type == ChatType.PRIVATE:
        await message.reply_text("Run /stopquiz inside the group whose quiz you want to stop.")
        return
    if not await _require_group_membership(message, pool, chat):
        return
    if not await _is_chat_admin(context, chat.id, user.id):
        await message.reply_text("Only this group's administrators or owner can stop its quiz.")
        return
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT s.*,q.poll_id,q.message_id
            FROM group_quiz_sessions s
            LEFT JOIN group_quiz_session_questions q ON q.session_id=s.id AND q.position=s.current_index
            WHERE s.chat_id=%s AND s.status='active' ORDER BY s.id DESC LIMIT 1""", (chat.id,))
        row = await cur.fetchone()
        if row:
            if row.get("poll_id"):
                await conn.execute("UPDATE group_quiz_session_questions SET status='closed',updated_at=NOW() WHERE session_id=%s AND position=%s", (row["id"], row["current_index"]))
            # Leave the session active until _finish_session atomically marks it completed.
            # Moving the cursor to the end prevents the scheduler from publishing another question.
            await conn.execute("UPDATE group_quiz_sessions SET current_index=questions_count,next_at=NOW() WHERE id=%s AND status='active'", (row["id"],))
    if not row:
        await message.reply_text("There is no active quiz to stop.")
        return
    if row.get("message_id"):
        try:
            await context.bot.stop_poll(chat_id=chat.id, message_id=int(row["message_id"]))
        except Exception:
            log.info("Poll for stopped session %s was already closed or unavailable", row["id"])
    try:
        await _finish_session(context.application, pool, row)
    except Exception:
        log.exception("Early stop finalization failed for session %s", row["id"])
        await message.reply_text(f"⚠️ Quiz #{row['id']} has been stopped. Its final report is being retried; run /quizstatus in a moment.")
        return
    # Successful manual stop is finalized through the normal completion flow.
    # Do not post a second status message in the group; the single final notice
    # and attached group report are the only completion output.


async def group_pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause the active quiz; close the current question and resume from the next one."""
    chat=update.effective_chat; user=update.effective_user; message=update.effective_message
    pool=context.application.bot_data.get("db_pool")
    if not chat or not user or not message or pool is None: return
    if chat.type==ChatType.PRIVATE:
        await message.reply_text("Run /pausequiz inside the group."); return
    if not await _require_group_membership(message,pool,chat): return
    if not await _is_chat_admin(context,chat.id,user.id):
        await message.reply_text("Only this group's administrators or owner can pause its quiz."); return
    async with pool.connection() as conn:
        cur=await conn.execute("""SELECT s.id,s.current_index,q.message_id,q.status AS question_status
            FROM group_quiz_sessions s LEFT JOIN group_quiz_session_questions q
            ON q.session_id=s.id AND q.position=s.current_index
            WHERE s.chat_id=%s AND s.status='active' ORDER BY s.id DESC LIMIT 1""",(chat.id,))
        row=await cur.fetchone()
        if row:
            if row.get("message_id"):
                await conn.execute("UPDATE group_quiz_session_questions SET status='closed',updated_at=NOW() WHERE session_id=%s AND position=%s AND status='open'",(row['id'],row['current_index']))
            # A Telegram poll cannot be reopened. Resume from the next question to avoid reposting the same question.
            next_index=int(row['current_index']) + (1 if row.get('question_status') in ('open','posting') else 0)
            await conn.execute("UPDATE group_quiz_sessions SET status='paused',current_index=%s,next_at=NOW() WHERE id=%s AND status='active'",(next_index,row['id']))
    if not row:
        await message.reply_text("There is no active quiz to pause."); return
    if row.get('message_id'):
        try: await context.bot.stop_poll(chat_id=chat.id,message_id=int(row['message_id']))
        except Exception: log.info("Poll already closed during pause for session %s",row['id'])
    await message.reply_text(f"⏸️ Quiz #{row['id']} paused. The current question is closed; /resumequiz will continue from the next question.")


async def group_resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume a paused group quiz from its persisted next question."""
    chat=update.effective_chat; user=update.effective_user; message=update.effective_message
    pool=context.application.bot_data.get("db_pool")
    if not chat or not user or not message or pool is None: return
    if chat.type==ChatType.PRIVATE:
        await message.reply_text("Run /resumequiz inside the group."); return
    if not await _require_group_membership(message,pool,chat): return
    if not await _is_chat_admin(context,chat.id,user.id):
        await message.reply_text("Only this group's administrators or owner can resume its quiz."); return
    # Do not resume a session whose current poll has an unknown send outcome. Telegram
    # provides no API to list a group's messages and recover that poll's ID after a timeout.
    async with pool.connection() as conn:
        cur=await conn.execute("""SELECT s.id,s.current_index,q.status AS question_status,q.poll_id
            FROM group_quiz_sessions s LEFT JOIN group_quiz_session_questions q
              ON q.session_id=s.id AND q.position=s.current_index
            WHERE s.chat_id=%s AND s.status='paused' ORDER BY s.id DESC LIMIT 1""",(chat.id,))
        paused=await cur.fetchone()
    if paused and paused.get("question_status")=="posting" and not paused.get("poll_id"):
        await message.reply_text(
            f"⚠️ Quiz #{paused['id']} cannot safely resume because Telegram's poll-send result was not confirmed. "
            "Please check the group for that question. To avoid duplicate polls, start a new quiz session; "
            "the new launch will retire this paused session."
        )
        return
    async with pool.connection() as conn:
        cur=await conn.execute("""UPDATE group_quiz_sessions SET status='active',next_at=NOW()
            WHERE id=(SELECT id FROM group_quiz_sessions WHERE chat_id=%s AND status='paused' ORDER BY id DESC LIMIT 1)
            AND status='paused' RETURNING id,current_index,questions_count""",(chat.id,))
        row=await cur.fetchone()
    if not row:
        await message.reply_text("There is no paused quiz to resume."); return
    if int(row['current_index'])>=int(row['questions_count']):
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT * FROM group_quiz_sessions WHERE id=%s",(row['id'],)); session=await cur.fetchone()
        await _finish_session(context.application,pool,session)
        await message.reply_text(f"🏁 Quiz #{row['id']} had reached the end and has been finalized.")
        return
    await message.reply_text(f"▶️ Quiz #{row['id']} resumed from question {int(row['current_index'])+1}/{row['questions_count']}.")


async def group_quiz_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Schedule a group quiz in UTC: /groupquiz_schedule <quiz_id> <YYYY-MM-DDTHH:MMZ> [15|30|60|120|300] [learning|exam]."""
    chat=update.effective_chat; user=update.effective_user; message=update.effective_message
    pool=context.application.bot_data.get("db_pool")
    if not chat or not user or not message or pool is None: return
    if chat.type==ChatType.PRIVATE:
        await message.reply_text("Run this command in the destination group. Syntax: /groupquiz_schedule 12 2026-10-12T18:00Z 30 exam (time is UTC).")
        return
    if not await _require_group_membership(message, pool, chat):
        return
    if not await _is_chat_admin(context,chat.id,user.id):
        await message.reply_text("Only this group's administrators can schedule a quiz."); return
    if len(context.args)<2:
        await message.reply_text("Syntax: /groupquiz_schedule <quiz_id> <YYYY-MM-DDTHH:MMZ> [15|30|60|120|300] [learning|exam]\nExample: /groupquiz_schedule 12 2026-10-12T18:00Z 30 exam\nAll scheduled times are UTC."); return
    try:
        quiz_id=int(context.args[0]); when_text=context.args[1].replace("Z","+00:00")
        when=datetime.fromisoformat(when_text)
        if when.tzinfo is None: raise ValueError("Include timezone Z; schedule times are UTC.")
        when=when.astimezone(timezone.utc)
        interval=int(context.args[2]) if len(context.args)>2 else 30
        mode=context.args[3].lower() if len(context.args)>3 else "learning"
        if interval not in (15,30,60,120,300): raise ValueError("Voting window must be 15, 30, 60, 120 or 300 seconds.")
        if mode not in ("learning","exam"): raise ValueError("Mode must be learning or exam.")
        if when < datetime.now(timezone.utc)+timedelta(minutes=2): raise ValueError("Schedule the quiz at least 2 minutes in the future.")
    except (ValueError,TypeError) as exc:
        await message.reply_text(f"Invalid schedule: {exc}\nUse UTC, e.g. /groupquiz_schedule 12 2026-10-12T18:00Z 30 exam"); return
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT id,title FROM bank_quizzes WHERE id=%s AND is_published=TRUE AND (is_custom=FALSE OR custom_owner_telegram_id=%s)",(quiz_id,user.id)); quiz=await cur.fetchone()
        if not quiz:
            await message.reply_text("Published quiz not found."); return
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 741923))", (f"bpsc_nexus_group_quiz:{int(chat.id)}",))
        active_id = await _reconcile_chat_quizzes(conn, chat.id)
        if active_id is not None:
            await message.reply_text(f"This group already has live or scheduled quiz session #{active_id}. Finish it before scheduling another."); return
        cur=await conn.execute("""SELECT b.id,b.question_en,b.question_hi,b.options,b.correct_option,b.explanation_en,b.explanation_hi
            FROM bank_quiz_questions qq JOIN question_bank b ON b.id=qq.question_id WHERE qq.quiz_id=%s AND b.status='ready' ORDER BY qq.position""",(quiz_id,))
        source=await cur.fetchall(); prepared=[]
        for row in source:
            opts=row["options"] if isinstance(row["options"],dict) else json.loads(row["options"] or "{}")
            letters=[letter for letter in "ABCD" if str(opts.get(letter,"")).strip()]
            if len(letters)>=2 and row["correct_option"] in letters: prepared.append((row,opts,letters))
        if not prepared:
            await message.reply_text("This quiz has no questions valid for Telegram polls."); return
        cur=await conn.execute("SELECT brand_name FROM group_quiz_memberships WHERE chat_id=%s", (int(chat.id),))
        brand_row=await cur.fetchone()
        brand_name=(brand_row or {}).get("brand_name")
        cur=await conn.execute("""INSERT INTO group_quiz_sessions(chat_id,chat_title,brand_name,quiz_id,quiz_title,started_by,interval_seconds,questions_count,mode,current_index,status,next_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'active',%s) RETURNING id""",(chat.id,chat.title or "Telegram group",brand_name,quiz_id,quiz["title"],user.id,interval,len(prepared),mode,when))
        session_id=int((await cur.fetchone())["id"])
        for position,(row,opts,letters) in enumerate(prepared):
            await conn.execute("""INSERT INTO group_quiz_session_questions(session_id,position,question_id,question_en,question_hi,options,option_letters,correct_option,explanation_en,explanation_hi)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)""",(session_id,position,row["id"],row["question_en"],row["question_hi"],json.dumps(opts,ensure_ascii=False),json.dumps(letters),row["correct_option"],row["explanation_en"],row["explanation_hi"]))
    await message.reply_text(f"📅 Quiz scheduled\n\n#{session_id} · {quiz['title']}\nStarts: {when.isoformat()} (UTC)\nMode: {mode.title()}\nQuestions: {len(prepared)}\nVoting window: {interval}s per question")


async def _create_range_quiz(pool, pending, user, startpos: int, endpos: int):
    """Snapshot a fixed 1-based range from a subject pool or published quiz into a private quiz."""
    count=endpos-startpos+1
    if count < 1 or count > 500:
        return None
    async with pool.connection() as conn:
        if pending.get("source_type")=="published_range":
            cur=await conn.execute("""SELECT b.id FROM bank_quiz_questions qq
                JOIN question_bank b ON b.id=qq.question_id AND b.status='ready'
                WHERE qq.quiz_id=%s ORDER BY qq.position OFFSET %s LIMIT %s""",
                (int(pending["quiz_id"]),startpos-1,count))
        elif pending.get("subject"):
            cur=await conn.execute("SELECT id FROM question_bank WHERE status='ready' AND subject=%s ORDER BY id OFFSET %s LIMIT %s",
                (pending["subject"],startpos-1,count))
        else:
            cur=await conn.execute("SELECT id FROM question_bank WHERE status='ready' ORDER BY id OFFSET %s LIMIT %s",
                (startpos-1,count))
        ids=[int(r["id"]) for r in await cur.fetchall()]
        if len(ids)!=count:
            return None
        # Snapshot the exact range first; randomize only its order.
        random.shuffle(ids)
        label=str(pending.get("subject_label") or "Question Bank")
        title=f"Group · {label[:80]} · Q{startpos}-{endpos} · {datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        cur=await conn.execute("""INSERT INTO bank_quizzes(title,description,subject,created_by_telegram_id,is_published,is_custom,custom_owner_telegram_id)
            VALUES(%s,%s,%s,%s,FALSE,TRUE,%s) RETURNING id""",
            (title,f"Fixed question range {startpos}-{endpos}; shuffled within selected range",label,user.id,user.id))
        quiz_id=int((await cur.fetchone())["id"])
        # Batch the fixed-range snapshot to avoid one DB round-trip per question.
        async with conn.cursor() as insert_cur:
            await insert_cur.executemany(
                "INSERT INTO bank_quiz_questions(quiz_id,position,question_id) VALUES(%s,%s,%s)",
                [(quiz_id, pos, qid) for pos, qid in enumerate(ids, 1)],
            )
    return {"quiz_id":quiz_id,"source_type":"range","generated_title":title,"count":count,
            "range_start":startpos,"range_end":endpos,"range_label":f"{startpos}–{endpos}"}


async def route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, pool) -> bool:
    q = update.callback_query
    if not q or not (q.data or "").startswith("gq:"):
        return False
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type == ChatType.PRIVATE:
        await q.answer("Use group quiz controls inside the destination group.", show_alert=True)
        return True
    if not await _group_is_authorized(pool, chat.id):
        await q.answer("This group does not have BPSC Nexus Quiz membership.", show_alert=True)
        return True
    if not await _is_chat_admin(context, chat.id, user.id):
        await q.answer("Only this group's administrators can control its quiz.", show_alert=True)
        return True
    data = q.data
    if data == "gq:bank":
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT subject,COUNT(*) AS n FROM question_bank WHERE status='ready' GROUP BY subject ORDER BY subject")
            rows=await cur.fetchall()
        if not rows:
            await q.answer("No verified questions in the bank yet.",show_alert=True); return True
        subjects=[{"name":r["subject"],"n":int(r["n"])} for r in rows]
        context.user_data["gq_subject_options"]=subjects
        buttons=[[InlineKeyboardButton(f"🌐 Mixed subjects ({sum(x['n'] for x in subjects)})",callback_data="gq:bank_subject:all")]]
        buttons += [[InlineKeyboardButton(f"{x['name'][:36]} ({x['n']})",callback_data=f"gq:bank_subject:{i}")] for i,x in enumerate(subjects)]
        buttons.append([InlineKeyboardButton("Cancel",callback_data="gq:cancel")])
        await q.answer(); await q.edit_message_text("Choose the question pool for this group quiz:",reply_markup=InlineKeyboardMarkup(buttons)); return True
    if data.startswith("gq:bank_subject:"):
        value=data.rsplit(":",1)[1]
        subjects=context.user_data.get("gq_subject_options",[])
        if value=="all": subject=None; label="Mixed Subjects"
        else:
            try: selected=subjects[int(value)]; subject=selected["name"]; label=subject
            except (ValueError,IndexError): await q.answer("Subject selection expired. Run /groupquiz again.",show_alert=True); return True
        eligible=sum(x["n"] for x in subjects) if subject is None else next((x["n"] for x in subjects if x["name"]==subject),0)
        context.user_data["group_quiz_pending"]={"source_type":"bank","subject":subject,"subject_label":label,"chat_id":chat.id,"chat_title":chat.title or "Telegram group","stage":"choose_range","eligible":eligible}
        buttons=[]
        for startpos in range(1, eligible+1, 50):
            endpos=min(startpos+49,eligible)
            buttons.append([InlineKeyboardButton(f"Questions {startpos}–{endpos} ({endpos-startpos+1})",callback_data=f"gq:range:{startpos}:{endpos}")])
        buttons.append([InlineKeyboardButton("✍️ Custom range…",callback_data="gq:range_custom")])
        buttons.append([InlineKeyboardButton("Cancel",callback_data="gq:cancel")])
        await q.answer(); await q.edit_message_text(f"Question pool: {label}\nEligible questions: {eligible}\n\nChoose exactly which questions to use. Each button selects a fixed 50-question block (last block may be shorter). You can also enter a custom range. No questions will start until you confirm the range.",reply_markup=InlineKeyboardMarkup(buttons)); return True
    if data == "gq:range_custom":
        pending=context.user_data.get("group_quiz_pending")
        if not pending or pending.get("chat_id")!=chat.id:
            await q.answer("Setup expired. Run /groupquiz again.",show_alert=True); return True
        pending["stage"]="range_start"; context.user_data["group_quiz_pending"]=pending
        await q.answer(); await q.edit_message_text(f"Enter the START question position (1–{int(pending.get('eligible',0))}). Example: 1",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data="gq:cancel")]])); return True
    if data.startswith("gq:range:"):
        pending=context.user_data.get("group_quiz_pending")
        if not pending or pending.get("chat_id")!=chat.id:
            await q.answer("Setup expired. Run /groupquiz again.",show_alert=True); return True
        try:
            parts=data.split(":")
            startpos,endpos=int(parts[2]),int(parts[3])
        except (ValueError,IndexError):
            await q.answer("Invalid range. Choose it again.",show_alert=True); return True
        eligible=int(pending.get("eligible",0))
        if not (1<=startpos<=endpos<=eligible) or endpos-startpos+1>500:
            await q.answer("Range is invalid or exceeds the 500-question limit.",show_alert=True); return True
        created=await _create_range_quiz(pool,pending,user,startpos,endpos)
        if not created:
            await q.answer("Could not create that range. Check the eligible question count and try again.",show_alert=True); return True
        pending.update(created); pending["stage"]=None; context.user_data["group_quiz_pending"]=pending
        await q.answer("Fixed range selected.")
        await q.edit_message_text(f"✅ Fixed range confirmed: questions {startpos}–{endpos} ({endpos-startpos+1} questions).\nThe questions will be shuffled only within this selected range.\n\nChoose quiz mode:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧠 Learning Mode",callback_data="gq:mode:learning")],[InlineKeyboardButton("📝 Exam Mode",callback_data="gq:mode:exam")],[InlineKeyboardButton("Cancel",callback_data="gq:cancel")]])); return True
    if data.startswith("gq:quiz:"):
        quiz_id = int(data.rsplit(":", 1)[1])
        async with pool.connection() as conn:
            cur = await conn.execute("""SELECT q.id,q.title,q.subject,COUNT(qq.question_id) AS n
                FROM bank_quizzes q JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
                JOIN question_bank b ON b.id=qq.question_id AND b.status='ready'
                WHERE q.id=%s AND q.is_published=TRUE GROUP BY q.id""", (quiz_id,))
            quiz = await cur.fetchone()
        if not quiz:
            await q.answer("Quiz is not published or has no eligible questions.", show_alert=True)
            return True
        eligible=int(quiz["n"])
        pending={"quiz_id":quiz_id,"source_type":"published_range","subject_label":quiz["title"],"subject":None,
                 "chat_id":chat.id,"chat_title":chat.title or "Telegram group","stage":"choose_range","eligible":eligible}
        context.user_data["group_quiz_pending"]=pending
        buttons=[]
        for startpos in range(1, eligible+1, 50):
            endpos=min(startpos+49,eligible)
            buttons.append([InlineKeyboardButton(f"Questions {startpos}–{endpos} ({endpos-startpos+1})",callback_data=f"gq:range:{startpos}:{endpos}")])
        buttons.append([InlineKeyboardButton("✍️ Custom range…",callback_data="gq:range_custom")])
        buttons.append([InlineKeyboardButton("Cancel",callback_data="gq:cancel")])
        await q.answer()
        await q.edit_message_text(f"Selected published quiz: {quiz['title']}\nEligible questions: {eligible}\n\nChoose the exact range to use. The full quiz will not start automatically.",reply_markup=InlineKeyboardMarkup(buttons))
        return True
    if data.startswith("gq:mode:"):
        mode=data.rsplit(":",1)[1]
        pending=context.user_data.get("group_quiz_pending")
        if not pending or pending.get("chat_id")!=chat.id or mode not in ("learning","exam"):
            await q.answer("Setup expired. Run /groupquiz again.",show_alert=True); return True
        pending["mode"]=mode
        context.user_data["group_quiz_pending"]=pending
        await q.answer()
        mode_text="Learning Mode — answer and explanation after each poll" if mode=="learning" else "Exam Mode — answers hidden until final submission"
        await q.edit_message_text(f"Mode: {mode_text}\n\nChoose time per question.",reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("15 sec",callback_data="gq:interval:15"),InlineKeyboardButton("20 sec",callback_data="gq:interval:20")],
            [InlineKeyboardButton("25 sec",callback_data="gq:interval:25"),InlineKeyboardButton("30 sec",callback_data="gq:interval:30")],
            [InlineKeyboardButton("Custom time…",callback_data="gq:interval:custom")],
            [InlineKeyboardButton("Cancel",callback_data="gq:cancel")],
        ])); return True
    if data.startswith("gq:report:"):
        await q.answer("This report button has been replaced. Use the My Analysis button under the quiz completion message.", show_alert=True)
        return True
    if data == "gq:cancel":
        context.user_data.pop("group_quiz_pending", None)
        await q.answer("Cancelled.")
        await q.edit_message_text("Group quiz setup cancelled.")
        return True
    if data.startswith("gq:interval:"):
        raw=data.rsplit(":",1)[1]; pending=context.user_data.get("group_quiz_pending")
        if not pending or pending.get("chat_id")!=chat.id:
            await q.answer("Setup expired. Run /groupquiz again.",show_alert=True); return True
        if raw=="custom":
            pending["stage"]="custom_timer"; context.user_data["group_quiz_pending"]=pending
            await q.answer(); await q.edit_message_text("Enter custom time per question in whole seconds (5–600).",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data="gq:cancel")]])); return True
        try: interval=int(raw)
        except ValueError: await q.answer("Invalid duration.",show_alert=True); return True
        if interval not in (15,20,25,30): await q.answer("Choose 15, 20, 25 or 30 seconds, or Custom.",show_alert=True); return True
        pending["interval_seconds"]=interval; pending["stage"]="positive_marks"; context.user_data["group_quiz_pending"]=pending
        await q.answer(); await q.edit_message_text(f"Time per question: {interval}s. Choose marks for a correct answer.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("+1",callback_data="gq:positive:1"),InlineKeyboardButton("+2",callback_data="gq:positive:2"),InlineKeyboardButton("+3",callback_data="gq:positive:3")],[InlineKeyboardButton("Custom…",callback_data="gq:positive:custom")],[InlineKeyboardButton("Cancel",callback_data="gq:cancel")]])); return True
    if data.startswith("gq:positive:"):
        pending=context.user_data.get("group_quiz_pending")
        if not pending or pending.get("chat_id")!=chat.id: await q.answer("Setup expired.",show_alert=True); return True
        raw=data.rsplit(":",1)[1]
        if raw=="custom":
            pending["stage"]="custom_positive"; context.user_data["group_quiz_pending"]=pending
            await q.answer(); await q.edit_message_text("Enter marks per correct answer (0.01–100).",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data="gq:cancel")]])); return True
        pending["positive_marks"]=float(raw); pending["stage"]="negative_marks"; context.user_data["group_quiz_pending"]=pending
        await q.answer(); await q.edit_message_text("Choose the negative-mark penalty per wrong answer.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("0",callback_data="gq:negative:0"),InlineKeyboardButton("−0.25",callback_data="gq:negative:0.25")],[InlineKeyboardButton("−0.33",callback_data="gq:negative:0.33"),InlineKeyboardButton("−0.50",callback_data="gq:negative:0.5")],[InlineKeyboardButton("−1",callback_data="gq:negative:1"),InlineKeyboardButton("Custom…",callback_data="gq:negative:custom")],[InlineKeyboardButton("Cancel",callback_data="gq:cancel")]])); return True
    if data.startswith("gq:negative:"):
        pending=context.user_data.get("group_quiz_pending")
        if not pending or pending.get("chat_id")!=chat.id: await q.answer("Setup expired.",show_alert=True); return True
        raw=data.rsplit(":",1)[1]
        if raw=="custom":
            pending["stage"]="custom_negative"; context.user_data["group_quiz_pending"]=pending
            await q.answer(); await q.edit_message_text("Enter the negative-mark penalty magnitude (0–100). Enter 0 for no negative marking.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data="gq:cancel")]])); return True
        pending["negative_marks"]=float(raw); pending["stage"]="confirm_launch"; context.user_data["group_quiz_pending"]=pending
        await q.answer(); await q.edit_message_text(_pending_preview(pending),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Confirm & Start Quiz",callback_data="gq:launch")],[InlineKeyboardButton("Cancel",callback_data="gq:cancel")]])); return True
    if data=="gq:launch":
        pending=context.user_data.get("group_quiz_pending")
        if not pending or pending.get("chat_id")!=chat.id or pending.get("stage")!="confirm_launch":
            await q.answer("Setup expired. Run /groupquiz again.",show_alert=True); return True
        return await _launch_pending_quiz(q,context,pool,pending,user)
    return True


def _pending_preview(pending):
    range_label=(f"{pending.get('range_start')}–{pending.get('range_end')}" if pending.get('range_start') is not None else (pending.get("range_label") or "Not set"))
    count_label=str(pending.get('count')) if pending.get('count') is not None else "From selected quiz"
    return ("🧾 FINAL QUIZ PREVIEW\n\n"
        f"Subject / quiz: {pending.get('subject_label') or pending.get('generated_title') or 'Selected quiz'}\n"
        f"Question range: {range_label}\n"
        f"Questions: {count_label}\n"
        f"Mode: {str(pending.get('mode','learning')).title()}\n"
        f"Time per question: {pending.get('interval_seconds',20)} seconds\n"
        f"Scoring: +{float(pending.get('positive_marks',1)):g} correct / −{float(pending.get('negative_marks',0.333)):g} wrong\n"
        "Question order: shuffled\n\nConfirm to start. The timer begins when each poll is published.")


async def _launch_pending_quiz(q,context,pool,pending,user):
    chat_id=int(pending["chat_id"]); interval=int(pending.get("interval_seconds",20)); mode=pending.get("mode","learning")
    positive=float(pending.get("positive_marks",1)); negative=float(pending.get("negative_marks",0.333))
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT id,title FROM bank_quizzes WHERE id=%s AND (is_published=TRUE OR (is_custom=TRUE AND custom_owner_telegram_id=%s))",(pending["quiz_id"],user.id)); quiz=await cur.fetchone()
        if not quiz: await q.answer("Quiz is no longer available.",show_alert=True); return True
        cur=await conn.execute("SELECT b.id,b.question_en,b.question_hi,b.options,b.correct_option,b.explanation_en,b.explanation_hi FROM bank_quiz_questions qq JOIN question_bank b ON b.id=qq.question_id WHERE qq.quiz_id=%s AND b.status='ready' ORDER BY qq.position",(quiz["id"],)); source=await cur.fetchall()
        prepared=[]
        for row in source:
            opts=row["options"] if isinstance(row["options"],dict) else json.loads(row["options"] or "{}")
            letters=[letter for letter in "ABCDE" if str(opts.get(letter,"")).strip() and not (letter=="E" and (str(opts.get(letter,"")).lower().startswith("not attempted") or str(opts.get(letter,"")).startswith("प्रयास नहीं किया")))]
            if len(letters)>=2 and row["correct_option"] in letters: prepared.append((row,opts,letters))
        if not prepared: await q.answer("No valid questions available for Telegram polls.",show_alert=True); return True
        expected_count=pending.get("count")
        if pending.get("source_type")!="range" or expected_count is None:
            await q.answer("A fixed question range must be selected before launch. Choose a range first.",show_alert=True); return True
        if len(prepared)!=int(expected_count):
            await q.answer(f"That fixed range contains {int(expected_count)-len(prepared)} question(s) that cannot be displayed as Telegram polls. Choose a different range so no question is silently omitted.",show_alert=True); return True
        random.shuffle(prepared)
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 741923))", (f"bpsc_nexus_group_quiz:{int(chat_id)}",))
        active_id = await _reconcile_chat_quizzes(conn, chat_id)
        if active_id is not None:
            await q.answer(f"This group already has live or scheduled quiz session #{active_id}. Finish it first.",show_alert=True); return True
        cur=await conn.execute("SELECT brand_name FROM group_quiz_memberships WHERE chat_id=%s", (chat_id,))
        brand_row=await cur.fetchone()
        brand_name=(brand_row or {}).get("brand_name")
        cur=await conn.execute("INSERT INTO group_quiz_sessions(chat_id,chat_title,brand_name,quiz_id,quiz_title,started_by,interval_seconds,questions_count,mode,current_index,status,next_at,positive_marks,negative_marks,started_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'active',NOW(),%s,%s,NOW()) RETURNING id",(chat_id,pending.get("chat_title","Telegram group"),brand_name,quiz["id"],quiz["title"],user.id,interval,len(prepared),mode,positive,negative)); sid=int((await cur.fetchone())["id"])
        # Batch snapshot all questions in one database operation; large quizzes no longer
        # incur hundreds of serial insert round-trips before the first poll can appear.
        async with conn.cursor() as insert_cur:
            await insert_cur.executemany(
                "INSERT INTO group_quiz_session_questions(session_id,position,question_id,question_en,question_hi,options,option_letters,correct_option,explanation_en,explanation_hi) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
                [
                    (sid, position, row["id"], row["question_en"], row["question_hi"],
                     json.dumps(opts, ensure_ascii=False), json.dumps(letters),
                     row["correct_option"], row["explanation_en"], row["explanation_hi"])
                    for position, (row, opts, letters) in enumerate(prepared)
                ],
            )
    context.user_data.pop("group_quiz_pending",None)
    await q.answer("Quiz launched.")
    range_text=f"\n𝗤𝗨𝗘𝗦𝗧𝗜𝗢𝗡 𝗥𝗔𝗡𝗚𝗘  {pending.get('range_start','?')}–{pending.get('range_end','?')}" if pending.get("range_start") is not None else ""
    confirmation = (
        f"✅ 𝗕𝗣𝗦𝗖 𝗡𝗘𝗫𝗨𝗦  ·  𝗤𝗨𝗜𝗭 #{sid}\n\n"
        f"𝗤𝗨𝗜𝗭 𝗟𝗜𝗩𝗘\n{quiz['title']}{range_text}\n\n"
        f"𝗤𝗨𝗘𝗦𝗧𝗜𝗢𝗡𝗦   {len(prepared)}\n"
        f"𝗠𝗢𝗗𝗘   {mode.title()}\n"
        f"𝗧𝗜𝗠𝗘   {interval}s / question\n"
        f"𝗦𝗖𝗢𝗥𝗜𝗡𝗚   +{positive:g} correct  /  −{negative:g} wrong\n\n"
        "Stay focused. Your first question is on its way."
    )
    await q.edit_message_text(confirmation)
    # Publish Q1 immediately instead of waiting for the scheduler's next polling tick.
    # _post_question atomically claims the pending row, so the scheduler cannot duplicate it.
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM group_quiz_sessions WHERE id=%s", (sid,))
            live_session = await cur.fetchone()
            cur = await conn.execute("SELECT * FROM group_quiz_session_questions WHERE session_id=%s AND position=0", (sid,))
            first_question = await cur.fetchone()
        if live_session and first_question:
            await _post_question(context.application, pool, live_session, first_question)
    except Exception:
        # Persisted scheduler will retry; don't lose a valid session because immediate
        # first-poll delivery failed transiently.
        log.exception("Immediate first-question delivery failed for group quiz session %s", sid)
    return True


async def group_quiz_text_input(update: Update,context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume group-quiz setup text only; all other messages remain untouched."""
    message=update.effective_message; chat=update.effective_chat; user=update.effective_user; pending=context.user_data.get("group_quiz_pending")
    if not message or not message.text or not chat or not user or not pending or int(pending.get("chat_id",0))!=chat.id: return False
    stage=pending.get("stage")
    if stage not in {"range_start","range_end","custom_timer","custom_positive","custom_negative"}: return False
    pool=context.application.bot_data.get("db_pool")
    if pool is None or chat.type==ChatType.PRIVATE or not await _is_chat_admin(context,chat.id,user.id): return False
    raw=message.text.strip()
    if stage in {"range_start","range_end"}:
        try: value=int(raw)
        except ValueError: await message.reply_text("Enter a whole-number question position."); return True
        eligible=int(pending.get("eligible",0))
        if not 1<=value<=eligible: await message.reply_text(f"Enter a position from 1 to {eligible}."); return True
        if stage=="range_start":
            pending["range_start"]=value; pending["stage"]="range_end"; context.user_data["group_quiz_pending"]=pending
            await message.reply_text(f"Start position: {value}. Enter the END position ({value}–{eligible})."); return True
        startpos=int(pending["range_start"])
        if value<startpos: await message.reply_text(f"End position must be {startpos} or higher."); return True
        count=value-startpos+1
        if count>500: await message.reply_text("Maximum is 500 questions per group quiz. Choose a smaller range."); return True
        created=await _create_range_quiz(pool,pending,user,startpos,value)
        if not created:
            await message.reply_text(f"Could not find all {count} eligible questions in that range. Please choose a valid range."); return True
        pending.update(created); pending["stage"]=None; context.user_data["group_quiz_pending"]=pending
        await message.reply_text(f"✅ Fixed range confirmed: questions {startpos}–{value} ({count} questions).\nOnly these questions will be used; order is shuffled within this range.\n\nChoose mode:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧠 Learning Mode",callback_data="gq:mode:learning")],[InlineKeyboardButton("📝 Exam Mode",callback_data="gq:mode:exam")],[InlineKeyboardButton("Cancel",callback_data="gq:cancel")]])); return True
    try: value=float(raw)
    except ValueError: await message.reply_text("Enter a valid number."); return True
    if stage=="custom_timer":
        if not value.is_integer() or not 5<=value<=600: await message.reply_text("Enter whole seconds from 5 to 600."); return True
        pending["interval_seconds"]=int(value); pending["stage"]="positive_marks"; context.user_data["group_quiz_pending"]=pending
        await message.reply_text("Choose correct-answer marks:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("+1",callback_data="gq:positive:1"),InlineKeyboardButton("+2",callback_data="gq:positive:2"),InlineKeyboardButton("+3",callback_data="gq:positive:3")],[InlineKeyboardButton("Custom…",callback_data="gq:positive:custom")]])); return True
    if stage=="custom_positive":
        if not 0.01<=value<=100: await message.reply_text("Enter marks from 0.01 to 100."); return True
        pending["positive_marks"]=value; pending["stage"]="negative_marks"; context.user_data["group_quiz_pending"]=pending
        await message.reply_text("Choose wrong-answer penalty:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("0",callback_data="gq:negative:0"),InlineKeyboardButton("−0.25",callback_data="gq:negative:0.25")],[InlineKeyboardButton("−0.33",callback_data="gq:negative:0.33"),InlineKeyboardButton("−0.50",callback_data="gq:negative:0.5")],[InlineKeyboardButton("−1",callback_data="gq:negative:1"),InlineKeyboardButton("Custom…",callback_data="gq:negative:custom")]])); return True
    if stage=="custom_negative":
        if not 0<=value<=100: await message.reply_text("Enter a penalty magnitude from 0 to 100."); return True
        pending["negative_marks"]=value; pending["stage"]="confirm_launch"; context.user_data["group_quiz_pending"]=pending
        await message.reply_text(_pending_preview(pending),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Confirm & Start Quiz",callback_data="gq:launch")],[InlineKeyboardButton("Cancel",callback_data="gq:cancel")]])); return True
    return True


async def _update_live_progress(application, pool, session_id: int, position: int, *, create_if_missing: bool = False, force: bool = False, completed: bool = False):
    """Create/edit one persistent progress card; never send a new card per question."""
    sid = int(session_id)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT chat_id,chat_title,brand_name,questions_count,interval_seconds,progress_message_id,status FROM group_quiz_sessions WHERE id=%s", (sid,))
            session = await cur.fetchone()
            if not session:
                return
            cur = await conn.execute("SELECT COUNT(DISTINCT telegram_id) AS participants FROM group_quiz_answers WHERE session_id=%s", (sid,))
            participants_row = await cur.fetchone()
            cur = await conn.execute("SELECT COUNT(DISTINCT telegram_id) AS responses FROM group_quiz_answers WHERE session_id=%s AND position=%s AND selected_letter IS NOT NULL", (sid, int(position)))
            responses_row = await cur.fetchone()
        total = max(1, int(session["questions_count"] or 1))
        done = total if completed else min(total, max(0, int(position)) + 1)
        pct = 100 if completed else int((done / total) * 100)
        brand = str(session.get("brand_name") or session.get("chat_title") or "Telegram Group").strip()[:48]
        participants = int((participants_row or {}).get("participants") or 0)
        responses = int((responses_row or {}).get("responses") or 0)
        if completed:
            text = f"🏁 {brand} · QUIZ CLOSED\n\n✅ The quiz session has ended.\n📊 Final results and the detailed report are available in the report attachment."
        else:
            text = (
                f"📊 {brand} · LIVE QUIZ\n"
                f"Q{done}/{total} · {pct}% complete\n"
                f"👥 Participants: {participants} · Responses this question: {responses}\n"
                f"⏳ {int(session['interval_seconds'])} seconds per question"
            )
        message_id = session.get("progress_message_id")
        if not message_id and not create_if_missing:
            return
        # Poll answers can arrive in bursts. Coalesce progress edits to avoid Telegram flood limits.
        last_edits = application.bot_data.setdefault("gq_progress_last_edit", {})
        now = asyncio.get_running_loop().time()
        if not force and sid in last_edits and now - last_edits[sid] < 2.0:
            return
        if message_id:
            try:
                await application.bot.edit_message_text(chat_id=int(session["chat_id"]), message_id=int(message_id), text=text)
                last_edits[sid] = now
                return
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    last_edits[sid] = now
                    return
                log.debug("Progress message edit failed for session %s; will attempt recovery", sid, exc_info=True)
                if not create_if_missing:
                    return
        sent = await application.bot.send_message(chat_id=int(session["chat_id"]), text=text)
        async with pool.connection() as conn:
            await conn.execute("UPDATE group_quiz_sessions SET progress_message_id=COALESCE(progress_message_id,%s) WHERE id=%s", (int(sent.message_id), sid))
            cur = await conn.execute("SELECT progress_message_id FROM group_quiz_sessions WHERE id=%s", (sid,))
            actual = await cur.fetchone()
        if actual and int(actual["progress_message_id"]) != int(sent.message_id):
            try:
                await application.bot.delete_message(chat_id=int(session["chat_id"]), message_id=int(sent.message_id))
            except Exception:
                pass
        last_edits[sid] = now
    except Exception:
        # Progress is a convenience feature; it must never interrupt question delivery/scoring.
        log.warning("Live progress update failed for quiz session %s", sid, exc_info=True)


async def _send_poll_with_rate_limit_retry(bot, **kwargs):
    """Retry only explicit Telegram rate-limit responses, which are safe to retry.

    A timeout/network failure is intentionally NOT retried here: Telegram may have
    accepted the poll even if the response never reached this process.
    """
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            return await bot.send_poll(**kwargs)
        except RetryAfter as exc:
            if attempt >= max_retries:
                raise
            delay = getattr(exc, "retry_after", 1)
            try:
                delay = float(delay.total_seconds()) if hasattr(delay, "total_seconds") else float(delay)
            except (TypeError, ValueError):
                delay = 1.0
            # Retry only when Telegram's requested wait is short enough to avoid
            # blocking the sequential scheduler for an excessive period.
            if delay > 10:
                raise
            await asyncio.sleep(max(1.0, delay))


async def _pause_uncertain_poll_delivery(application, pool, session, sid: int, position: int, exc):
    """Pause safely when Telegram's poll-send outcome is unknown; never blindly duplicate."""
    chat_id = int(session["chat_id"])
    async with pool.connection() as conn:
        # Keep status='posting' as a durable uncertain-delivery marker. The scheduler
        # must not treat it as pending and resend the same question automatically.
        await conn.execute(
            "UPDATE group_quiz_session_questions SET status='posting',updated_at=NOW() "
            "WHERE session_id=%s AND position=%s AND status IN ('posting','pending')",
            (sid, position),
        )
        await conn.execute(
            "UPDATE group_quiz_sessions SET status='paused',next_at=NOW() "
            "WHERE id=%s AND status='active'",
            (sid,),
        )
    log.error(
        "Poll delivery outcome uncertain; paused session=%s position=%s error=%s",
        sid, position, type(exc).__name__, exc_info=True,
    )
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ Group quiz #{sid} has been paused because Telegram's response timed out "
                f"while publishing question {position + 1}. The poll may or may not have appeared. "
                "To prevent duplicate polls, it will not be resent automatically. Check the group "
                "messages; if the question did not appear, start a new quiz session."
            ),
        )
    except Exception:
        log.warning("Could not notify group about uncertain poll delivery for session %s", sid, exc_info=True)


async def _post_question(application, pool, session, question):
    sid = int(session["id"])
    position = int(session["current_index"])
    # Write a durable intent before sending. If the process dies mid-send, pause rather than blindly duplicating a poll.
    async with pool.connection() as conn:
        cur = await conn.execute("UPDATE group_quiz_session_questions SET status='posting',updated_at=NOW() WHERE session_id=%s AND position=%s AND status='pending' RETURNING position", (sid, position))
        if not await cur.fetchone():
            cur = await conn.execute("SELECT status,poll_id,updated_at FROM group_quiz_session_questions WHERE session_id=%s AND position=%s", (sid, position))
            existing = await cur.fetchone()
            if existing and existing["status"] == "posting" and not existing["poll_id"] and existing["updated_at"] < datetime.now(timezone.utc) - timedelta(seconds=90):
                await conn.execute("UPDATE group_quiz_sessions SET status='paused' WHERE id=%s", (sid,))
                await application.bot.send_message(chat_id=session["chat_id"], text=f"⚠️ Group quiz #{sid} paused because a poll's send status could not be confirmed after a restart. Start a new session to avoid duplicate questions.")
            return
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT announcement_sent FROM group_quiz_sessions WHERE id=%s", (sid,))
        promo_row = await cur.fetchone()
    if promo_row and not promo_row["announcement_sent"]:
        try:
            launch_text = (
                f"🚀 𝗕𝗣𝗦𝗖 𝗡𝗘𝗫𝗨𝗦  ·  𝗚𝗥𝗢𝗨𝗣 𝗤𝗨𝗜𝗭 #{sid}\n\n"
                f"𝗡𝗔𝗧𝗜𝗢𝗡𝗔𝗟 𝗘𝗩𝗘𝗡𝗧𝗦\n{session.get('quiz_title','Group Quiz')}\n\n"
                f"𝗤𝗨𝗘𝗦𝗧𝗜𝗢𝗡𝗦   {int(session['questions_count'])}\n"
                f"𝗠𝗢𝗗𝗘   {str(session.get('mode','learning')).title()}\n"
                f"𝗧𝗜𝗠𝗘   {int(session['interval_seconds'])}s / question\n\n"
                "Sharpen your preparation. Challenge your knowledge. Build your rank.\n\n"
                "𝗣𝗥𝗔𝗖𝗧𝗜𝗦𝗘 𝗠𝗢𝗥𝗘\n@BpscNexustestbot"
            )
            announcement = await application.bot.send_message(chat_id=int(session["chat_id"]), text=launch_text)
            async with pool.connection() as conn:
                await conn.execute("UPDATE group_quiz_sessions SET announcement_sent=TRUE,launch_announcement_message_id=%s WHERE id=%s", (int(announcement.message_id), sid))
        except Exception:
            log.warning("Promotional announcement failed; proceeding with quiz session %s", sid, exc_info=True)
            # Optional promotion failure must never stop a quiz.
            try:
                async with pool.connection() as conn:
                    await conn.execute("UPDATE group_quiz_sessions SET announcement_sent=TRUE WHERE id=%s", (sid,))
            except Exception:
                log.warning("Could not persist announcement guard for session %s", sid, exc_info=True)
    # Group-branded identity and a single editable progress card are shown for every question.
    await _update_live_progress(application, pool, sid, position, create_if_missing=True, force=True)
    opts = question["options"] if isinstance(question["options"], dict) else json.loads(question["options"] or "{}")
    letters = question["option_letters"] if isinstance(question["option_letters"], list) else json.loads(question["option_letters"] or "[]")
    base_question_text = str(question["question_en"] or "Question")
    if question.get("question_hi"):
        base_question_text += "\n" + str(question["question_hi"])
    brand = str(session.get("brand_name") or session.get("chat_title") or "Telegram Group").strip()[:48]
    identity = f"🏷 {brand} · Q{position + 1}/{int(session['questions_count'])} · ⏱ {int(session['interval_seconds'])}s\n\n"
    question_text = identity + base_question_text
    # Telegram limits poll questions to 300 characters; preserve the branding header first.
    question_text = question_text[:300]
    poll_options = [str(opts.get(letter, letter))[:100] for letter in letters]
    explanation = "\n".join(x for x in (question.get("explanation_en"), question.get("explanation_hi")) if x)
    poll = None
    try:
        if session.get("mode","learning")=="exam":
            poll = await _send_poll_with_rate_limit_retry(application.bot,
                chat_id=session["chat_id"], question=question_text[:300], options=poll_options,
                type="regular", is_anonymous=False, allows_multiple_answers=False,
                open_period=int(session["interval_seconds"]),
            )
        else:
            poll = await _send_poll_with_rate_limit_retry(application.bot,
                chat_id=session["chat_id"], question=question_text[:300], options=poll_options,
                type="quiz", correct_option_id=letters.index(question["correct_option"]),
                is_anonymous=False, explanation=explanation[:200] if explanation else None,
                open_period=int(session["interval_seconds"]),
            )
        async with pool.connection() as conn:
            await conn.execute("UPDATE group_quiz_session_questions SET poll_id=%s,message_id=%s,status='open',opened_at=NOW(),updated_at=NOW() WHERE session_id=%s AND position=%s", (str(poll.poll.id), int(poll.message_id), sid, position))
            await conn.execute("UPDATE group_quiz_sessions SET next_at=NOW()+(%s * INTERVAL '1 second') WHERE id=%s", (int(session["interval_seconds"]), sid))
        # Confirmation is sent before this function; delete the launch notice only
        # after the first question was successfully published and persisted.
        if position == 0:
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT launch_announcement_message_id,launch_announcement_deleted FROM group_quiz_sessions WHERE id=%s", (sid,))
                announcement_row = await cur.fetchone()
            if announcement_row and announcement_row.get("launch_announcement_message_id") and not announcement_row.get("launch_announcement_deleted"):
                try:
                    await application.bot.delete_message(chat_id=int(session["chat_id"]), message_id=int(announcement_row["launch_announcement_message_id"]))
                    async with pool.connection() as conn:
                        await conn.execute("UPDATE group_quiz_sessions SET launch_announcement_deleted=TRUE WHERE id=%s", (sid,))
                except Exception:
                    # Telegram permission/deletion failures must not interrupt the live quiz.
                    log.warning("Could not delete launch announcement for quiz session %s; check bot delete permissions", sid, exc_info=True)
    except RetryAfter as exc:
        # Telegram explicitly rejected this request due to rate limiting, so it is safe
        # to retry later. Leave the question pending and pause for an admin-controlled resume.
        log.warning("Telegram rate-limited poll for session %s position %s: %s", sid, position, exc)
        async with pool.connection() as conn:
            await conn.execute("UPDATE group_quiz_session_questions SET status='pending',updated_at=NOW() WHERE session_id=%s AND position=%s AND status='posting'", (sid, position))
            await conn.execute("UPDATE group_quiz_sessions SET status='paused',next_at=NOW() WHERE id=%s AND status='active'", (sid,))
        try:
            await application.bot.send_message(chat_id=session["chat_id"], text=f"⏳ Group quiz #{sid} is paused because Telegram rate-limited poll delivery. No poll was accepted for this attempt. An admin can use /resumequiz to retry question {position + 1}.")
        except Exception:
            log.warning("Could not notify group about rate limit for session %s", sid, exc_info=True)
    except (TimedOut, NetworkError) as exc:
        # This is an ambiguous delivery: Telegram may have created the poll even though
        # the client did not receive its response. Pause rather than marking the quiz failed
        # or blindly retrying and potentially creating duplicate polls.
        await _pause_uncertain_poll_delivery(application, pool, session, sid, position, exc)
    except (Forbidden, BadRequest) as exc:
        # Definitive API rejection: report the actual category instead of claiming every
        # failure is a permission issue. These errors are not safe to retry unchanged.
        log.exception("Telegram rejected poll for session %s position %s", sid, position)
        async with pool.connection() as conn:
            await conn.execute("UPDATE group_quiz_session_questions SET status='failed',updated_at=NOW() WHERE session_id=%s AND position=%s", (sid, position))
            await conn.execute("UPDATE group_quiz_sessions SET status='failed',completed_at=COALESCE(completed_at,NOW()) WHERE id=%s", (sid,))
        if isinstance(exc, Forbidden):
            reason = "Telegram denied the request. Check that the bot is still in the group and has permission to send polls."
        else:
            reason = f"Telegram rejected this poll: {str(exc)[:250]}"
        try:
            await application.bot.send_message(chat_id=session["chat_id"], text=f"❌ Group quiz #{sid} could not publish question {position + 1}. {reason}")
        except Exception:
            log.warning("Could not send poll rejection notice for session %s", sid, exc_info=True)
    except Exception as exc:
        # If Telegram returned a poll object, its ID is known. Retry persistence before
        # falling back to an uncertain-delivery pause, so a transient DB error does not
        # lose the poll ID and cause the next deployment to misclassify the question.
        if poll is not None:
            for attempt in range(3):
                try:
                    async with pool.connection() as conn:
                        await conn.execute(
                            "UPDATE group_quiz_session_questions SET poll_id=%s,message_id=%s,status='open',opened_at=COALESCE(opened_at,NOW()),updated_at=NOW() WHERE session_id=%s AND position=%s",
                            (str(poll.poll.id), int(poll.message_id), sid, position),
                        )
                        await conn.execute(
                            "UPDATE group_quiz_sessions SET next_at=NOW()+(%s * INTERVAL '1 second') WHERE id=%s",
                            (int(session["interval_seconds"]), sid),
                        )
                    log.warning("Recovered poll persistence after error for session %s position %s", sid, position)
                    return
                except Exception:
                    if attempt == 2:
                        log.exception("Could not persist known Telegram poll for session %s position %s", sid, position)
                    else:
                        await asyncio.sleep(0.5 * (attempt + 1))
        # Unexpected errors may occur before Telegram confirms delivery or after the
        # request outcome becomes ambiguous. Never mark the entire session failed solely
        # because the client did not get a reliable confirmation.
        await _pause_uncertain_poll_delivery(application, pool, session, sid, position, exc)


def _html_page(title: str, body: str, report_type: str = "report", brand_name: str = "BPSC Nexus") -> str:
    """Build a self-contained Nexus Magazine report with group-specific branding."""
    safe_brand = html_escape(str(brand_name or "BPSC Nexus").strip() or "BPSC Nexus")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_escape(title)}</title><style>
:root{{--ink:#252a31;--muted:#68717c;--accent:#b24d3e;--line:#e2ded8;--panel:#f8f5f1;--paper:#fff}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:#ece8e2;color:var(--ink);font:15px/1.65 Arial,Helvetica,sans-serif}}
main{{max-width:1000px;margin:0 auto;padding:22px 16px 36px}}
header{{background:var(--paper);color:var(--ink);border:1px solid #d7d0c7;border-top:8px solid var(--accent);padding:30px 28px;margin-bottom:18px;position:relative}}
.eyebrow{{font:700 10px/1.4 Arial,sans-serif;letter-spacing:.22em;text-transform:uppercase;color:var(--accent);margin-bottom:12px}}
header h1{{font:normal clamp(28px,5vw,42px)/1.12 Georgia,'Times New Roman',serif;margin:0 0 10px;max-width:800px}}
header p{{margin:5px 0;color:#64676b}}.cover-meta{{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}}
.cover-meta span,.pill{{display:inline-block;padding:4px 9px;border:1px solid #e2ded8;background:#f7e9e5;color:#8b392f;border-radius:2px;font-size:11px;font-weight:700;letter-spacing:.04em}}
.section-head{{display:flex;align-items:baseline;gap:12px;border-bottom:1px solid #cfc7bd;padding:0 0 8px;margin:26px 0 12px}}
.section-no{{font:700 11px Arial,sans-serif;color:var(--accent);letter-spacing:.12em}}
h2{{font:normal 25px/1.2 Georgia,'Times New Roman',serif;margin:0}}h3{{font:normal 19px/1.3 Georgia,'Times New Roman',serif;margin:0 0 10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px;margin:14px 0}}
.card,.q,.panel{{background:white;border:1px solid var(--line);border-radius:2px;padding:16px;break-inside:avoid}}
.metric{{font:normal 27px/1.15 Georgia,'Times New Roman',serif;color:var(--ink);margin-top:6px}}.muted{{color:var(--muted);font-size:12px}}
.q{{margin:12px 0;padding:18px 20px;border-left:3px solid #d6c6b9}}.q h3{{font-size:18px}}.options{{margin:9px 0;padding-left:22px}}.options li{{padding:3px 0}}.correct{{color:#276344;font-weight:700}}.wrong{{color:#a33b31;font-weight:700}}
.pill{{border:0;background:#f1eee8;color:#66503b;letter-spacing:0}}.bar{{height:7px;background:#f0ede8;overflow:hidden;margin:10px 0 14px}}.bar span{{display:block;height:100%;background:var(--accent)}}
.table-wrap{{overflow-x:auto;border:1px solid var(--line);background:white}}table{{width:100%;border-collapse:collapse;min-width:540px}}th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px}}th{{background:#f6eeea;color:#65352e;text-transform:uppercase;letter-spacing:.06em;font:700 10px Arial,sans-serif}}tbody tr:nth-child(even){{background:#fcfbf9}}
.note{{padding:12px 14px;background:#f8f5f1;border-left:3px solid var(--accent);color:#62666b;font-size:12px;margin:14px 0}}
footer{{border-top:1px solid #d7d0c7;padding-top:14px;color:var(--muted);font-size:11px;margin:30px 0 0;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}}
@media(max-width:600px){{main{{padding:10px 8px 24px}}header{{padding:22px 18px}}.grid{{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}.card{{padding:12px}}.metric{{font-size:23px}}.q{{padding:14px}}th,td{{padding:8px}}}}
@media print{{body{{background:#fff}}main{{max-width:none;padding:0}}header,.card,.q,.panel{{box-shadow:none;break-inside:avoid}}.q{{break-inside:auto}}}}
</style></head><body><main>{body}<footer><span>Powered by BPSC Nexus · {html_escape(report_type)}</span><span>{safe_brand}</span></footer></main></body></html>''' 


def _options_dict(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


def _build_group_html(session_id, title, total, participants, votes, correct, wrong,
                      unanswered, positive, negative, average_score, median_score,
                      high_score, low_score, question_stats, answer_key, scores, brand_name="BPSC Nexus"):
    brand = html_escape(str(brand_name or "BPSC Nexus").strip() or "BPSC Nexus")
    body = [f'<header><div class="eyebrow">{brand} · QUIZ REPORT</div><h1>Group Performance Report</h1><p>{html_escape(str(title))}</p><div class="cover-meta"><span>GROUP EDITION</span><span>QUIZ #{int(session_id)}</span><span>{int(total)} QUESTIONS</span><span>{int(participants)} PARTICIPANTS</span></div></header>']
    metrics = [("Participants", participants), ("Questions", total), ("Submitted answers", votes), ("Correct responses", correct), ("Wrong responses", wrong), ("Unanswered", unanswered)]
    body.append('<div class="section-head"><span class="section-no">01</span><h2>At a glance</h2></div><section class="grid">' + ''.join(f'<div class="card"><div class="muted">{html_escape(str(k))}</div><div class="metric">{int(v)}</div></div>' for k,v in metrics) + '</section>')
    stat_cards = ''.join(f'<div class="card"><div class="muted">{label}</div><div class="metric">{value:.2f}</div></div>' for label,value in [("Average",average_score),("Median",median_score),("Highest",high_score),("Lowest",low_score)])
    body.append(f'<div class="section-head"><span class="section-no">02</span><h2>Score statistics</h2></div><section class="grid">{stat_cards}</section>')
    body.append(f'<p class="note">Scoring: +{positive:g} for correct answers · −{negative:g} for incorrect answers. The report contains group-level question analysis; student-by-student answers are kept private.</p>')
    body.append('<div class="section-head"><span class="section-no">03</span><h2>Leaderboard</h2></div><div class="table-wrap"><table><thead><tr><th>Rank</th><th>Participant</th><th>Score</th><th>Accuracy</th><th>Correct</th><th>Wrong</th><th>Skipped</th></tr></thead><tbody>')
    for rank, row in enumerate(scores, 1):
        name = html_escape(str(row.get('first_name') or row.get('username') or 'Student').replace('\n', ' ').replace('\r', ' ')[:50])
        c = int(row.get('correct') or 0); w = int(row.get('incorrect') or 0); attempted = int(row.get('attempted') or 0)
        skipped = max(0, int(total) - attempted); score = c * float(positive) - w * float(negative)
        accuracy = (100 * c / attempted) if attempted else 0
        body.append(f'<tr><td>{rank:02d}</td><td>{name}</td><td>{score:.2f}</td><td>{accuracy:.1f}%</td><td>{c}</td><td>{w}</td><td>{skipped}</td></tr>')
    body.append('</tbody></table></div><div class="section-head"><span class="section-no">04</span><h2>Question-wise analysis & answer key</h2></div>')
    for stat, answer in zip(question_stats, answer_key):
        opts = _options_dict(answer.get('options')); qn = int(stat['position']) + 1
        attempted_q = int(stat.get('attempted') or 0); correct_q = int(stat.get('correct') or 0); wrong_q = int(stat.get('incorrect') or 0)
        skipped_q = max(0, int(participants) - attempted_q); acc = 100 * correct_q / attempted_q if attempted_q else 0
        prompt = html_escape(str(answer.get('question_en') or '')); hi = html_escape(str(answer.get('question_hi') or ''))
        opts_html = ''.join(f'<li class="{"correct" if str(k)==str(answer.get("correct_option")) else ""}">{html_escape(str(k))}. {html_escape(str(v))}{" ✓" if str(k)==str(answer.get("correct_option")) else ""}</li>' for k,v in opts.items())
        explanation = '<p><b>Explanation:</b> ' + html_escape(str(answer.get('explanation_en') or '')) + (('<br>' + html_escape(str(answer.get('explanation_hi') or ''))) if answer.get('explanation_hi') else '') + '</p>' if (answer.get('explanation_en') or answer.get('explanation_hi')) else ''
        body.append(f'<article class="q"><h3>Q{qn}. {prompt}</h3>{("<p>"+hi+"</p>") if hi else ""}<span class="pill">{acc:.1f}% correct among attempts</span><p class="muted">Attempted: {attempted_q} · Correct: {correct_q} · Wrong: {wrong_q} · Unanswered: {skipped_q} · Average response: {float(stat.get("avg_response_seconds") or 0):.1f}s</p><div class="bar"><span style="width:{min(100,max(0,acc)):.1f}%"></span></div><ol class="options">{opts_html}</ol>{explanation}</article>')
    body.append('<h2>Most difficult questions</h2><table><thead><tr><th>Question</th><th>Correct</th><th>Wrong</th><th>Accuracy</th></tr></thead><tbody>')
    ranked = sorted(question_stats, key=lambda x: ((int(x.get('correct') or 0) / int(x.get('attempted') or 1)) if int(x.get('attempted') or 0) else 0, int(x['position'])))[:10]
    for st in ranked:
        at=int(st.get('attempted') or 0); cq=int(st.get('correct') or 0); wq=int(st.get('incorrect') or 0); ac=100*cq/at if at else 0
        body.append(f'<tr><td>Q{int(st["position"])+1}</td><td>{cq}</td><td>{wq}</td><td>{ac:.1f}%</td></tr>')
    body.append('</tbody></table>')
    return _html_page(f'{str(brand_name or "BPSC Nexus").strip() or "BPSC Nexus"} · Group Quiz {session_id}', ''.join(body), 'Group edition · shared report', brand_name)


async def send_personal_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, pool, session_id: int) -> bool:
    """Send only the requesting Telegram user's personal analysis in private chat."""
    user = update.effective_user; message = update.effective_message
    if user is None or message is None or message.chat.type != ChatType.PRIVATE:
        return False
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id,quiz_title,questions_count,positive_marks,negative_marks,status,brand_name FROM group_quiz_sessions WHERE id=%s", (int(session_id),))
        session = await cur.fetchone()
        if not session or session['status'] != 'completed':
            await message.reply_text("This quiz report is not ready yet, or the quiz reference is invalid.")
            return True
        cur = await conn.execute("""SELECT q.position,q.question_en,q.question_hi,q.options,q.correct_option,q.explanation_en,q.explanation_hi,q.opened_at,a.selected_letter,a.answered_at
            FROM group_quiz_session_questions q LEFT JOIN group_quiz_answers a
              ON a.session_id=q.session_id AND a.position=q.position AND a.telegram_id=%s
            WHERE q.session_id=%s ORDER BY q.position""", (int(user.id), int(session_id)))
        rows = await cur.fetchall()
    answered_rows = [r for r in rows if r.get('selected_letter') is not None]
    if not answered_rows:
        await message.reply_text("No saved answers were found for your Telegram account in this quiz. Make sure you participated using this account.")
        return True
    total = int(session['questions_count'] or len(rows)); positive=float(session['positive_marks'] or 1); negative=float(session['negative_marks'] or 0)
    correct = sum(1 for r in answered_rows if str(r['selected_letter']) == str(r['correct_option']))
    wrong = sum(1 for r in answered_rows if str(r['selected_letter']) != str(r['correct_option']))
    attempted=len(answered_rows); skipped=max(0,total-attempted); score=correct*positive-wrong*negative; accuracy=100*correct/attempted if attempted else 0
    brand_name = str(session.get('brand_name') or 'BPSC Nexus').strip() or 'BPSC Nexus'
    safe_brand = html_escape(brand_name)
    body=[f'<header><div class="eyebrow">{safe_brand} · PRIVATE SCORECARD</div><h1>My Performance Report</h1><p>{html_escape(str(session["quiz_title"]))}</p><div class="cover-meta"><span>PERSONAL EDITION</span><span>QUIZ #{int(session_id)}</span><span>{total} QUESTIONS</span></div></header>']
    metrics=[('My score',f'{score:.2f}'),('Accuracy',f'{accuracy:.1f}%'),('Correct',correct),('Wrong',wrong),('Attempted',attempted),('Skipped',skipped)]
    body.append('<div class="section-head"><span class="section-no">01</span><h2>Your scorecard</h2></div><section class="grid">'+''.join(f'<div class="card"><div class="muted">{html_escape(str(k))}</div><div class="metric">{html_escape(str(v))}</div></div>' for k,v in metrics)+'</section><div class="section-head"><span class="section-no">02</span><h2>Question-by-question review</h2></div>')
    for r in rows:
        qn=int(r['position'])+1; opts=_options_dict(r.get('options')); selected=r.get('selected_letter'); correct_letter=r.get('correct_option')
        state='Skipped' if selected is None else ('Correct' if str(selected)==str(correct_letter) else 'Incorrect')
        state_class='correct' if state=='Correct' else ('wrong' if state=='Incorrect' else 'muted')
        body.append(f'<article class="q"><h3>Q{qn}. {html_escape(str(r.get("question_en") or ""))}</h3>')
        if r.get('question_hi'): body.append(f'<p>{html_escape(str(r["question_hi"]))}</p>')
        my_answer = str(selected) + '. ' + str(opts.get(selected, '')) if selected is not None else 'Not attempted'
        correct_answer = str(correct_letter) + '. ' + str(opts.get(correct_letter, ''))
        body.append(f'<p><span class="pill {state_class}">{state}</span></p><p><b>My answer:</b> {html_escape(my_answer)}</p><p class="correct"><b>Correct answer:</b> {html_escape(correct_answer)}</p><ol class="options">')
        for key,val in opts.items():
            cls='correct' if str(key)==str(correct_letter) else ('wrong' if selected is not None and str(key)==str(selected) else '')
            body.append(f'<li class="{cls}">{html_escape(str(key))}. {html_escape(str(val))}</li>')
        body.append('</ol>')
        if r.get('explanation_en'): body.append(f'<p><b>Explanation:</b> {html_escape(str(r["explanation_en"]))}</p>')
        if r.get('explanation_hi'): body.append(f'<p>{html_escape(str(r["explanation_hi"]))}</p>')
        if selected is not None and r.get('answered_at') and r.get('opened_at'):
            try:
                secs=max(0,(r['answered_at']-r['opened_at']).total_seconds()); body.append(f'<p class="muted">Your response time: {secs:.1f}s</p>')
            except Exception: pass
        body.append('</article>')
    html_doc=_html_page(f'{brand_name} · Personal Quiz Analysis {session_id}', ''.join(body), 'Personal edition · private report', brand_name)
    brand_slug = re.sub(r'[^A-Za-z0-9_-]+', '_', brand_name).strip('_')[:40] or 'BPSC_Nexus'
    bio=BytesIO(html_doc.encode('utf-8')); bio.name=f'{brand_slug}_Personal_Quiz_Report_{int(session_id)}.html'
    try:
        await context.bot.send_document(
            chat_id=user.id,
            document=bio,
            filename=bio.name,
            caption=(
                "📊 <b>Your Personal Quiz Report Is Ready</b>\n\n"
                "Your performance summary, answer review and explanations are included in the attached report.\n\n"
                "🎯 Review your mistakes, strengthen weak areas, and make every attempt count.\n"
                "<b>Keep improving. Keep progressing.</b>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        log.exception('Could not send personal quiz report to Telegram user %s', user.id)
        await message.reply_text("I couldn't send the report. Please press Start in this private chat and try /myanalysis again.")
    return True


async def personal_analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE, pool):
    user=update.effective_user; message=update.effective_message
    if not user or not message: return
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text("For privacy, open @BpscNexustestbot in a private chat to view your own analysis.")
        return
    session_id=None
    if context.args:
        try: session_id=int(context.args[0])
        except ValueError:
            await message.reply_text("Usage: /myanalysis or /myanalysis <quiz_id>"); return
    else:
        async with pool.connection() as conn:
            cur=await conn.execute("""SELECT s.id FROM group_quiz_sessions s JOIN group_quiz_answers a ON a.session_id=s.id
                WHERE s.status='completed' AND a.telegram_id=%s GROUP BY s.id,s.completed_at ORDER BY s.completed_at DESC LIMIT 1""",(int(user.id),))
            latest=await cur.fetchone()
        if not latest:
            await message.reply_text("No completed group quiz with saved answers was found for your account yet.")
            return
        session_id=int(latest['id'])
    await send_personal_analysis(update,context,pool,session_id)



async def _finish_session(application, pool, session):
    sid=int(session["id"])
    async with pool.connection() as conn:
        cur=await conn.execute("""UPDATE group_quiz_sessions SET status='completed',completed_at=COALESCE(completed_at,NOW())
            WHERE id=%s AND (status='active' OR (status='completed' AND report_sent=FALSE))
            RETURNING questions_count,quiz_title,positive_marks,negative_marks,started_by,chat_id,started_at,mode,summary_sent,answer_key_sent,group_report_sent,brand_name,chat_title""",(sid,))
        finished=await cur.fetchone()
        if not finished: return
        positive=float(finished["positive_marks"] or 1); negative=float(finished["negative_marks"] or 0.333)
        cur=await conn.execute("""SELECT a.telegram_id,MAX(a.first_name) AS first_name,MAX(a.username) AS username,
            COUNT(*) FILTER (WHERE a.selected_letter=q.correct_option) AS correct,
            COUNT(*) FILTER (WHERE a.selected_letter IS NOT NULL AND a.selected_letter<>q.correct_option) AS incorrect,
            COUNT(*) FILTER (WHERE a.selected_letter IS NOT NULL) AS attempted,
            MIN(a.answered_at) AS first_answer,MAX(a.answered_at) AS last_answer,
            SUM(GREATEST(0,EXTRACT(EPOCH FROM (a.answered_at-q.opened_at)))) FILTER (WHERE a.selected_letter IS NOT NULL AND q.opened_at IS NOT NULL) AS total_response_seconds,
            AVG(GREATEST(0,EXTRACT(EPOCH FROM (a.answered_at-q.opened_at)))) FILTER (WHERE a.selected_letter IS NOT NULL AND q.opened_at IS NOT NULL) AS avg_response_seconds
            FROM group_quiz_answers a JOIN group_quiz_session_questions q USING(session_id,position)
            WHERE a.session_id=%s GROUP BY a.telegram_id ORDER BY
            (COUNT(*) FILTER (WHERE a.selected_letter=q.correct_option)*%s-COUNT(*) FILTER (WHERE a.selected_letter IS NOT NULL AND a.selected_letter<>q.correct_option)*%s) DESC,
            correct DESC,incorrect ASC,MAX(a.answered_at)-MIN(a.answered_at) ASC,a.telegram_id ASC""",(sid,positive,negative))
        scores=await cur.fetchall()
        cur=await conn.execute("""SELECT COUNT(DISTINCT telegram_id) AS participants,
            COUNT(*) FILTER (WHERE selected_letter IS NOT NULL) AS votes,
            COUNT(*) FILTER (WHERE selected_letter=q.correct_option) AS correct,
            COUNT(*) FILTER (WHERE selected_letter IS NOT NULL AND selected_letter<>q.correct_option) AS incorrect
            FROM group_quiz_answers a JOIN group_quiz_session_questions q USING(session_id,position) WHERE a.session_id=%s""",(sid,))
        agg=await cur.fetchone()
        cur=await conn.execute("SELECT position,question_en,question_hi,options,correct_option,explanation_en,explanation_hi FROM group_quiz_session_questions WHERE session_id=%s ORDER BY position",(sid,))
        answer_key=await cur.fetchall()
        cur=await conn.execute("""SELECT q.position,COUNT(a.telegram_id) FILTER (WHERE a.selected_letter IS NOT NULL) AS attempted,
            COUNT(a.telegram_id) FILTER (WHERE a.selected_letter=q.correct_option) AS correct,
            COUNT(a.telegram_id) FILTER (WHERE a.selected_letter IS NOT NULL AND a.selected_letter<>q.correct_option) AS incorrect,
            AVG(EXTRACT(EPOCH FROM (a.answered_at-q.opened_at))) FILTER (WHERE a.selected_letter IS NOT NULL AND q.opened_at IS NOT NULL) AS avg_response_seconds,
            (SELECT a2.selected_letter FROM group_quiz_answers a2 WHERE a2.session_id=q.session_id AND a2.position=q.position AND a2.selected_letter IS NOT NULL AND a2.selected_letter<>q.correct_option GROUP BY a2.selected_letter ORDER BY COUNT(*) DESC,a2.selected_letter LIMIT 1) AS most_common_wrong
            FROM group_quiz_session_questions q LEFT JOIN group_quiz_answers a ON a.session_id=q.session_id AND a.position=q.position
            WHERE q.session_id=%s GROUP BY q.position,q.session_id,q.correct_option ORDER BY q.position""",(sid,))
        question_stats=await cur.fetchall()
    total=int(finished["questions_count"] or 0); participants=int(agg["participants"] or 0); votes=int(agg["votes"] or 0); correct=int(agg["correct"] or 0); wrong=int(agg["incorrect"] or 0)
    started=finished["started_at"]
    if started and started.tzinfo is None: started=started.replace(tzinfo=timezone.utc)
    elapsed=max(0,int((datetime.now(timezone.utc)-(started or datetime.now(timezone.utc))).total_seconds()))
    unanswered=max(0,total*participants-votes)
    score_values=sorted((int(row["correct"] or 0)*positive-int(row["incorrect"] or 0)*negative) for row in scores)
    if score_values:
        average_score=sum(score_values)/len(score_values)
        middle=len(score_values)//2
        median_score=score_values[middle] if len(score_values)%2 else (score_values[middle-1]+score_values[middle])/2
        high_score=max(score_values); low_score=min(score_values)
    else:
        average_score=median_score=high_score=low_score=0.0
    # Nexus Magazine: one concise group completion notice. Detailed statistics
    # live in the attached HTML report, not in a second text leaderboard message.
    heading = f"🏁 BPSC NEXUS · QUIZ #{sid}"
    lines = (
        f"<b>{heading}</b>\n\n"
        "✅ Quiz concluded successfully.\n\n"
        "📑 The complete results and detailed performance report are attached.\n\n"
        "📊 <b>MY ANALYSIS</b> · Access your personal performance report using the button below.\n\n"
        "@BpscNexustestbot"
    )

    # Claim each Telegram delivery atomically. This prevents concurrent scheduler
    # iterations/finalization calls from sending duplicate summaries or HTML files.
    # A stale claim is reclaimed after 15 minutes if a worker dies mid-delivery.
    async def claim_delivery(claim_column: str, sent_column: str) -> bool:
        allowed = {("summary_claimed_at", "summary_sent"), ("group_report_claimed_at", "group_report_sent")}
        if (claim_column, sent_column) not in allowed:
            raise ValueError("Invalid report delivery claim")
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE group_quiz_sessions SET {claim_column}=NOW() "
                f"WHERE id=%s AND {sent_column}=FALSE "
                f"AND ({claim_column} IS NULL OR {claim_column}<NOW()-INTERVAL '15 minutes') RETURNING id",
                (sid,),
            )
            return await cur.fetchone() is not None

    if not finished.get("summary_sent", False) and await claim_delivery("summary_claimed_at", "summary_sent"):
        try:
            bot_username = application.bot_data.get("bot_username")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
                "📊 My Analysis", url=f"https://t.me/{bot_username}?start=gqa_{sid}"
            )]]) if bot_username else None
            await application.bot.send_message(
                chat_id=int(finished["chat_id"]), text=lines[:3900], parse_mode="HTML", reply_markup=keyboard
            )
            async with pool.connection() as conn:
                await conn.execute("UPDATE group_quiz_sessions SET summary_sent=TRUE,summary_claimed_at=NULL WHERE id=%s", (sid,))
        except Exception:
            async with pool.connection() as conn:
                await conn.execute("UPDATE group_quiz_sessions SET summary_claimed_at=NULL WHERE id=%s AND summary_sent=FALSE", (sid,))
            log.exception("Could not publish group quiz summary for %s; it will be retried", sid)
            raise

    # The group HTML contains the answer key and explanations. Do not send a second,
    # redundant Exam Mode answer-key message to the group.
    if not finished.get("group_report_sent", False) and await claim_delivery("group_report_claimed_at", "group_report_sent"):
        try:
            group_html = _build_group_html(sid, finished["quiz_title"], total, participants, votes,
                correct, wrong, unanswered, positive, negative, average_score, median_score,
                high_score, low_score, question_stats, answer_key, scores, finished.get("brand_name") or "BPSC Nexus")
            report_file = BytesIO(group_html.encode("utf-8"))
            brand_slug = re.sub(r'[^A-Za-z0-9_-]+', '_', str(finished.get("brand_name") or "BPSC Nexus")).strip('_')[:40] or "BPSC_Nexus"
            report_file.name = f"{brand_slug}_Quiz_{sid}_Group_Report.html"
            await application.bot.send_document(
                chat_id=int(finished["chat_id"]), document=report_file, filename=report_file.name,
                caption=(f"📊 <b>{html_escape(str(finished.get('brand_name') or 'BPSC Nexus'))} · GROUP ANALYSIS</b>\n"
                         f"Quiz #{sid} · {html_escape(str(finished['quiz_title'] or 'Group Quiz'))}\n"
                         "Question-wise statistics, answer key and explanations. Personal student data is excluded."),
                parse_mode="HTML",
            )
            async with pool.connection() as conn:
                await conn.execute("UPDATE group_quiz_sessions SET group_report_sent=TRUE,group_report_claimed_at=NULL WHERE id=%s", (sid,))
        except Exception:
            async with pool.connection() as conn:
                await conn.execute("UPDATE group_quiz_sessions SET group_report_claimed_at=NULL WHERE id=%s AND group_report_sent=FALSE", (sid,))
            log.exception("Could not publish group HTML report for quiz %s; it will be retried", sid)
            raise

    # Personal reports are generated only when each student requests their own report.
    # No individual analysis is sent automatically to the quiz creator/admin.
    async with pool.connection() as conn:
        await conn.execute("""UPDATE group_quiz_sessions SET report_sent=(summary_sent AND group_report_sent)
            WHERE id=%s""", (sid,))
    await _update_live_progress(application, pool, sid, max(0, total - 1), force=True, completed=True)

async def scheduler(application, pool):
    """Persisted scheduler. Poll state and due times survive service restarts."""
    while True:
        try:
            async with pool.connection() as conn:
                # Global recovery: retire orphaned active rows and finalize exhausted sessions
                # before the normal due-session loop. This prevents legacy DB rows from
                # permanently blocking new quizzes after a deployment/restart.
                await conn.execute("""UPDATE group_quiz_sessions s SET status='completed',completed_at=COALESCE(completed_at,NOW()),next_at=NOW()
                    WHERE s.status='active' AND s.questions_count>0 AND s.current_index>=s.questions_count""")
                await conn.execute("""UPDATE group_quiz_sessions s SET status='cancelled',completed_at=COALESCE(completed_at,NOW())
                    WHERE s.status='active' AND (s.questions_count<=0 OR NOT EXISTS (
                        SELECT 1 FROM group_quiz_session_questions q WHERE q.session_id=s.id))""")
                # Any non-progressing active/paused session older than 24h is stale. A
                # future-dated scheduled quiz is preserved because next_at must already be due.
                await conn.execute("""UPDATE group_quiz_sessions s SET status='cancelled',completed_at=COALESCE(completed_at,NOW())
                    WHERE s.status IN ('active','paused') AND s.next_at<=NOW()
                      AND s.created_at < NOW()-INTERVAL '24 hours'""")
                # Recover sessions whose final question finished even if next_at/current_index
                # were left stale by an older deployment or a restart. The NOT EXISTS clause
                # only matches sessions where no question remains pending, posting, or open.
                cur = await conn.execute("""SELECT s.* FROM group_quiz_sessions s
                    WHERE (s.status='active' AND (
                        s.next_at<=NOW()
                        OR s.current_index>=s.questions_count
                        OR (
                            EXISTS (SELECT 1 FROM group_quiz_session_questions q
                                    WHERE q.session_id=s.id AND q.status='closed')
                            AND NOT EXISTS (SELECT 1 FROM group_quiz_session_questions q
                                            WHERE q.session_id=s.id AND q.status IN ('pending','posting','open'))
                        )
                    )) OR (s.status='completed' AND s.report_sent=FALSE)
                    ORDER BY s.next_at,s.id LIMIT 10""")
                sessions = await cur.fetchall()
            for session in sessions:
                sid = int(session["id"]); position = int(session["current_index"])
                if session.get("status") == "completed":
                    await _finish_session(application, pool, session)
                    continue
                # Finalization takes priority over membership checks: a quiz that has
                # already exhausted its questions must produce its results, not be cancelled.
                if position >= int(session["questions_count"]):
                    await _finish_session(application, pool, session)
                    continue
                if not await _group_is_authorized(pool, int(session["chat_id"])):
                    async with pool.connection() as conn:
                        await conn.execute("UPDATE group_quiz_sessions SET status='cancelled',completed_at=NOW() WHERE id=%s AND status='active'", (sid,))
                    try:
                        await application.bot.send_message(chat_id=int(session["chat_id"]), text="🔒 BPSC Nexus Quiz membership is inactive for this group. This quiz has been stopped.")
                    except Exception:
                        log.info("Could not notify group %s about inactive membership", session["chat_id"])
                    continue
                async with pool.connection() as conn:
                    cur = await conn.execute("SELECT * FROM group_quiz_session_questions WHERE session_id=%s AND position=%s", (sid, position))
                    question = await cur.fetchone()
                if not question:
                    async with pool.connection() as conn:
                        await conn.execute("UPDATE group_quiz_sessions SET status='failed' WHERE id=%s", (sid,))
                    continue
                if question["status"] in ("pending", "posting"):
                    await _post_question(application, pool, session, question)
                    continue
                if question["status"] == "open":
                    try:
                        await application.bot.stop_poll(chat_id=session["chat_id"], message_id=int(await _poll_message_id(pool, question["poll_id"])))
                    except Exception:
                        # Telegram may have auto-closed the poll at open_period; that is expected.
                        log.debug("Poll already closed or message unavailable for session %s", sid)
                    async with pool.connection() as conn:
                        await conn.execute("UPDATE group_quiz_session_questions SET status='closed',updated_at=NOW() WHERE session_id=%s AND position=%s", (sid, position))
                        next_pos = position + 1
                        await conn.execute("UPDATE group_quiz_sessions SET current_index=%s,next_at=NOW() WHERE id=%s AND status='active'", (next_pos, sid))
                    if next_pos >= int(session["questions_count"]):
                        async with pool.connection() as conn:
                            cur = await conn.execute("SELECT * FROM group_quiz_sessions WHERE id=%s", (sid,)); fresh = await cur.fetchone()
                        await _finish_session(application, pool, fresh)
                elif question["status"] == "closed":
                    # Backward-compatible recovery for sessions from older code that
                    # closed the poll but failed to advance current_index.
                    async with pool.connection() as conn:
                        await conn.execute("UPDATE group_quiz_sessions SET current_index=GREATEST(current_index,%s),next_at=NOW() WHERE id=%s AND status='active'", (position + 1, sid))
                        cur = await conn.execute("SELECT * FROM group_quiz_sessions WHERE id=%s", (sid,))
                        fresh = await cur.fetchone()
                    if fresh and int(fresh["current_index"]) >= int(fresh["questions_count"]):
                        await _finish_session(application, pool, fresh)
                elif question["status"] == "failed":
                    async with pool.connection() as conn:
                        await conn.execute("UPDATE group_quiz_sessions SET status='failed' WHERE id=%s", (sid,))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Group quiz scheduler iteration failed")
        await asyncio.sleep(0.75)


async def _poll_message_id(pool, poll_id):
    # Telegram closePoll requires the message ID, which is stored alongside the poll ID.
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT message_id FROM group_quiz_session_questions WHERE poll_id=%s", (str(poll_id),))
        row = await cur.fetchone()
    if not row or not row["message_id"]:
        raise RuntimeError("No Telegram message ID stored for poll")
    return int(row["message_id"])


async def group_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    pool = context.application.bot_data.get("db_pool")
    if not pa or not pa.user or pool is None:
        return
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT q.session_id,q.position,q.option_letters FROM group_quiz_session_questions q JOIN group_quiz_sessions s ON s.id=q.session_id WHERE q.poll_id=%s AND q.status='open' AND s.status='active'", (str(pa.poll_id),))
        row = await cur.fetchone()
        if not row:
            return
        letters = row["option_letters"] if isinstance(row["option_letters"], list) else json.loads(row["option_letters"] or "[]")
        selected = letters[pa.option_ids[0]] if pa.option_ids and pa.option_ids[0] < len(letters) else None
        await conn.execute("""INSERT INTO group_quiz_answers(session_id,position,telegram_id,first_name,username,selected_letter)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(session_id,position,telegram_id) DO UPDATE
            SET first_name=EXCLUDED.first_name,username=EXCLUDED.username,selected_letter=EXCLUDED.selected_letter,answered_at=NOW()""",
            (row["session_id"], row["position"], pa.user.id, pa.user.first_name, pa.user.username, selected))
    await _update_live_progress(context.application, pool, int(row["session_id"]), int(row["position"]), force=False)
