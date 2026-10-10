"""BPSC Nexus personal CBT Mini App API + static frontend server.

Deploy as a separate Render Web Service from the Telegram bot. Shares DATABASE_URL.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import question_bank as qb

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("bpsc_nexus_miniapp")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PORT = int(os.environ.get("PORT", "10000"))
ROOT = Path(__file__).parent


def validate_init_data(init_data: str) -> dict:
    if not BOT_TOKEN or not init_data:
        raise ValueError("Open this app from the BPSC Nexus Telegram bot.")
    pairs = parse_qsl(init_data, keep_blank_values=True)
    values = dict(pairs)
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise ValueError("Telegram launch data is missing its signature.")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise ValueError("Telegram launch data signature is invalid.")
    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError:
        raise ValueError("Telegram launch timestamp is invalid.")
    now = int(time.time())
    if auth_date <= 0 or now - auth_date > 86400 or auth_date > now + 60:
        raise ValueError("Telegram launch data has expired or has an invalid timestamp. Close and reopen the Mini App.")
    try:
        user = json.loads(values["user"])
        telegram_id = int(user["id"])
    except Exception:
        raise ValueError("Telegram user data is missing.")
    return {"id": telegram_id, "first_name": str(user.get("first_name", "Student")), "username": user.get("username")}


@web.middleware
async def telegram_auth(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    try:
        request["telegram_user"] = validate_init_data(init_data)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=401)
    return await handler(request)


def json_response(payload, status=200):
    return web.json_response(payload, status=status, dumps=lambda x: json.dumps(x, ensure_ascii=False, default=str))


async def _attempt_row(pool, attempt_id: int, telegram_id: int):
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT a.*,q.title,q.subject,q.duration_minutes,q.positive_marks,q.negative_marks
            FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id
            WHERE a.id=%s AND a.telegram_id=%s""", (attempt_id, telegram_id))
        return await cur.fetchone()


