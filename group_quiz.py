"""Persistent sequential Telegram group quiz engine for BPSC Nexus."""
from __future__ import annotations

import asyncio
import json
import logging
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
        current_index INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','failed','paused','cancelled')),
        next_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ
    )""",
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
    "CREATE INDEX IF NOT EXISTS idx_group_quiz_answers_session ON group_quiz_answers(session_id,telegram_id)",
]


async def ensure_schema(pool):
    async with pool.connection() as conn:
        for stmt in DDL:
            await conn.execute(stmt)


async def _is_chat_admin(context, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        log.exception("Could not verify group admin status")
        return False


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
    if not await _is_chat_admin(context, chat.id, user.id):
        await message.reply_text("Only this group's administrators can start a group quiz.")
        return
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT q.id,q.title,q.subject,COUNT(qq.question_id) AS n
            FROM bank_quizzes q JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
            JOIN question_bank b ON b.id=qq.question_id AND b.status='ready'
            WHERE q.is_published=TRUE GROUP BY q.id ORDER BY q.id DESC LIMIT 20""")
        quizzes = await cur.fetchall()
    if not quizzes:
        await message.reply_text("No published quizzes with verified questions are available yet. Build and publish a quiz from the private admin panel first.")
        return
    buttons = [[InlineKeyboardButton(f"#{r['id']} · {r['title'][:35]} ({r['n']} Q)", callback_data=f"gq:quiz:{r['id']}")] for r in quizzes]
    await message.reply_text("🧩 **BPSC Nexus Group Quiz**\n\nChoose a published quiz. Questions will be sent one poll at a time; the poll interval is also the voting window.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, pool) -> bool:
    q = update.callback_query
    if not q or not (q.data or "").startswith("gq:"):
        return False
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type == ChatType.PRIVATE:
        await q.answer("Use group quiz controls inside the destination group.", show_alert=True)
        return True
    if not await _is_chat_admin(context, chat.id, user.id):
        await q.answer("Only this group's administrators can control its quiz.", show_alert=True)
        return True
    data = q.data
    if data.startswith("gq:quiz:"):
        quiz_id = int(data.rsplit(":", 1)[1])
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT id,title FROM bank_quizzes WHERE id=%s AND is_published=TRUE", (quiz_id,))
            quiz = await cur.fetchone()
        if not quiz:
            await q.answer("Quiz is not published.", show_alert=True)
            return True
        context.user_data["group_quiz_pending"] = {"quiz_id": quiz_id, "chat_id": chat.id, "chat_title": chat.title or "Telegram group"}
        await q.answer()
        await q.edit_message_text(f"Selected: {quiz['title']}\n\nChoose the voting window and interval. Each poll will close before the next question is posted.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("15 seconds", callback_data="gq:interval:15"), InlineKeyboardButton("30 seconds", callback_data="gq:interval:30")],
            [InlineKeyboardButton("1 minute", callback_data="gq:interval:60"), InlineKeyboardButton("2 minutes", callback_data="gq:interval:120")],
            [InlineKeyboardButton("5 minutes", callback_data="gq:interval:300")],
            [InlineKeyboardButton("Cancel", callback_data="gq:cancel")],
        ]))
        return True
    if data == "gq:cancel":
        context.user_data.pop("group_quiz_pending", None)
        await q.answer("Cancelled.")
        await q.edit_message_text("Group quiz setup cancelled.")
        return True
    if data.startswith("gq:interval:"):
        interval = int(data.rsplit(":", 1)[1])
        pending = context.user_data.get("group_quiz_pending")
        if not pending or pending.get("chat_id") != chat.id or interval not in (15, 30, 60, 120, 300):
            await q.answer("Setup expired. Run /groupquiz again.", show_alert=True)
            return True
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT id,title,subject,duration_minutes,positive_marks,negative_marks FROM bank_quizzes WHERE id=%s AND is_published=TRUE", (pending["quiz_id"],))
            quiz = await cur.fetchone()
            if not quiz:
                await q.answer("Quiz is no longer available.", show_alert=True)
                return True
            cur = await conn.execute("""SELECT b.id,b.question_en,b.question_hi,b.options,b.correct_option,b.explanation_en,b.explanation_hi
                FROM bank_quiz_questions qq JOIN question_bank b ON b.id=qq.question_id
                WHERE qq.quiz_id=%s AND b.status='ready' ORDER BY qq.position""", (quiz["id"],))
            source_questions = await cur.fetchall()
            if not source_questions:
                await q.answer("This quiz has no verified questions.", show_alert=True)
                return True
            prepared = []
            for row in source_questions:
                opts = row["options"] if isinstance(row["options"], dict) else json.loads(row["options"] or "{}")
                letters = []
                for letter in "ABCDE":
                    value = str(opts.get(letter, "")).strip()
                    if not value:
                        continue
                    if letter == "E" and (value.lower().startswith("not attempted") or value.startswith("प्रयास नहीं किया")):
                        continue
                    letters.append(letter)
                if len(letters) < 2 or row["correct_option"] not in letters:
                    continue
                prepared.append((row, opts, letters))
            if not prepared:
                await q.answer("No questions have valid answer options for Telegram polls.", show_alert=True)
                return True
            cur = await conn.execute("SELECT id FROM group_quiz_sessions WHERE chat_id=%s AND status='active' LIMIT 1", (chat.id,))
            active = await cur.fetchone()
            if active:
                await q.answer(f"This group already has active quiz session #{active['id']}.", show_alert=True)
                return True
            cur = await conn.execute("""INSERT INTO group_quiz_sessions(chat_id,chat_title,quiz_id,quiz_title,started_by,interval_seconds,questions_count,current_index,status,next_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,0,'active',NOW()) RETURNING id""", (chat.id, pending["chat_title"], quiz["id"], quiz["title"], user.id, interval, len(prepared)))
            session_id = int((await cur.fetchone())["id"])
            for position, (row, opts, letters) in enumerate(prepared):
                await conn.execute("""INSERT INTO group_quiz_session_questions(session_id,position,question_id,question_en,question_hi,options,option_letters,correct_option,explanation_en,explanation_hi)
                    VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)""", (session_id, position, row["id"], row["question_en"], row["question_hi"], json.dumps(opts, ensure_ascii=False), json.dumps(letters), row["correct_option"], row["explanation_en"], row["explanation_hi"]))
        context.user_data.pop("group_quiz_pending", None)
        await q.answer("Quiz scheduled.")
        await q.edit_message_text(f"✅ Group quiz #{session_id} queued.\n\n{quiz['title']}\nQuestions: {len(prepared)}\nVoting window: {interval} seconds per question\n\nThe first poll will be posted automatically.")
        return True
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
    opts = question["options"] if isinstance(question["options"], dict) else json.loads(question["options"] or "{}")
    letters = question["option_letters"] if isinstance(question["option_letters"], list) else json.loads(question["option_letters"] or "[]")
    question_text = str(question["question_en"] or "Question")
    if question.get("question_hi"):
        question_text += "\n" + str(question["question_hi"])
    poll_options = [str(opts.get(letter, letter))[:100] for letter in letters]
    explanation = "\n".join(x for x in (question.get("explanation_en"), question.get("explanation_hi")) if x)
    try:
        poll = await application.bot.send_poll(
            chat_id=session["chat_id"], question=question_text[:300], options=poll_options,
            type="quiz", correct_option_id=letters.index(question["correct_option"]),
            is_anonymous=False, explanation=explanation[:200] if explanation else None,
            open_period=int(session["interval_seconds"]),
        )
        async with pool.connection() as conn:
            await conn.execute("UPDATE group_quiz_session_questions SET poll_id=%s,message_id=%s,status='open',updated_at=NOW() WHERE session_id=%s AND position=%s", (str(poll.poll.id), int(poll.message_id), sid, position))
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
    sid = int(session["id"])
    async with pool.connection() as conn:
        cur = await conn.execute("UPDATE group_quiz_sessions SET status='completed',completed_at=NOW() WHERE id=%s AND status='active' RETURNING questions_count,quiz_title", (sid,))
        finished = await cur.fetchone()
        if not finished:
            return
        cur = await conn.execute("""SELECT a.telegram_id,a.first_name,a.username,
            COUNT(*) FILTER (WHERE a.selected_letter=q.correct_option) AS correct,
            COUNT(*) FILTER (WHERE a.selected_letter IS NOT NULL AND a.selected_letter<>q.correct_option) AS incorrect,
            COUNT(*) FILTER (WHERE a.selected_letter IS NOT NULL) AS attempted
            FROM group_quiz_answers a JOIN group_quiz_session_questions q USING(session_id,position)
            WHERE a.session_id=%s GROUP BY a.telegram_id,a.first_name,a.username
            ORDER BY correct DESC,incorrect ASC,attempted DESC,a.telegram_id ASC LIMIT 20""", (sid,))
        scores = await conn.fetchall()
        cur = await conn.execute("""SELECT COUNT(DISTINCT telegram_id) AS participants,
            COUNT(*) FILTER (WHERE selected_letter IS NOT NULL) AS votes,
            COUNT(*) FILTER (WHERE selected_letter=q.correct_option) AS correct,
            COUNT(*) FILTER (WHERE selected_letter IS NOT NULL AND selected_letter<>q.correct_option) AS incorrect
            FROM group_quiz_answers a JOIN group_quiz_session_questions q USING(session_id,position)
            WHERE a.session_id=%s""", (sid,))
        agg = await cur.fetchone()
    lines = [f"🏁 Group Quiz Complete · #{sid}", str(finished["quiz_title"]), "", f"Questions: {finished['questions_count']}", f"Participants: {agg['participants']}", f"Votes recorded: {agg['votes']}", f"Correct votes: {agg['correct']}", f"Incorrect votes: {agg['incorrect']}", "", "🏆 Leaderboard (1 mark correct, −⅓ incorrect)"]
    if not scores:
        lines.append("No participant answers were recorded.")
    for rank, row in enumerate(scores[:10], 1):
        name = (row["first_name"] or "Student").replace("\n", " ")[:35]
        attempted = int(row["attempted"] or 0)
        skipped = max(0, int(finished["questions_count"]) - attempted)
        correct = int(row["correct"] or 0); incorrect = int(row["incorrect"] or 0)
        score = correct - incorrect / 3
        lines.append(f"{rank}. {name} — {score:.2f} pts · {correct}✓ {incorrect}✗ {skipped} skipped")
    try:
        await application.bot.send_message(chat_id=session["chat_id"], text="\n".join(lines)[:4000])
    except Exception:
        log.exception("Could not publish group quiz results for %s", sid)


