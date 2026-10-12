"""Persistent sequential Telegram group quiz engine for BPSC Nexus."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import ContextTypes

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
    await message.reply_text(f"🏁 Quiz #{row['id']} stopped early and marked COMPLETED. The summary, leaderboard and detailed admin analysis are being finalized.")


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
        await conn.execute("SELECT pg_advisory_xact_lock(741923, %s)", (int(chat.id),))
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
        cur=await conn.execute("""INSERT INTO group_quiz_sessions(chat_id,chat_title,quiz_id,quiz_title,started_by,interval_seconds,questions_count,mode,current_index,status,next_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0,'active',%s) RETURNING id""",(chat.id,chat.title or "Telegram group",quiz_id,quiz["title"],user.id,interval,len(prepared),mode,when))
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
        for pos,qid in enumerate(ids,1):
            await conn.execute("INSERT INTO bank_quiz_questions(quiz_id,position,question_id) VALUES(%s,%s,%s)",(quiz_id,pos,qid))
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
        try: sid=int(data.rsplit(":",1)[1])
        except ValueError:
            await q.answer("Invalid report reference.",show_alert=True); return True
        async with pool.connection() as conn:
            cur=await conn.execute("""SELECT r.chunk_no,r.report_text FROM group_quiz_admin_reports r
                JOIN group_quiz_sessions s ON s.id=r.session_id
                WHERE r.session_id=%s AND s.chat_id=%s AND s.status='completed' ORDER BY r.chunk_no""",(sid,chat.id))
            reports=await cur.fetchall()
        if not reports:
            await q.answer("The report is still being prepared. Tap again in a few seconds.",show_alert=True); return True
        try:
            for report in reports:
                await context.bot.send_message(chat_id=user.id,text=report["report_text"])
            await q.answer("Detailed analysis sent privately.")
        except Exception:
            await q.answer("Open @BpscNexustestbot and press Start, then tap this report button again.",show_alert=True)
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
        await conn.execute("SELECT pg_advisory_xact_lock(741923, %s)", (int(chat_id),))
        active_id = await _reconcile_chat_quizzes(conn, chat_id)
        if active_id is not None:
            await q.answer(f"This group already has live or scheduled quiz session #{active_id}. Finish it first.",show_alert=True); return True
        cur=await conn.execute("INSERT INTO group_quiz_sessions(chat_id,chat_title,quiz_id,quiz_title,started_by,interval_seconds,questions_count,mode,current_index,status,next_at,positive_marks,negative_marks,started_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0,'active',NOW()+INTERVAL '5 seconds',%s,%s,NOW()) RETURNING id",(chat_id,pending.get("chat_title","Telegram group"),quiz["id"],quiz["title"],user.id,interval,len(prepared),mode,positive,negative)); sid=int((await cur.fetchone())["id"])
        for position,(row,opts,letters) in enumerate(prepared):
            await conn.execute("INSERT INTO group_quiz_session_questions(session_id,position,question_id,question_en,question_hi,options,option_letters,correct_option,explanation_en,explanation_hi) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",(sid,position,row["id"],row["question_en"],row["question_hi"],json.dumps(opts,ensure_ascii=False),json.dumps(letters),row["correct_option"],row["explanation_en"],row["explanation_hi"]))
    context.user_data.pop("group_quiz_pending",None)
    await q.answer("Quiz launched.")
    range_text=f"\nQuestion range: {pending.get('range_start','?')}–{pending.get('range_end','?')}" if pending.get("range_start") is not None else ""
    await q.edit_message_text(f"✅ Group quiz #{sid} queued\n{quiz['title']}{range_text}\nExactly {len(prepared)} questions · {mode.title()} Mode · {interval}s/question\nScoring: +{positive:g} correct / −{negative:g} wrong")
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
        cur=await conn.execute("SELECT announcement_sent FROM group_quiz_sessions WHERE id=%s",(sid,))
        promo_row=await cur.fetchone()
    if promo_row and not promo_row["announcement_sent"]:
        try:
            await application.bot.send_message(chat_id=int(session["chat_id"]),text=f"🚀 BPSC NEXUS · GROUP QUIZ #{sid}\n{session.get('quiz_title','Group Quiz')}\n{int(session['questions_count'])} questions · {str(session.get('mode','learning')).title()} Mode · {int(session['interval_seconds'])}s/question\n\nPractice more with @BpscNexustestbot: https://t.me/BpscNexustestbot")
        except Exception:
            log.warning("Promotional announcement failed; proceeding with quiz session %s",sid,exc_info=True)
        finally:
            try:
                async with pool.connection() as conn:
                    await conn.execute("UPDATE group_quiz_sessions SET announcement_sent=TRUE WHERE id=%s",(sid,))
            except Exception:
                log.warning("Could not persist announcement guard for session %s",sid,exc_info=True)
    opts = question["options"] if isinstance(question["options"], dict) else json.loads(question["options"] or "{}")
    letters = question["option_letters"] if isinstance(question["option_letters"], list) else json.loads(question["option_letters"] or "[]")
    question_text = str(question["question_en"] or "Question")
    if question.get("question_hi"):
        question_text += "\n" + str(question["question_hi"])
    poll_options = [str(opts.get(letter, letter))[:100] for letter in letters]
    explanation = "\n".join(x for x in (question.get("explanation_en"), question.get("explanation_hi")) if x)
    try:
        if session.get("mode","learning")=="exam":
            poll = await application.bot.send_poll(
                chat_id=session["chat_id"], question=question_text[:300], options=poll_options,
                type="regular", is_anonymous=False, allows_multiple_answers=False,
                open_period=int(session["interval_seconds"]),
            )
        else:
            poll = await application.bot.send_poll(
                chat_id=session["chat_id"], question=question_text[:300], options=poll_options,
                type="quiz", correct_option_id=letters.index(question["correct_option"]),
                is_anonymous=False, explanation=explanation[:200] if explanation else None,
                open_period=int(session["interval_seconds"]),
            )
        async with pool.connection() as conn:
            await conn.execute("UPDATE group_quiz_session_questions SET poll_id=%s,message_id=%s,status='open',opened_at=NOW(),updated_at=NOW() WHERE session_id=%s AND position=%s", (str(poll.poll.id), int(poll.message_id), sid, position))
            await conn.execute("UPDATE group_quiz_sessions SET next_at=NOW()+(%s * INTERVAL '1 second') WHERE id=%s", (int(session["interval_seconds"]), sid))
    except Exception:
        log.exception("Failed to publish group poll for session %s position %s", sid, position)
        async with pool.connection() as conn:
            await conn.execute("UPDATE group_quiz_session_questions SET status='failed',updated_at=NOW() WHERE session_id=%s AND position=%s", (sid, position))
            await conn.execute("UPDATE group_quiz_sessions SET status='failed' WHERE id=%s", (sid,))
        try:
            await application.bot.send_message(chat_id=session["chat_id"], text=f"❌ Group quiz #{sid} stopped because Telegram could not publish a poll. Check bot permissions and start a new session after fixing the issue.")
        except Exception:
            pass


async def _finish_session(application, pool, session):
    sid=int(session["id"])
    async with pool.connection() as conn:
        cur=await conn.execute("""UPDATE group_quiz_sessions SET status='completed',completed_at=COALESCE(completed_at,NOW())
            WHERE id=%s AND (status='active' OR (status='completed' AND report_sent=FALSE))
            RETURNING questions_count,quiz_title,positive_marks,negative_marks,started_by,chat_id,started_at,mode,summary_sent,answer_key_sent""",(sid,))
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
        scores=await conn.fetchall()
        cur=await conn.execute("""SELECT COUNT(DISTINCT telegram_id) AS participants,
            COUNT(*) FILTER (WHERE selected_letter IS NOT NULL) AS votes,
            COUNT(*) FILTER (WHERE selected_letter=q.correct_option) AS correct,
            COUNT(*) FILTER (WHERE selected_letter IS NOT NULL AND selected_letter<>q.correct_option) AS incorrect
            FROM group_quiz_answers a JOIN group_quiz_session_questions q USING(session_id,position) WHERE a.session_id=%s""",(sid,))
        agg=await cur.fetchone()
        cur=await conn.execute("SELECT position,question_en,question_hi,options,correct_option,explanation_en,explanation_hi FROM group_quiz_session_questions WHERE session_id=%s ORDER BY position",(sid,))
        answer_key=await conn.fetchall()
        cur=await conn.execute("""SELECT q.position,COUNT(a.telegram_id) FILTER (WHERE a.selected_letter IS NOT NULL) AS attempted,
            COUNT(a.telegram_id) FILTER (WHERE a.selected_letter=q.correct_option) AS correct,
            COUNT(a.telegram_id) FILTER (WHERE a.selected_letter IS NOT NULL AND a.selected_letter<>q.correct_option) AS incorrect,
            AVG(EXTRACT(EPOCH FROM (a.answered_at-q.opened_at))) FILTER (WHERE a.selected_letter IS NOT NULL AND q.opened_at IS NOT NULL) AS avg_response_seconds,
            (SELECT a2.selected_letter FROM group_quiz_answers a2 WHERE a2.session_id=q.session_id AND a2.position=q.position AND a2.selected_letter IS NOT NULL AND a2.selected_letter<>q.correct_option GROUP BY a2.selected_letter ORDER BY COUNT(*) DESC,a2.selected_letter LIMIT 1) AS most_common_wrong
            FROM group_quiz_session_questions q LEFT JOIN group_quiz_answers a ON a.session_id=q.session_id AND a.position=q.position
            WHERE q.session_id=%s GROUP BY q.position,q.session_id,q.correct_option ORDER BY q.position""",(sid,))
        question_stats=await conn.fetchall()
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
    lines=[f"🏁 BPSC Nexus Group Quiz Complete · #{sid}",str(finished["quiz_title"]),f"Questions: {total} · Participants: {participants}",f"Responses: {votes} · Correct: {correct} · Wrong: {wrong} · Unanswered: {unanswered}",f"Scoring: +{positive:g} correct / −{negative:g} wrong",f"Quiz elapsed: {elapsed//60}m {elapsed%60}s",f"Score average/median/high/low: {average_score:.2f}/{median_score:.2f}/{high_score:.2f}/{low_score:.2f}","","🏆 LEADERBOARD"]
    for rank,row in enumerate(scores[:10],1):
        name=(row["first_name"] or row["username"] or f"Student {row['telegram_id']}").replace("\n"," ")[:30]
        c=int(row["correct"] or 0); w=int(row["incorrect"] or 0); attempted=int(row["attempted"] or 0); skipped=max(0,total-attempted); score=c*positive-w*negative; accuracy=(100*c/attempted) if attempted else 0
        lines.append(f"{rank}. {name} — {score:.2f} pts · {accuracy:.0f}% accuracy · {c}✓ {w}✗ {skipped} unanswered")
    lines += ["","Practice more with @BpscNexustestbot: https://t.me/BpscNexustestbot"]
    # Group summary stays concise; a persisted guard prevents duplicate announcements on retries.
    if not finished.get("summary_sent",False):
        try:
            await application.bot.send_message(
                chat_id=int(finished["chat_id"]), text="\n".join(lines)[:3900],
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 Full Admin Analysis (admins only)",callback_data=f"gq:report:{sid}")]])
            )
            async with pool.connection() as conn:
                await conn.execute("UPDATE group_quiz_sessions SET summary_sent=TRUE WHERE id=%s",(sid,))
        except Exception:
            log.exception("Could not publish group quiz summary for %s",sid)
            raise
    if finished.get("mode")=="exam" and not finished.get("answer_key_sent",False):
        key_lines=["📝 Exam answer key (quiz submitted)"]
        for row in answer_key:
            opts=row["options"] if isinstance(row["options"],dict) else json.loads(row["options"] or "{}")
            item=f"Q{int(row['position'])+1}. {row['correct_option']}. {opts.get(row['correct_option'],'')}"
            if row.get("explanation_en"): item += "\n"+str(row["explanation_en"])[:180]
            if row.get("explanation_hi"): item += "\n"+str(row["explanation_hi"])[:180]
            key_lines.append(item)
        chunks=[]; buf=""
        for line in key_lines:
            if len(buf)+len(line)+1>3500: chunks.append(buf); buf=""
            buf += ("\n" if buf else "")+line
        if buf: chunks.append(buf)
        for chunk in chunks:
            try: await application.bot.send_message(chat_id=int(finished["chat_id"]),text=chunk)
            except Exception:
                log.exception("Could not post final exam answer key for %s",sid)
                raise
        async with pool.connection() as conn:
            await conn.execute("UPDATE group_quiz_sessions SET answer_key_sent=TRUE WHERE id=%s",(sid,))
    admin_lines=[f"📊 FULL ADMIN REPORT · Group Quiz #{sid}",str(finished["quiz_title"]),f"Participants: {participants} · Questions: {total}",f"Correct: {correct} · Wrong: {wrong} · Unanswered: {unanswered}",f"Scoring: +{positive:g} / −{negative:g}",f"Elapsed: {elapsed//60}m {elapsed%60}s",f"Score average/median/high/low: {average_score:.2f}/{median_score:.2f}/{high_score:.2f}/{low_score:.2f}","","INDIVIDUAL PERFORMANCE"]
    for row in scores:
        name=(row["first_name"] or row["username"] or f"Student {row['telegram_id']}").replace("\n"," ")[:32]
        c=int(row["correct"] or 0); w=int(row["incorrect"] or 0); attempted=int(row["attempted"] or 0); score=c*positive-w*negative; acc=100*c/attempted if attempted else 0
        span=0
        if row["first_answer"] and row["last_answer"]: span=max(0,int((row["last_answer"]-row["first_answer"]).total_seconds()))
        response_total=float(row["total_response_seconds"] or 0); response_avg=float(row["avg_response_seconds"] or 0)
        admin_lines.append(f"{name}: {score:.2f} pts | {acc:.1f}% accuracy | {c} correct, {w} wrong, {max(0,total-attempted)} unanswered | active response time {response_total:.1f}s total, {response_avg:.1f}s avg/question | answer span {span}s")
    admin_lines += ["","QUESTION ANALYSIS (attempted / correct / wrong / average response seconds)"]
    for stat in question_stats:
        attempted_q=int(stat["attempted"] or 0); correct_q=int(stat["correct"] or 0); wrong_q=int(stat["incorrect"] or 0)
        accuracy_q=(100*correct_q/attempted_q) if attempted_q else 0
        avg_seconds=float(stat["avg_response_seconds"] or 0)
        common_wrong=stat["most_common_wrong"] or "—"
        admin_lines.append(f"Q{int(stat['position'])+1}: {attempted_q} attempted, {correct_q} correct, {wrong_q} wrong, {accuracy_q:.1f}% accuracy, avg {avg_seconds:.1f}s, most common wrong option {common_wrong}")
    for row in answer_key:
        opts=row["options"] if isinstance(row["options"],dict) else json.loads(row["options"] or "{}")
        item=f"Q{int(row['position'])+1}. {row['correct_option']}. {opts.get(row['correct_option'],'')}"
        if row.get("explanation_en"): item += " — "+str(row["explanation_en"])[:220]
        if row.get("explanation_hi"): item += " / "+str(row["explanation_hi"])[:220]
        admin_lines.append(item)
    chunks=[]; buf=""
    for line in admin_lines:
        if len(buf)+len(line)+1>3500: chunks.append(buf); buf=""
        buf += ("\n" if buf else "")+line
    if buf: chunks.append(buf)
    if chunks:
        chunks[-1] += "\n\nContinue practising with @BpscNexustestbot: https://t.me/BpscNexustestbot"
    # Persist every report chunk so an admin can retrieve it later from the group button.
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM group_quiz_admin_reports WHERE session_id=%s",(sid,))
        for chunk_no,chunk in enumerate(chunks,1):
            await conn.execute("INSERT INTO group_quiz_admin_reports(session_id,chunk_no,report_text) VALUES(%s,%s,%s)",(sid,chunk_no,chunk))
    for chunk in chunks:
        try: await application.bot.send_message(chat_id=int(finished["started_by"]),text=chunk)
        except Exception:
            log.info("Could not DM full admin report for session %s; group report button remains available",sid,exc_info=True); break
    async with pool.connection() as conn:
        await conn.execute("UPDATE group_quiz_sessions SET report_sent=TRUE WHERE id=%s",(sid,))


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
        await asyncio.sleep(2)


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