async def _grade_attempt(pool, attempt_id: int, telegram_id: int):
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT a.id,a.quiz_id,a.mode,a.total_questions,a.submitted_at,q.positive_marks,q.negative_marks
            FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id
            WHERE a.id=%s AND a.telegram_id=%s FOR UPDATE OF a""", (attempt_id, telegram_id))
        attempt = await cur.fetchone()
        if not attempt:
            return None
        if attempt["submitted_at"]:
            return await _result_payload(conn, attempt_id, telegram_id)
        cur = await conn.execute("""SELECT qb.id,qb.correct_option,aa.selected_option
            FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
            LEFT JOIN bank_quiz_attempt_answers aa ON aa.attempt_id=%s AND aa.question_id=qb.id
            WHERE qq.quiz_id=%s ORDER BY qq.position""", (attempt_id, attempt["quiz_id"]))
        rows = await cur.fetchall()
        correct = sum(1 for r in rows if r["selected_option"] and r["selected_option"] == r["correct_option"])
        incorrect = sum(1 for r in rows if r["selected_option"] and r["selected_option"] != r["correct_option"])
        skipped = max(0, len(rows) - correct - incorrect)
        score = correct * float(attempt["positive_marks"]) - incorrect * float(attempt["negative_marks"])
        await conn.execute("""UPDATE bank_quiz_attempts SET submitted_at=NOW(),correct_count=%s,incorrect_count=%s,skipped_count=%s,score=%s
            WHERE id=%s AND telegram_id=%s AND submitted_at IS NULL""", (correct, incorrect, skipped, round(score, 2), attempt_id, telegram_id))
        return await _result_payload(conn, attempt_id, telegram_id)


async def _result_payload(conn, attempt_id: int, telegram_id: int):
    cur = await conn.execute("""SELECT a.id,a.quiz_id,a.mode,a.total_questions,a.correct_count,a.incorrect_count,a.skipped_count,a.score,a.started_at,a.submitted_at,
        q.title,q.subject,q.positive_marks,q.negative_marks
        FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id WHERE a.id=%s AND a.telegram_id=%s""", (attempt_id, telegram_id))
    attempt = await cur.fetchone()
    if not attempt:
        return None
    cur = await conn.execute("""SELECT qb.id,qb.question_en,qb.question_hi,qb.options,qb.correct_option,qb.explanation_en,qb.explanation_hi,aa.selected_option
        FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
        LEFT JOIN bank_quiz_attempt_answers aa ON aa.attempt_id=%s AND aa.question_id=qb.id
        WHERE qq.quiz_id=%s ORDER BY qq.position""", (attempt_id, attempt["quiz_id"]))
    review = []
    for r in await cur.fetchall():
        options = r["options"] if isinstance(r["options"], dict) else json.loads(r["options"] or "{}")
        review.append({"id": r["id"], "question_en": r["question_en"], "question_hi": r["question_hi"], "options": options,
                       "selected_option": r["selected_option"], "correct_option": r["correct_option"],
                       "explanation_en": r["explanation_en"], "explanation_hi": r["explanation_hi"],
                       "is_correct": bool(r["selected_option"] and r["selected_option"] == r["correct_option"])})
    attempted = int(attempt["correct_count"] or 0) + int(attempt["incorrect_count"] or 0)
    return {"submitted": True, "attempt_id": int(attempt["id"]), "title": attempt["title"], "subject": attempt["subject"],
            "mode": attempt["mode"], "total_questions": int(attempt["total_questions"]), "correct": int(attempt["correct_count"] or 0),
            "incorrect": int(attempt["incorrect_count"] or 0), "skipped": int(attempt["skipped_count"] or 0), "score": float(attempt["score"] or 0),
            "max_score": float(attempt["total_questions"]) * float(attempt["positive_marks"]),
            "accuracy": round(int(attempt["correct_count"] or 0) / attempted * 100, 1) if attempted else 0,
            "negative_marks": float(attempt["negative_marks"]), "review": review}


async def index(request):
    return web.FileResponse(ROOT / "static" / "index.html")


async def health(request):
    return json_response({"status": "ok", "service": "bpsc-nexus-miniapp"})


async def list_quizzes(request):
    pool = request.app["pool"]
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT q.id,q.title,q.description,q.subject,q.duration_minutes,q.positive_marks,q.negative_marks,
            COUNT(qq.question_id) AS total_questions FROM bank_quizzes q
            JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
            JOIN question_bank qb ON qb.id=qq.question_id AND qb.status='ready'
            WHERE q.is_published=TRUE GROUP BY q.id ORDER BY q.id DESC LIMIT 100""")
        rows = await cur.fetchall()
    return json_response({"quizzes": [{"id": int(r["id"]), "title": r["title"], "description": r["description"], "subject": r["subject"],
        "duration_minutes": int(r["duration_minutes"]), "positive_marks": float(r["positive_marks"]), "negative_marks": float(r["negative_marks"]),
        "total_questions": int(r["total_questions"])} for r in rows]})


async def start_attempt(request):
    user = request["telegram_user"]
    pool = request.app["pool"]
    try:
        body = await request.json()
        quiz_id = int(body.get("quiz_id"))
        mode = body.get("mode")
        if mode not in ("practice", "exam"):
            raise ValueError()
    except Exception:
        return json_response({"error": "Provide a valid quiz_id and mode."}, 400)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id,title,duration_minutes FROM bank_quizzes WHERE id=%s AND is_published=TRUE", (quiz_id,))
        quiz = await cur.fetchone()
        if not quiz:
            return json_response({"error": "Quiz is unavailable."}, 404)
        cur = await conn.execute("""SELECT qb.id FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
            WHERE qq.quiz_id=%s AND qb.status='ready' ORDER BY qq.position""", (quiz_id,))
        ids = await cur.fetchall()
        if not ids:
            return json_response({"error": "This quiz has no verified questions."}, 400)
        cur = await conn.execute("INSERT INTO bank_quiz_attempts(quiz_id,telegram_id,mode,total_questions) VALUES(%s,%s,%s,%s) RETURNING id,started_at", (quiz_id, user["id"], mode, len(ids)))
        attempt = await cur.fetchone()
    return json_response({"attempt_id": int(attempt["id"]), "started_at": attempt["started_at"].isoformat(), "duration_minutes": int(quiz["duration_minutes"]), "mode": mode})


