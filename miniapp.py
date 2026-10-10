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
        cur = await conn.execute("""SELECT a.*,q.title,q.subject,q.duration_minutes,q.positive_marks,q.negative_marks,q.question_time_seconds,q.shuffle_options
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
        cur = await conn.execute("""SELECT s.question_id AS id,s.correct_option,aa.selected_option
            FROM bank_quiz_attempt_question_snapshots s
            LEFT JOIN bank_quiz_attempt_answers aa ON aa.attempt_id=s.attempt_id AND aa.question_id=s.question_id
            WHERE s.attempt_id=%s ORDER BY s.position""", (attempt_id,))
        rows = await cur.fetchall()
        correct = sum(1 for r in rows if r["selected_option"] and r["selected_option"] == r["correct_option"])
        incorrect = sum(1 for r in rows if r["selected_option"] and r["selected_option"] != r["correct_option"])
        skipped = max(0, len(rows) - correct - incorrect)
        score = correct * float(attempt["positive_marks"]) - incorrect * float(attempt["negative_marks"])
        await conn.execute("""UPDATE bank_quiz_attempts SET submitted_at=NOW(),correct_count=%s,incorrect_count=%s,skipped_count=%s,score=%s
            WHERE id=%s AND telegram_id=%s AND submitted_at IS NULL""", (correct, incorrect, skipped, round(score, 2), attempt_id, telegram_id))
        return await _result_payload(conn, attempt_id, telegram_id)


async def _result_payload(conn, attempt_id: int, telegram_id: int):
    cur = await conn.execute("""SELECT a.id,a.quiz_id,a.mode,a.quiz_settings,a.total_questions,a.correct_count,a.incorrect_count,a.skipped_count,a.score,a.started_at,a.submitted_at,
        q.title,q.subject,q.positive_marks,q.negative_marks
        FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id WHERE a.id=%s AND a.telegram_id=%s""", (attempt_id, telegram_id))
    attempt = await cur.fetchone()
    if not attempt:
        return None
    cur = await conn.execute("""SELECT s.question_id AS id,s.subject,s.question_en,s.question_hi,s.options,s.correct_option,s.explanation_en,s.explanation_hi,aa.selected_option
        FROM bank_quiz_attempt_question_snapshots s
        LEFT JOIN bank_quiz_attempt_answers aa ON aa.attempt_id=s.attempt_id AND aa.question_id=s.question_id
        WHERE s.attempt_id=%s ORDER BY s.position""", (attempt_id,))
    review = []
    for r in await cur.fetchall():
        options = r["options"] if isinstance(r["options"], dict) else json.loads(r["options"] or "{}")
        review.append({"id": r["id"], "subject": r["subject"], "question_en": r["question_en"], "question_hi": r["question_hi"], "options": options,
                       "selected_option": r["selected_option"], "correct_option": r["correct_option"],
                       "explanation_en": r["explanation_en"], "explanation_hi": r["explanation_hi"],
                       "is_correct": bool(r["selected_option"] and r["selected_option"] == r["correct_option"])})
    attempted = int(attempt["correct_count"] or 0) + int(attempt["incorrect_count"] or 0)
    settings = attempt["quiz_settings"] if isinstance(attempt["quiz_settings"], dict) else json.loads(attempt["quiz_settings"] or "{}")
    mode_label = settings.get("mode_label", attempt["mode"])
    by_subject = {}
    for item in review:
        key = item.get("subject") or "Other / Unclassified"
        agg = by_subject.setdefault(key, {"subject": key, "total": 0, "correct": 0, "incorrect": 0, "skipped": 0})
        agg["total"] += 1
        if not item["selected_option"]:
            agg["skipped"] += 1
        elif item["is_correct"]:
            agg["correct"] += 1
        else:
            agg["incorrect"] += 1
    subject_breakdown = sorted(by_subject.values(), key=lambda x: (-x["total"], x["subject"]))
    return {"submitted": True, "attempt_id": int(attempt["id"]), "title": attempt["title"], "subject": attempt["subject"],
            "mode": attempt["mode"], "mode_label": mode_label, "total_questions": int(attempt["total_questions"]), "correct": int(attempt["correct_count"] or 0),
            "incorrect": int(attempt["incorrect_count"] or 0), "skipped": int(attempt["skipped_count"] or 0), "score": float(attempt["score"] or 0),
            "max_score": float(attempt["total_questions"]) * float(attempt["positive_marks"]),
            "accuracy": round(int(attempt["correct_count"] or 0) / attempted * 100, 1) if attempted else 0,
            "negative_marks": float(attempt["negative_marks"]), "subject_breakdown": subject_breakdown, "review": review}


async def index(request):
    # Resolve frontend relative to this file, not the process working directory.
    page = ROOT / "static" / "index.html"
    if not page.is_file():
        log.error("Mini App frontend missing at %s", page)
        return web.Response(
            text="BPSC Nexus Mini App frontend is missing. Ensure static/index.html is committed to the deployment repository.",
            status=500,
            content_type="text/plain",
        )
    response = web.FileResponse(page)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


async def static_asset(request):
    # Static assets are public; API routes remain protected by Telegram initData auth.
    asset_path = (ROOT / "static" / request.match_info["filename"]).resolve()
    static_root = (ROOT / "static").resolve()
    if static_root not in asset_path.parents or not asset_path.is_file():
        raise web.HTTPNotFound(text="Static asset not found")
    return web.FileResponse(asset_path)


async def health(request):
    return json_response({"status": "ok", "service": "bpsc-nexus-miniapp"})


async def list_quizzes(request):
    pool = request.app["pool"]
    user_id = request["telegram_user"]["id"]
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT q.id,q.title,q.description,q.subject,q.duration_minutes,q.positive_marks,q.negative_marks,
            q.question_time_seconds, q.shuffle_options, q.is_custom, COUNT(qq.question_id) AS total_questions FROM bank_quizzes q
            JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
            JOIN question_bank qb ON qb.id=qq.question_id AND qb.status='ready'
            WHERE q.is_published=TRUE AND (q.is_custom=FALSE OR q.custom_owner_telegram_id=%s)
            GROUP BY q.id ORDER BY q.is_custom ASC,q.id DESC LIMIT 100""", (user_id,))
        rows = await cur.fetchall()
    return json_response({"quizzes": [{"id": int(r["id"]), "title": r["title"], "description": r["description"], "subject": r["subject"],
        "duration_minutes": int(r["duration_minutes"]), "positive_marks": float(r["positive_marks"]), "negative_marks": float(r["negative_marks"]),
        "question_time_seconds": int(r["question_time_seconds"] or 0), "shuffle_options": bool(r["shuffle_options"]), "is_custom": bool(r["is_custom"]),
        "total_questions": int(r["total_questions"])} for r in rows]})