async def scheduler(application, pool):
    """Persisted scheduler. Poll state and due times survive service restarts."""
    while True:
        try:
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT * FROM group_quiz_sessions WHERE status='active' AND next_at<=NOW() ORDER BY next_at,id LIMIT 10")
                sessions = await cur.fetchall()
            for session in sessions:
                sid = int(session["id"]); position = int(session["current_index"])
                if position >= int(session["questions_count"]):
                    await _finish_session(application, pool, session)
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
        cur = await conn.execute("SELECT session_id,position,option_letters FROM group_quiz_session_questions WHERE poll_id=%s", (str(pa.poll_id),))
        row = await cur.fetchone()
        if not row:
            return
        letters = row["option_letters"] if isinstance(row["option_letters"], list) else json.loads(row["option_letters"] or "[]")
        selected = letters[pa.option_ids[0]] if pa.option_ids and pa.option_ids[0] < len(letters) else None
        await conn.execute("""INSERT INTO group_quiz_answers(session_id,position,telegram_id,first_name,username,selected_letter)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(session_id,position,telegram_id) DO UPDATE
            SET first_name=EXCLUDED.first_name,username=EXCLUDED.username,selected_letter=EXCLUDED.selected_letter,answered_at=NOW()""",
            (row["session_id"], row["position"], pa.user.id, pa.user.first_name, pa.user.username, selected))