async def get_attempt(request):
    user = request["telegram_user"]
    pool = request.app["pool"]
    try:
        attempt_id = int(request.match_info["attempt_id"])
    except ValueError:
        return json_response({"error": "Invalid attempt ID."}, 400)
    attempt = await _attempt_row(pool, attempt_id, user["id"])
    if not attempt:
        return json_response({"error": "Attempt not found."}, 404)
    if attempt["submitted_at"]:
        async with pool.connection() as conn:
            result = await _result_payload(conn, attempt_id, user["id"])
        return json_response(result)
    deadline = attempt["started_at"].timestamp() + int(attempt["duration_minutes"]) * 60
    if attempt["mode"] == "exam" and time.time() >= deadline:
        result = await _grade_attempt(pool, attempt_id, user["id"])
        return json_response(result)
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT qb.id,qb.question_en,qb.question_hi,qb.options,aa.selected_option,aa.is_correct,aa.is_marked_review
            FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
            LEFT JOIN bank_quiz_attempt_answers aa ON aa.attempt_id=%s AND aa.question_id=qb.id
            WHERE qq.quiz_id=%s AND qb.status='ready' ORDER BY qq.position""", (attempt_id, attempt["quiz_id"]))
        rows = await cur.fetchall()
    questions = []
    for r in rows:
        opts = r["options"] if isinstance(r["options"], dict) else json.loads(r["options"] or "{}")
        item = {"id": int(r["id"]), "question_en": r["question_en"], "question_hi": r["question_hi"], "options": opts, "selected_option": r["selected_option"], "marked_review": bool(r["is_marked_review"])}
        if attempt["mode"] == "practice" and r["selected_option"]:
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT correct_option,explanation_en,explanation_hi FROM question_bank WHERE id=%s", (r["id"],))
                ans = await cur.fetchone()
            item["feedback"] = {"is_correct": bool(ans and ans["correct_option"] == r["selected_option"]), "correct_option": ans["correct_option"] if ans else None,
                                 "explanation_en": ans["explanation_en"] if ans else None, "explanation_hi": ans["explanation_hi"] if ans else None}
        questions.append(item)
    return json_response({"submitted": False, "attempt_id": attempt_id, "title": attempt["title"], "subject": attempt["subject"], "mode": attempt["mode"],
                          "duration_minutes": int(attempt["duration_minutes"]), "started_at": attempt["started_at"].isoformat(), "deadline": datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
                          "positive_marks": float(attempt["positive_marks"]), "negative_marks": float(attempt["negative_marks"]), "questions": questions})


async def save_answer(request):
    user = request["telegram_user"]
    pool = request.app["pool"]
    try:
        attempt_id = int(request.match_info["attempt_id"])
        body = await request.json()
        question_id = int(body.get("question_id"))
        selected = body.get("selected_option")
        if selected is not None and selected not in "ABCDE":
            raise ValueError()
    except Exception:
        return json_response({"error": "Invalid answer payload."}, 400)
    attempt = await _attempt_row(pool, attempt_id, user["id"])
    if not attempt:
        return json_response({"error": "Attempt not found."}, 404)
    if attempt["submitted_at"]:
        return json_response({"error": "This attempt is already submitted."}, 409)
    if attempt["mode"] == "exam" and time.time() >= attempt["started_at"].timestamp() + int(attempt["duration_minutes"]) * 60:
        result = await _grade_attempt(pool, attempt_id, user["id"])
        return json_response({"error": "Time is up.", "result": result}, 409)
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT qb.correct_option FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
            WHERE qq.quiz_id=%s AND qb.id=%s AND qb.status='ready'""", (attempt["quiz_id"], question_id))
        qrow = await cur.fetchone()
        if not qrow:
            return json_response({"error": "Question is not part of this attempt."}, 404)
        is_correct = (selected == qrow["correct_option"]) if selected and attempt["mode"] == "practice" else None
        await conn.execute("""INSERT INTO bank_quiz_attempt_answers(attempt_id,question_id,selected_option,is_correct)
            VALUES(%s,%s,%s,%s) ON CONFLICT(attempt_id,question_id) DO UPDATE SET selected_option=EXCLUDED.selected_option,is_correct=EXCLUDED.is_correct""",
            (attempt_id, question_id, selected, is_correct))
        if attempt["mode"] == "practice" and selected:
            cur = await conn.execute("SELECT correct_option,explanation_en,explanation_hi FROM question_bank WHERE id=%s", (question_id,))
            ans = await cur.fetchone()
            feedback = {"is_correct": bool(ans["correct_option"] == selected), "correct_option": ans["correct_option"], "explanation_en": ans["explanation_en"], "explanation_hi": ans["explanation_hi"]}
        else:
            feedback = None
    return json_response({"saved": True, "feedback": feedback})