async def quiz_builder_meta(request):
    pool = request.app["pool"]
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT subject, COUNT(*) AS n FROM question_bank WHERE status='ready' GROUP BY subject ORDER BY subject")
        subjects = [{"name": r["subject"], "count": int(r["n"])} for r in await cur.fetchall()]
        cur = await conn.execute("SELECT DISTINCT topic FROM question_bank WHERE status='ready' AND NULLIF(BTRIM(topic),'') IS NOT NULL ORDER BY topic LIMIT 500")
        topics = [r["topic"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT COUNT(*) AS n FROM question_bank WHERE status='ready'")
        total = int((await cur.fetchone())["n"])
    return json_response({"subjects": subjects, "topics": topics, "total_ready": total,
        "selection_modes": ["random", "sequential", "unseen", "incorrect"]})


async def quiz_builder_count(request):
    user_id = request["telegram_user"]["id"]
    subject = str(request.query.get("subject", "All subjects")).strip()
    topic = str(request.query.get("topic", "")).strip()
    selection = str(request.query.get("selection", "random"))
    conditions = ["qb.status='ready'"]
    params = []
    if subject and subject != "All subjects":
        conditions.append("qb.subject=%s"); params.append(subject)
    if topic:
        conditions.append("qb.topic=%s"); params.append(topic)
    if selection == "unseen":
        conditions.append("NOT EXISTS (SELECT 1 FROM bank_quiz_attempt_answers aa JOIN bank_quiz_attempts a ON a.id=aa.attempt_id WHERE aa.question_id=qb.id AND a.telegram_id=%s)"); params.append(user_id)
    elif selection == "incorrect":
        conditions.append("EXISTS (SELECT 1 FROM bank_quiz_attempt_answers aa JOIN bank_quiz_attempts a ON a.id=aa.attempt_id JOIN bank_quiz_attempt_question_snapshots ss ON ss.attempt_id=aa.attempt_id AND ss.question_id=aa.question_id WHERE aa.question_id=qb.id AND a.telegram_id=%s AND aa.selected_option IS NOT NULL AND aa.selected_option<>ss.correct_option)"); params.append(user_id)
    async with request.app["pool"].connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM question_bank qb WHERE " + " AND ".join(conditions), tuple(params))
        n = int((await cur.fetchone())["n"])
    return json_response({"eligible_count": n})


async def create_custom_quiz(request):
    user = request["telegram_user"]
    pool = request.app["pool"]
    try:
        body = await request.json()
        subject = str(body.get("subject") or "All subjects").strip()
        topic = str(body.get("topic") or "").strip()
        count = int(body.get("count", 10))
        duration = int(body.get("duration_minutes", 20))
        question_time = int(body.get("question_time_seconds", 0))
        selection = str(body.get("selection", "random"))
        mode = str(body.get("mode", "practice"))
        positive = float(body.get("positive_marks", 1))
        negative = float(body.get("negative_marks", 1/3))
        title = str(body.get("title") or "My Custom Quiz").strip()[:100]
        shuffle_options = bool(body.get("shuffle_options", False))
        if not 1 <= count <= 100: raise ValueError("Question count must be between 1 and 100.")
        if not 1 <= duration <= 360: raise ValueError("Total duration must be between 1 and 360 minutes.")
        if question_time not in (0, 10, 15, 20, 30, 45, 60, 90, 120): raise ValueError("Choose a supported per-question timer.")
        if selection not in ("random", "sequential", "unseen", "incorrect"): raise ValueError("Invalid question selection method.")
        if mode not in ("practice", "exam", "revision", "rapidfire"): raise ValueError("Choose a valid attempt mode.")
        if not 0 <= negative <= 5 or not 0 < positive <= 10: raise ValueError("Invalid scoring settings.")
    except ValueError as exc:
        return json_response({"error": str(exc) or "Invalid quiz settings."}, 400)
    except Exception:
        return json_response({"error": "Invalid quiz builder settings."}, 400)

    conditions = ["qb.status='ready'"]
    params = []
    if subject != "All subjects":
        conditions.append("qb.subject=%s"); params.append(subject)
    if topic:
        conditions.append("qb.topic=%s"); params.append(topic)
    if selection == "unseen":
        conditions.append("NOT EXISTS (SELECT 1 FROM bank_quiz_attempt_answers aa JOIN bank_quiz_attempts a ON a.id=aa.attempt_id WHERE aa.question_id=qb.id AND a.telegram_id=%s)")
        params.append(user["id"])
    elif selection == "incorrect":
        conditions.append("EXISTS (SELECT 1 FROM bank_quiz_attempt_answers aa JOIN bank_quiz_attempts a ON a.id=aa.attempt_id JOIN bank_quiz_attempt_question_snapshots ss ON ss.attempt_id=aa.attempt_id AND ss.question_id=aa.question_id WHERE aa.question_id=qb.id AND a.telegram_id=%s AND aa.selected_option IS NOT NULL AND aa.selected_option<>ss.correct_option)")
        params.append(user["id"])
    order = "RANDOM()" if selection in ("random", "incorrect") else "qb.id ASC"
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM question_bank qb WHERE " + " AND ".join(conditions), tuple(params))
        eligible = int((await cur.fetchone())["n"])
        if eligible < count:
            return json_response({"error": f"Only {eligible} eligible questions match these filters. Reduce the count or change your filters.", "eligible_count": eligible}, 400)
        cur = await conn.execute("SELECT qb.id FROM question_bank qb WHERE " + " AND ".join(conditions) + f" ORDER BY {order} LIMIT %s", tuple(params + [count]))
        ids = [int(r["id"]) for r in await cur.fetchall()]
        display_subject = subject if subject != "All subjects" else "Mixed Subjects"
        title = title or "My Custom Quiz"
        cur = await conn.execute("""INSERT INTO bank_quizzes(title,description,subject,created_by_telegram_id,is_published,duration_minutes,positive_marks,negative_marks,is_custom,custom_owner_telegram_id,question_time_seconds,shuffle_options,published_at)
            VALUES(%s,%s,%s,%s,TRUE,%s,%s,%s,TRUE,%s,%s,%s,NOW()) RETURNING id""",
            (title, f"Custom {mode} quiz · {count} questions", display_subject, user["id"], duration, positive, negative, user["id"], question_time, shuffle_options))
        quiz_id = int((await cur.fetchone())["id"])
        for position, question_id in enumerate(ids):
            await conn.execute("INSERT INTO bank_quiz_questions(quiz_id,position,question_id) VALUES(%s,%s,%s)", (quiz_id, position, question_id))
    return json_response({"quiz_id": quiz_id, "title": title, "count": len(ids), "eligible_count": eligible, "mode": mode,
        "message": "Custom quiz created from verified question-bank records."}, 201)


async def start_attempt(request):
    user = request["telegram_user"]
    pool = request.app["pool"]
    try:
        body = await request.json()
        quiz_id = int(body.get("quiz_id"))
        requested_mode = body.get("mode")
        if requested_mode not in ("practice", "exam", "revision", "rapidfire"):
            raise ValueError()
        mode = "practice" if requested_mode in ("revision", "rapidfire") else requested_mode
    except Exception:
        return json_response({"error": "Provide a valid quiz_id and mode."}, 400)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id,title,duration_minutes,question_time_seconds,shuffle_options,is_custom,custom_owner_telegram_id FROM bank_quizzes WHERE id=%s AND is_published=TRUE AND (is_custom=FALSE OR custom_owner_telegram_id=%s)", (quiz_id, user["id"]))
        quiz = await cur.fetchone()
        if not quiz:
            return json_response({"error": "Quiz is unavailable."}, 404)
        cur = await conn.execute("""SELECT qb.id FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
            WHERE qq.quiz_id=%s AND qb.status='ready' ORDER BY qq.position""", (quiz_id,))
        ids = await cur.fetchall()
        if not ids:
            return json_response({"error": "This quiz has no verified questions."}, 400)
        cur = await conn.execute("INSERT INTO bank_quiz_attempts(quiz_id,telegram_id,mode,total_questions,question_time_seconds,quiz_settings) VALUES(%s,%s,%s,%s,%s,%s::jsonb) RETURNING id,started_at", (quiz_id, user["id"], mode, len(ids), int(quiz["question_time_seconds"] or 0), json.dumps({"custom": bool(quiz["is_custom"]), "mode_label": requested_mode})))
        attempt = await cur.fetchone()
        await conn.execute("""INSERT INTO bank_quiz_attempt_question_snapshots(attempt_id,position,question_id,subject,question_en,question_hi,options,correct_option,explanation_en,explanation_hi)
            SELECT %s,qq.position,qb.id,qb.subject,qb.question_en,qb.question_hi,qb.options,qb.correct_option, qb.explanation_en,qb.explanation_hi
            FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
            WHERE qq.quiz_id=%s AND qb.status='ready' ORDER BY qq.position""", (attempt["id"], quiz_id))
    return json_response({"attempt_id": int(attempt["id"]), "started_at": attempt["started_at"].isoformat(), "duration_minutes": int(quiz["duration_minutes"]), "question_time_seconds": int(quiz["question_time_seconds"] or 0), "shuffle_options": bool(quiz["shuffle_options"]), "mode": mode, "mode_label": requested_mode})


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
        cur = await conn.execute("""SELECT s.question_id AS id,s.question_en,s.question_hi,s.options,aa.selected_option,aa.is_correct,aa.is_marked_review,
            s.correct_option,s.explanation_en,s.explanation_hi
            FROM bank_quiz_attempt_question_snapshots s
            LEFT JOIN bank_quiz_attempt_answers aa ON aa.attempt_id=s.attempt_id AND aa.question_id=s.question_id
            WHERE s.attempt_id=%s ORDER BY s.position""", (attempt_id,))
        rows = await cur.fetchall()
    questions = []
    for r in rows:
        opts = r["options"] if isinstance(r["options"], dict) else json.loads(r["options"] or "{}")
        item = {"id": int(r["id"]), "question_en": r["question_en"], "question_hi": r["question_hi"], "options": opts, "selected_option": r["selected_option"], "marked_review": bool(r["is_marked_review"])}
        if attempt["mode"] in ("practice", "revision", "rapidfire") and r["selected_option"]:
            item["feedback"] = {"is_correct": bool(r["correct_option"] == r["selected_option"]), "correct_option": r["correct_option"],
                                 "explanation_en": r["explanation_en"], "explanation_hi": r["explanation_hi"]}
        questions.append(item)
    settings = attempt["quiz_settings"] if isinstance(attempt["quiz_settings"], dict) else json.loads(attempt["quiz_settings"] or "{}")
    return json_response({"submitted": False, "attempt_id": attempt_id, "title": attempt["title"], "subject": attempt["subject"], "mode": attempt["mode"], "mode_label": settings.get("mode_label", attempt["mode"]),
                          "duration_minutes": int(attempt["duration_minutes"]), "question_time_seconds": int(attempt["question_time_seconds"] or 0), "shuffle_options": bool(attempt["shuffle_options"]), "started_at": attempt["started_at"].isoformat(), "deadline": datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
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
        cur = await conn.execute("SELECT correct_option FROM bank_quiz_attempt_question_snapshots WHERE attempt_id=%s AND question_id=%s", (attempt_id, question_id))
        qrow = await cur.fetchone()
        if not qrow:
            return json_response({"error": "Question is not part of this attempt."}, 404)
        is_correct = (selected == qrow["correct_option"]) if selected and attempt["mode"] in ("practice", "revision", "rapidfire") else None
        await conn.execute("""INSERT INTO bank_quiz_attempt_answers(attempt_id,question_id,selected_option,is_correct)
            VALUES(%s,%s,%s,%s) ON CONFLICT(attempt_id,question_id) DO UPDATE SET selected_option=EXCLUDED.selected_option,is_correct=EXCLUDED.is_correct""",
            (attempt_id, question_id, selected, is_correct))
        if attempt["mode"] in ("practice", "revision", "rapidfire") and selected:
            cur = await conn.execute("SELECT correct_option,explanation_en,explanation_hi FROM bank_quiz_attempt_question_snapshots WHERE attempt_id=%s AND question_id=%s", (attempt_id, question_id))
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
        cur = await conn.execute("SELECT 1 FROM bank_quiz_attempt_question_snapshots WHERE attempt_id=%s AND question_id=%s", (attempt_id, question_id))
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
    app.router.add_get("/static/{filename}", static_asset)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/quizzes", list_quizzes)
    app.router.add_get("/api/quiz-builder/meta", quiz_builder_meta)
    app.router.add_get("/api/quiz-builder/count", quiz_builder_count)
    app.router.add_post("/api/quiz-builder/create", create_custom_quiz)
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