async def save_review(request):
    user = request["telegram_user"]
    pool = request.app["pool"]
    try:
        attempt_id = int(request.match_info["attempt_id"])
        body = await request.json()
        question_id = int(body.get("question_id"))
        marked = bool(body.get("marked_for_review"))
    except Exception:
        return json_response({"error": "Invalid review payload."}, 400)
    attempt = await _attempt_row(pool, attempt_id, user["id"])
    if not attempt:
        return json_response({"error": "Attempt not found."}, 404)
    if attempt["submitted_at"]:
        return json_response({"error": "This attempt is already submitted."}, 409)
    if attempt["mode"] == "exam" and time.time() >= attempt["started_at"].timestamp() + int(attempt["duration_minutes"]) * 60:
        result = await _grade_attempt(pool, attempt_id, user["id"])
        return json_response({"error": "Time is up.", "result": result}, 409)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM bank_quiz_questions WHERE quiz_id=%s AND question_id=%s", (attempt["quiz_id"], question_id))
        if not await cur.fetchone():
            return json_response({"error": "Question is not part of this attempt."}, 404)
        await conn.execute("""INSERT INTO bank_quiz_attempt_answers(attempt_id,question_id,is_marked_review)
            VALUES(%s,%s,%s) ON CONFLICT(attempt_id,question_id) DO UPDATE SET is_marked_review=EXCLUDED.is_marked_review""",
            (attempt_id, question_id, marked))
    return json_response({"saved": True, "marked_for_review": marked})


async def submit_attempt(request):
    user = request["telegram_user"]
    try:
        attempt_id = int(request.match_info["attempt_id"])
    except ValueError:
        return json_response({"error": "Invalid attempt ID."}, 400)
    result = await _grade_attempt(request.app["pool"], attempt_id, user["id"])
    if result is None:
        return json_response({"error": "Attempt not found."}, 404)
    return json_response(result)


async def startup(app):
    if not BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError("Set BOT_TOKEN and DATABASE_URL in the Render environment.")
    pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, open=False, kwargs={"row_factory": dict_row, "connect_timeout": 10})
    await pool.open(); await pool.wait()
    await qb.ensure_schema(pool)
    app["pool"] = pool
    log.info("Mini App API connected; question bank schema verified.")


async def cleanup(app):
    pool = app.get("pool")
    if pool:
        await pool.close()


def make_app():
    app = web.Application(middlewares=[telegram_auth], client_max_size=1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/quizzes", list_quizzes)
    app.router.add_post("/api/attempts", start_attempt)
    app.router.add_get("/api/attempts/{attempt_id}", get_attempt)
    app.router.add_post("/api/attempts/{attempt_id}/answer", save_answer)
    app.router.add_post("/api/attempts/{attempt_id}/review", save_review)
    app.router.add_post("/api/attempts/{attempt_id}/submit", submit_attempt)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
