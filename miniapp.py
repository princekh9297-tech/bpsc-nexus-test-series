"""BPSC Nexus personal CBT Mini App API + static frontend server.

Deploy as a separate Render Web Service from the Telegram bot. Shares DATABASE_URL.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import question_bank as qb
import leaderboard as lb

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("bpsc_nexus_miniapp")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PORT = int(os.environ.get("PORT", "10000"))
ADMIN_TELEGRAM_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip().isdigit()}
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
        cur = await conn.execute("""SELECT a.*,q.title,q.subject,q.duration_minutes,q.positive_marks,q.negative_marks,q.question_time_seconds,q.shuffle_options,q.is_custom
            FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id
            WHERE a.id=%s AND a.telegram_id=%s""", (attempt_id, telegram_id))
        return await cur.fetchone()


async def _grade_attempt(pool, attempt_id: int, telegram_id: int, display_name: str = "Student"):
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
        await lb.record_bank_attempt(conn, attempt_id, telegram_id, display_name)
        # Persist this student's wrong/skipped questions in their private revision box.
        await ensure_revision_schema(conn)
        for row in rows:
            selected=row["selected_option"]
            reason="skipped" if not selected else ("wrong" if selected != row["correct_option"] else None)
            if reason:
                await conn.execute("""INSERT INTO bpsc_nexus_revision_items(telegram_id,question_id,reason)
                    VALUES(%s,%s,%s) ON CONFLICT(telegram_id,question_id,reason) DO UPDATE SET last_saved_at=NOW(),review_count=bpsc_nexus_revision_items.review_count+1,is_resolved=FALSE""",(telegram_id,row["id"],reason))
        return await _result_payload(conn, attempt_id, telegram_id)


async def _result_payload(conn, attempt_id: int, telegram_id: int):
    cur = await conn.execute("""SELECT a.id,a.quiz_id,a.mode,a.quiz_settings,a.total_questions,a.correct_count,a.incorrect_count,a.skipped_count,a.score,a.started_at,a.submitted_at,
        q.title,q.subject,q.positive_marks,q.negative_marks
        FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id WHERE a.id=%s AND a.telegram_id=%s""", (attempt_id, telegram_id))
    attempt = await cur.fetchone()
    if not attempt:
        return None
    cur = await conn.execute("""SELECT s.question_id AS id,s.subject,s.question_en,s.question_hi,s.question_type,s.question_structure,s.question_flags,s.options,s.correct_option,s.explanation_en,s.explanation_hi,aa.selected_option
        FROM bank_quiz_attempt_question_snapshots s
        LEFT JOIN bank_quiz_attempt_answers aa ON aa.attempt_id=s.attempt_id AND aa.question_id=s.question_id
        WHERE s.attempt_id=%s ORDER BY s.position""", (attempt_id,))
    review = []
    for r in await cur.fetchall():
        options = r["options"] if isinstance(r["options"], dict) else json.loads(r["options"] or "{}")
        review.append({"id": r["id"], "subject": r["subject"], "question_en": r["question_en"], "question_hi": r["question_hi"], "question_type": r["question_type"],
                       "question_structure": r["question_structure"] if isinstance(r["question_structure"], dict) else json.loads(r["question_structure"] or "{}"),
                       "question_flags": r["question_flags"] if isinstance(r["question_flags"], dict) else json.loads(r["question_flags"] or "{}"), "options": options,
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
    return {"submitted": True, "attempt_id": int(attempt["id"]), "quiz_id": int(attempt["quiz_id"]), "title": attempt["title"], "subject": attempt["subject"],
            "mode": attempt["mode"], "mode_label": mode_label, "total_questions": int(attempt["total_questions"]), "correct": int(attempt["correct_count"] or 0),
            "incorrect": int(attempt["incorrect_count"] or 0), "skipped": int(attempt["skipped_count"] or 0), "score": float(attempt["score"] or 0),
            "max_score": float(attempt["total_questions"]) * float(attempt["positive_marks"]),
            "accuracy": round(int(attempt["correct_count"] or 0) / attempted * 100, 1) if attempted else 0,
            "negative_marks": float(attempt["negative_marks"]), "subject_breakdown": subject_breakdown, "review": review}



def _utc_period(category: str, period: str | None):
    now=datetime.now(timezone.utc)
    if category=="weekly":
        iso=now.isocalendar(); value=period or f"{iso.year}-W{iso.week:02d}"
        if not re.fullmatch(r"\d{4}-W\d{2}",value): raise ValueError("Use an ISO week such as 2026-W41.")
        try: datetime.fromisocalendar(int(value[:4]),int(value[-2:]),1)
        except ValueError: raise ValueError("Invalid ISO week.")
        return value
    if category=="monthly":
        value=period or now.strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-\d{2}",value) or not 1<=int(value[-2:])<=12: raise ValueError("Use a month such as 2026-10.")
        return value
    return "all"

def _leaderboard_row(r):
    return {"rank":int(r["rank"]),"display_name":r["display_name"] or "Student","points":round(float(r["normalized_points"] or 0),2),
      "correct":int(r["correct_count"] or 0),"incorrect":int(r["incorrect_count"] or 0),"skipped":int(r["skipped_count"] or 0),
      "accuracy":round(float(r["accuracy"] or 0),1),"time_seconds":int(r.get("total_time_seconds",r.get("time_taken_seconds",0)) or 0),"attempts":int(r.get("attempts_count",1) or 1)}

async def leaderboard_data(request):
    user_id=request["telegram_user"]["id"]; category=request.query.get("category","weekly").lower(); subject=request.query.get("subject","").strip()[:120]
    if category not in {"weekly","monthly","daily","subject","lifetime"}: return json_response({"error":"Unknown leaderboard category."},400)
    try: limit=max(1,min(100,int(request.query.get("limit","100"))))
    except ValueError: return json_response({"error":"Invalid page size."},400)
    pool=request.app["pool"]
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT DISTINCT subject FROM bpsc_leaderboard_period_totals WHERE period_type='lifetime' AND period_key='all' AND subject<>'All subjects' ORDER BY subject")
        subjects=[r["subject"] for r in await cur.fetchall()]
        if category=="daily":
            raw_date=request.query.get("period") or datetime.now(timezone.utc).date().isoformat()
            try: datetime.strptime(raw_date,"%Y-%m-%d")
            except ValueError: return json_response({"error":"Use a date such as 2026-10-12."},400)
            cur=await conn.execute("SELECT id,quiz_id,title FROM bpsc_leaderboard_daily_challenges WHERE challenge_date=%s AND active=TRUE",(raw_date,)); challenge=await cur.fetchone()
            if not challenge: return json_response({"category":category,"period":raw_date,"challenge":None,"subjects":subjects,"entries":[],"my_rank":None,"is_admin":user_id in ADMIN_TELEGRAM_IDS,"message":"No official daily challenge is configured for this date yet."})
            cur=await conn.execute("""WITH best AS (
              SELECT DISTINCT ON(a.telegram_id) a.telegram_id,a.display_name,a.normalized_points,a.correct_count,a.incorrect_count,a.skipped_count,a.accuracy,a.time_taken_seconds,a.submitted_at,a.quiz_title
              FROM bpsc_global_leaderboard_attempts a WHERE a.quiz_id=%s AND a.eligible=TRUE AND a.validation_status='verified'
              AND a.submitted_at >= (%s::date::timestamp AT TIME ZONE 'UTC') AND a.submitted_at < ((%s::date+1)::timestamp AT TIME ZONE 'UTC')
              ORDER BY a.telegram_id,a.normalized_points DESC,a.accuracy DESC,a.time_taken_seconds ASC,a.submitted_at ASC
            ), ranked AS (SELECT ROW_NUMBER() OVER(ORDER BY normalized_points DESC,accuracy DESC,time_taken_seconds ASC,submitted_at ASC,telegram_id ASC) rank,* FROM best)
            SELECT * FROM ranked WHERE rank<=%s OR telegram_id=%s ORDER BY rank""",(challenge["quiz_id"],raw_date,raw_date,limit,user_id)); rows=await cur.fetchall()
            return json_response({"category":category,"period":raw_date,"challenge":{"id":int(challenge["id"]),"title":challenge["title"]},"subjects":subjects,"entries":[_leaderboard_row(r) for r in rows if int(r["rank"])<=limit],"my_rank":next((_leaderboard_row(r) for r in rows if int(r["telegram_id"])==user_id),None),"is_admin":user_id in ADMIN_TELEGRAM_IDS})
        if category=="subject":
            if not subject and subjects: subject=subjects[0]
            if subject not in subjects: return json_response({"category":category,"period":"all","subjects":subjects,"entries":[],"my_rank":None,"is_admin":user_id in ADMIN_TELEGRAM_IDS,"message":"Choose a subject with verified submissions."})
            period_type,period_key="lifetime","all"
        elif category=="lifetime": period_type,period_key="lifetime","all"
        else:
            try: period_key=_utc_period(category,request.query.get("period"))
            except ValueError as exc: return json_response({"error":str(exc)},400)
            period_type=category
        chosen_subject=subject if category=="subject" else "All subjects"
        cur=await conn.execute("""WITH scored AS (
          SELECT telegram_id,display_name,normalized_points,correct_count,incorrect_count,skipped_count,total_time_seconds,total_questions_count,attempts_count,first_submitted_at,
            CASE WHEN correct_count+incorrect_count>0 THEN correct_count::numeric/(correct_count+incorrect_count)*100 ELSE 0 END accuracy
          FROM bpsc_leaderboard_period_totals WHERE period_type=%s AND period_key=%s AND subject=%s
        ), ranked AS (SELECT ROW_NUMBER() OVER(ORDER BY normalized_points DESC,accuracy DESC,(total_time_seconds::numeric/NULLIF(total_questions_count,0)) ASC,first_submitted_at ASC,telegram_id ASC) rank,* FROM scored)
        SELECT * FROM ranked WHERE rank<=%s OR telegram_id=%s ORDER BY rank""",(period_type,period_key,chosen_subject,limit,user_id)); rows=await cur.fetchall()
    return json_response({"category":category,"period":period_key,"subject":subject if category=="subject" else None,"subjects":subjects,"entries":[_leaderboard_row(r) for r in rows if int(r["rank"])<=limit],"my_rank":next((_leaderboard_row(r) for r in rows if int(r["telegram_id"])==user_id),None),"is_admin":user_id in ADMIN_TELEGRAM_IDS})

async def leaderboard_admin_challenges(request):
    user_id=request["telegram_user"]["id"]
    if user_id not in ADMIN_TELEGRAM_IDS: return json_response({"error":"Administrator access required."},403)
    pool=request.app["pool"]
    if request.method=="GET":
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT c.id,c.quiz_id,c.challenge_date,c.title,c.active,q.subject FROM bpsc_leaderboard_daily_challenges c JOIN bank_quizzes q ON q.id=c.quiz_id ORDER BY c.challenge_date DESC LIMIT 60")
            challenges=[{**dict(r),"id":int(r["id"]),"quiz_id":int(r["quiz_id"]),"challenge_date":r["challenge_date"].isoformat()} for r in await cur.fetchall()]
            cur=await conn.execute("SELECT id,title,subject FROM bank_quizzes WHERE is_published=TRUE AND is_custom=FALSE ORDER BY id DESC LIMIT 200")
            quizzes=[{"id":int(r["id"]),"title":r["title"],"subject":r["subject"]} for r in await cur.fetchall()]
        return json_response({"challenges":challenges,"quizzes":quizzes})
    try:
        body=await request.json(); quiz_id=int(body["quiz_id"]); day=str(body.get("challenge_date") or datetime.now(timezone.utc).date().isoformat()); title=str(body.get("title") or "Daily Challenge").strip()[:180]
        datetime.strptime(day,"%Y-%m-%d")
        if not title: raise ValueError
    except (ValueError,TypeError,KeyError): return json_response({"error":"Provide a published quiz, date (YYYY-MM-DD), and title."},400)
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT id FROM bank_quizzes WHERE id=%s AND is_published=TRUE AND is_custom=FALSE",(quiz_id,))
        if not await cur.fetchone(): return json_response({"error":"Choose a published non-personal quiz."},400)
        cur=await conn.execute("""INSERT INTO bpsc_leaderboard_daily_challenges(quiz_id,challenge_date,title,created_by_telegram_id) VALUES(%s,%s,%s,%s)
          ON CONFLICT(challenge_date) DO UPDATE SET quiz_id=EXCLUDED.quiz_id,title=EXCLUDED.title,active=TRUE,created_by_telegram_id=EXCLUDED.created_by_telegram_id RETURNING id""",(quiz_id,day,title,user_id)); row=await cur.fetchone()
    return json_response({"saved":True,"challenge_id":int(row["id"]),"challenge_date":day},201)

async def leaderboard_admin_attempts(request):
    user_id=request["telegram_user"]["id"]
    if user_id not in ADMIN_TELEGRAM_IDS: return json_response({"error":"Administrator access required."},403)
    async with request.app["pool"].connection() as conn:
        cur=await conn.execute("""SELECT id,source_key,source_type,telegram_id,display_name,quiz_title,subject,score,max_score,normalized_points,validation_status,eligible,exclusion_reason,submitted_at FROM bpsc_global_leaderboard_attempts WHERE validation_status<>'verified' OR eligible=FALSE OR score>max_score OR submitted_at<started_at ORDER BY submitted_at DESC LIMIT 100"""); rows=await cur.fetchall()
    return json_response({"attempts":[{**dict(r),"id":int(r["id"]),"telegram_id":int(r["telegram_id"]),"score":float(r["score"]),"max_score":float(r["max_score"]),"normalized_points":float(r["normalized_points"])} for r in rows]})

async def leaderboard_admin_stats(request):
    user_id=request["telegram_user"]["id"]
    if user_id not in ADMIN_TELEGRAM_IDS: return json_response({"error":"Administrator access required."},403)
    async with request.app["pool"].connection() as conn:
        cur=await conn.execute("""SELECT
          COUNT(*) FILTER (WHERE submitted_at >= date_trunc('day',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS attempts_today,
          COUNT(DISTINCT telegram_id) FILTER (WHERE submitted_at >= date_trunc('day',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS users_today,
          COUNT(*) FILTER (WHERE submitted_at >= date_trunc('week',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS attempts_week,
          COUNT(DISTINCT telegram_id) FILTER (WHERE submitted_at >= date_trunc('week',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS users_week,
          COUNT(*) FILTER (WHERE submitted_at >= date_trunc('month',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS attempts_month,
          COUNT(DISTINCT telegram_id) FILTER (WHERE submitted_at >= date_trunc('month',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS users_month,
          COUNT(*) AS all_time_attempts,COUNT(DISTINCT telegram_id) AS all_time_users
          FROM bpsc_global_leaderboard_attempts WHERE eligible=TRUE AND validation_status='verified'""")
        row=await cur.fetchone()
    return json_response({k:int(v or 0) for k,v in dict(row).items()})

async def leaderboard_admin_exclude(request):
    user_id=request["telegram_user"]["id"]
    if user_id not in ADMIN_TELEGRAM_IDS: return json_response({"error":"Administrator access required."},403)
    try: body=await request.json(); attempt_id=int(body["attempt_id"]); reason=str(body.get("reason","")).strip()[:500]
    except (ValueError,TypeError,KeyError): return json_response({"error":"Invalid attempt ID or reason."},400)
    if len(reason)<8: return json_response({"error":"Provide a specific reason of at least 8 characters."},400)
    async with request.app["pool"].connection() as conn:
        async with conn.transaction():
            cur=await conn.execute("UPDATE bpsc_global_leaderboard_attempts SET eligible=FALSE,validation_status='invalid',exclusion_reason=%s WHERE id=%s AND (eligible=TRUE OR validation_status='review') RETURNING id",(reason,attempt_id))
            if not await cur.fetchone(): return json_response({"error":"Attempt not found or already excluded."},404)
            await conn.execute("INSERT INTO bpsc_leaderboard_audit(attempt_id,admin_telegram_id,action,reason) VALUES(%s,%s,'exclude',%s)",(attempt_id,user_id,reason))
            await lb.rebuild_summaries(conn)
    return json_response({"excluded":True,"attempt_id":attempt_id})

VALID_THEMES = {"obsidian", "emerald", "cobalt", "amethyst", "ivory", "graphite"}

async def get_theme_preference(request):
    user_id = request["telegram_user"]["id"]
    async with request.app["pool"].connection() as conn:
        cur = await conn.execute("SELECT preference_value FROM bpsc_nexus_user_preferences WHERE telegram_id=%s AND preference_key='theme'", (user_id,))
        row = await cur.fetchone()
    theme = row["preference_value"] if row else "obsidian"
    # Keep legacy saved preferences readable while supporting the five current palettes.
    theme = {"graphite": "obsidian"}.get(theme, theme)
    if theme not in VALID_THEMES:
        theme = "obsidian"
    return json_response({"theme": theme})

async def save_theme_preference(request):
    user_id = request["telegram_user"]["id"]
    try:
        body = await request.json()
        theme = str(body.get("theme", ""))
    except Exception:
        return json_response({"error": "Invalid preference payload."}, 400)
    if theme not in VALID_THEMES:
        return json_response({"error": "Unsupported theme."}, 400)
    async with request.app["pool"].connection() as conn:
        await conn.execute("""INSERT INTO bpsc_nexus_user_preferences(telegram_id, preference_key, preference_value, updated_at)
            VALUES(%s, 'theme', %s, NOW())
            ON CONFLICT(telegram_id, preference_key) DO UPDATE SET preference_value=EXCLUDED.preference_value, updated_at=NOW()""", (user_id, theme))
    return json_response({"saved": True, "theme": theme})

# Personal study planner: additive tables, isolated by authenticated Telegram user.
async def list_tasks(request):
    user_id = request["telegram_user"]["id"]
    async with request.app["pool"].connection() as conn:
        cur = await conn.execute("""SELECT id,title,task_type,quiz_id,due_date,is_completed,created_at,completed_at
            FROM bpsc_nexus_tasks WHERE telegram_id=%s ORDER BY is_completed ASC, due_date ASC NULLS LAST, created_at DESC LIMIT 200""", (user_id,))
        rows = await cur.fetchall()
    return json_response({"tasks": [{"id": int(r["id"]), "title": r["title"], "task_type": r["task_type"], "quiz_id": int(r["quiz_id"]) if r["quiz_id"] else None,
        "due_date": r["due_date"].isoformat() if r["due_date"] else None, "is_completed": bool(r["is_completed"]),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None, "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None} for r in rows]})

async def create_task(request):
    user_id = request["telegram_user"]["id"]
    try:
        body = await request.json(); title = str(body.get("title", "")).strip()[:180]
        task_type = "quiz" if body.get("quiz_id") else "custom"
        quiz_id = int(body["quiz_id"]) if body.get("quiz_id") else None
        due_date = str(body.get("due_date") or "").strip() or None
        if not title: return json_response({"error": "Enter a task name."}, 400)
        if due_date:
            datetime.strptime(due_date, "%Y-%m-%d")
    except (ValueError, TypeError, KeyError):
        return json_response({"error": "Invalid task details or due date."}, 400)
    async with request.app["pool"].connection() as conn:
        if quiz_id:
            cur = await conn.execute("SELECT 1 FROM bank_quizzes WHERE id=%s AND is_published=TRUE AND (is_custom=FALSE OR custom_owner_telegram_id=%s)", (quiz_id, user_id))
            if not await cur.fetchone(): return json_response({"error": "That quiz is unavailable to this account."}, 404)
        cur = await conn.execute("""INSERT INTO bpsc_nexus_tasks(telegram_id,title,task_type,quiz_id,due_date)
            VALUES(%s,%s,%s,%s,%s) RETURNING id,title,task_type,quiz_id,due_date,is_completed,created_at,completed_at""", (user_id,title,task_type,quiz_id,due_date))
        r = await cur.fetchone()
    return json_response({"task": {"id": int(r["id"]), "title": r["title"], "task_type": r["task_type"], "quiz_id": int(r["quiz_id"]) if r["quiz_id"] else None, "due_date": r["due_date"].isoformat() if r["due_date"] else None, "is_completed": False, "created_at": r["created_at"].isoformat() if r["created_at"] else None}}, 201)

async def update_task(request):
    user_id = request["telegram_user"]["id"]
    try: task_id = int(request.match_info["task_id"]); body = await request.json()
    except Exception: return json_response({"error": "Invalid task request."}, 400)
    fields=[]; params=[]
    if "is_completed" in body:
        completed = bool(body["is_completed"]); fields.append("is_completed=%s"); params.append(completed); fields.append("completed_at=CASE WHEN %s THEN NOW() ELSE NULL END"); params.append(completed)
    if "title" in body:
        title=str(body.get("title") or "").strip()[:180]
        if not title: return json_response({"error":"Task title cannot be empty."},400)
        fields.append("title=%s");params.append(title)
    if "due_date" in body:
        due=str(body.get("due_date") or "").strip() or None
        try:
            if due: datetime.strptime(due,"%Y-%m-%d")
        except ValueError: return json_response({"error":"Invalid due date."},400)
        fields.append("due_date=%s");params.append(due)
    if not fields: return json_response({"error":"No supported task changes supplied."},400)
    params.extend([task_id,user_id])
    async with request.app["pool"].connection() as conn:
        cur=await conn.execute("UPDATE bpsc_nexus_tasks SET "+", ".join(fields)+" WHERE id=%s AND telegram_id=%s RETURNING id",params)
        if not await cur.fetchone(): return json_response({"error":"Task not found."},404)
    return json_response({"saved":True})

async def delete_task(request):
    user_id=request["telegram_user"]["id"]
    try: task_id=int(request.match_info["task_id"])
    except ValueError: return json_response({"error":"Invalid task."},400)
    async with request.app["pool"].connection() as conn:
        cur=await conn.execute("DELETE FROM bpsc_nexus_tasks WHERE id=%s AND telegram_id=%s RETURNING id",(task_id,user_id))
        if not await cur.fetchone(): return json_response({"error":"Task not found."},404)
    return json_response({"deleted":True})

# Arena foundation: invite-based/matchmaking 1v1 rooms, server-selected questions and server-side scoring.
def _arena_code():
    import secrets, string
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))

async def arena_list(request):
    uid=request["telegram_user"]["id"]
    async with request.app["pool"].connection() as conn:
        cur=await conn.execute("""SELECT m.id,m.invite_code,m.status,m.created_at,m.question_count,p.score,p.rating_delta,
            (SELECT COUNT(*) FROM bpsc_nexus_arena_players pp WHERE pp.match_id=m.id) AS players
            FROM bpsc_nexus_arena_matches m JOIN bpsc_nexus_arena_players p ON p.match_id=m.id AND p.telegram_id=%s
            ORDER BY m.created_at DESC LIMIT 20""",(uid,))
        rows=await cur.fetchall()
        cur=await conn.execute("SELECT rating,wins,losses,draws FROM bpsc_nexus_arena_ratings WHERE telegram_id=%s",(uid,)); rating=await cur.fetchone()
    return json_response({"matches":[{"id":int(r["id"]),"invite_code":r["invite_code"],"status":r["status"],"created_at":r["created_at"].isoformat(),"question_count":int(r["question_count"]),"score":int(r["score"] or 0),"players":int(r["players"])} for r in rows],"rating":int(rating["rating"]) if rating else 1000,"wins":int(rating["wins"]) if rating else 0,"losses":int(rating["losses"]) if rating else 0,"draws":int(rating["draws"]) if rating else 0})

async def arena_create(request):
    uid=request["telegram_user"]["id"]
    try: body=await request.json(); mode=str(body.get("mode","invite")); mode=mode if mode in {"invite","matchmaking"} else "invite"
    except Exception: mode="invite"
    async with request.app["pool"].connection() as conn:
        # Matchmaking joins the oldest open queue room, if one exists.
        if mode=="matchmaking":
            cur=await conn.execute("""SELECT m.id FROM bpsc_nexus_arena_matches m JOIN bpsc_nexus_arena_players p ON p.match_id=m.id
                WHERE m.status='waiting' AND m.match_type='matchmaking' AND p.telegram_id<>%s ORDER BY m.created_at FOR UPDATE OF m SKIP LOCKED LIMIT 1""",(uid,))
            open_match=await cur.fetchone()
            if open_match:
                match_id=int(open_match["id"])
                await conn.execute("INSERT INTO bpsc_nexus_arena_players(match_id,telegram_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(match_id,uid))
                await conn.execute("UPDATE bpsc_nexus_arena_matches SET status='active',started_at=NOW() WHERE id=%s AND status='waiting'",(match_id,))
                return json_response({"match_id":match_id,"status":"active","joined":True})
        cur=await conn.execute("SELECT id FROM question_bank WHERE status='ready' ORDER BY RANDOM() LIMIT 10")
        qs=await cur.fetchall()
        if len(qs)<5: return json_response({"error":"The question bank needs at least five verified questions before Arena can start."},503)
        code=_arena_code()
        cur=await conn.execute("""INSERT INTO bpsc_nexus_arena_matches(invite_code,match_type,status,question_count,created_by_telegram_id)
            VALUES(%s,%s,'waiting',%s,%s) RETURNING id""",(code,mode,len(qs),uid))
        match_id=int((await cur.fetchone())["id"])
        await conn.execute("INSERT INTO bpsc_nexus_arena_players(match_id,telegram_id) VALUES(%s,%s)",(match_id,uid))
        for pos,q in enumerate(qs,1):
            await conn.execute("INSERT INTO bpsc_nexus_arena_questions(match_id,position,question_id) VALUES(%s,%s,%s)",(match_id,pos,int(q["id"])))
    return json_response({"match_id":match_id,"invite_code":code,"status":"waiting","joined":False},201)

async def arena_join(request):
    uid=request["telegram_user"]["id"]
    try: body=await request.json(); code=str(body.get("invite_code","")).strip().upper()
    except Exception: return json_response({"error":"Invalid invite code."},400)
    if not code: return json_response({"error":"Enter an invite code."},400)
    async with request.app["pool"].connection() as conn:
        cur=await conn.execute("SELECT id,status FROM bpsc_nexus_arena_matches WHERE invite_code=%s FOR UPDATE",(code,)); m=await cur.fetchone()
        if not m: return json_response({"error":"No arena room found for that code."},404)
        if m["status"]!="waiting": return json_response({"error":"This room has already started or finished."},409)
        cur=await conn.execute("SELECT 1 FROM bpsc_nexus_arena_players WHERE match_id=%s AND telegram_id=%s",(m["id"],uid))
        if await cur.fetchone(): return json_response({"error":"You are already in this room."},409)
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_nexus_arena_players WHERE match_id=%s",(m["id"],)); n=int((await cur.fetchone())["n"])
        if n>=2: return json_response({"error":"This room is full."},409)
        await conn.execute("INSERT INTO bpsc_nexus_arena_players(match_id,telegram_id) VALUES(%s,%s)",(m["id"],uid))
        await conn.execute("UPDATE bpsc_nexus_arena_matches SET status='active',started_at=NOW() WHERE id=%s",(m["id"],))
    return json_response({"match_id":int(m["id"]),"status":"active","joined":True})

async def arena_match(request):
    uid=request["telegram_user"]["id"]
    try: mid=int(request.match_info["match_id"])
    except ValueError: return json_response({"error":"Invalid match."},400)
    async with request.app["pool"].connection() as conn:
        cur=await conn.execute("SELECT m.id,m.invite_code,m.status,m.question_count,m.started_at FROM bpsc_nexus_arena_matches m JOIN bpsc_nexus_arena_players p ON p.match_id=m.id AND p.telegram_id=%s WHERE m.id=%s",(uid,mid)); m=await cur.fetchone()
        if not m:return json_response({"error":"Arena match not found."},404)
        cur=await conn.execute("SELECT telegram_id,score,finished_at FROM bpsc_nexus_arena_players WHERE match_id=%s ORDER BY joined_at",(mid,)); players=await cur.fetchall()
        cur=await conn.execute("""SELECT q.position,q.question_id,b.subject,b.question_en,b.question_hi,b.options,a.selected_option
            FROM bpsc_nexus_arena_questions q JOIN question_bank b ON b.id=q.question_id
            LEFT JOIN bpsc_nexus_arena_answers a ON a.match_id=q.match_id AND a.position=q.position AND a.telegram_id=%s
            WHERE q.match_id=%s ORDER BY q.position""",(uid,mid)); qs=await cur.fetchall()
        cur=await conn.execute("SELECT position,selected_option FROM bpsc_nexus_arena_answers WHERE match_id=%s AND telegram_id=%s",(mid,uid)); answered={int(r["position"]):r["selected_option"] for r in await cur.fetchall()}
    return json_response({"id":int(m["id"]),"invite_code":m["invite_code"],"status":m["status"],"question_count":int(m["question_count"]),"started_at":m["started_at"].isoformat() if m["started_at"] else None,"players":[{"is_you":int(p["telegram_id"])==uid,"score":int(p["score"] or 0),"finished":bool(p["finished_at"])} for p in players],"questions":[{"position":int(q["position"]),"id":int(q["question_id"]),"subject":q["subject"],"question_en":q["question_en"],"question_hi":q["question_hi"],"options":q["options"] if isinstance(q["options"],dict) else json.loads(q["options"] or "{}"),"selected_option":answered.get(int(q["position"]))} for q in qs]})

async def arena_answer(request):
    uid=request["telegram_user"]["id"]
    try: mid=int(request.match_info["match_id"]); body=await request.json(); pos=int(body.get("position")); selected=str(body.get("selected_option",""));
    except Exception:return json_response({"error":"Invalid answer."},400)
    if selected not in {"A","B","C","D","E"}: return json_response({"error":"Choose a valid option."},400)
    pool=request.app["pool"]
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT status,question_count,started_at FROM bpsc_nexus_arena_matches WHERE id=%s FOR UPDATE",(mid,)); m=await cur.fetchone()
        if not m:return json_response({"error":"Match not found."},404)
        if m["status"]!="active":return json_response({"error":"This match is not active."},409)
        if m["started_at"] and (datetime.now(timezone.utc)-m["started_at"].astimezone(timezone.utc)).total_seconds()>900:
            await conn.execute("UPDATE bpsc_nexus_arena_matches SET status='finished',finished_at=NOW() WHERE id=%s",(mid,))
            return json_response({"error":"Match expired. Start a new battle."},409)
        cur=await conn.execute("SELECT 1 FROM bpsc_nexus_arena_players WHERE match_id=%s AND telegram_id=%s",(mid,uid))
        if not await cur.fetchone():return json_response({"error":"You are not a participant in this match."},403)
        cur=await conn.execute("SELECT q.question_id,b.correct_option FROM bpsc_nexus_arena_questions q JOIN question_bank b ON b.id=q.question_id WHERE q.match_id=%s AND q.position=%s",(mid,pos)); q=await cur.fetchone()
        if not q:return json_response({"error":"Question not found."},404)
        cur=await conn.execute("SELECT 1 FROM bpsc_nexus_arena_answers WHERE match_id=%s AND telegram_id=%s AND position=%s",(mid,uid,pos))
        if await cur.fetchone():return json_response({"error":"This answer has already been recorded."},409)
        correct=(selected==q["correct_option"])
        await conn.execute("INSERT INTO bpsc_nexus_arena_answers(match_id,telegram_id,position,selected_option,is_correct) VALUES(%s,%s,%s,%s,%s)",(mid,uid,pos,selected,correct))
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_nexus_arena_answers WHERE match_id=%s AND telegram_id=%s AND is_correct=TRUE",(mid,uid)); score=int((await cur.fetchone())["n"])
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_nexus_arena_answers WHERE match_id=%s AND telegram_id=%s",(mid,uid)); done=int((await cur.fetchone())["n"])
        await conn.execute("UPDATE bpsc_nexus_arena_players SET score=%s,finished_at=CASE WHEN %s>=(SELECT question_count FROM bpsc_nexus_arena_matches WHERE id=%s) THEN NOW() ELSE finished_at END WHERE match_id=%s AND telegram_id=%s",(score,done,mid,mid,uid))
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_nexus_arena_players WHERE match_id=%s AND finished_at IS NOT NULL",(mid,)); finished=int((await cur.fetchone())["n"])
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_nexus_arena_players WHERE match_id=%s",(mid,)); players=int((await cur.fetchone())["n"])
        if players==2 and finished==2:
            await conn.execute("UPDATE bpsc_nexus_arena_matches SET status='finished',finished_at=NOW() WHERE id=%s",(mid,))
            cur=await conn.execute("SELECT telegram_id,score FROM bpsc_nexus_arena_players WHERE match_id=%s",(mid,)); standings=await cur.fetchall()
            if len(standings)==2:
                for row in standings:
                    outcome="draws" if standings[0]["score"]==standings[1]["score"] else ("wins" if row["score"]==max(x["score"] for x in standings) else "losses")
                    delta=0 if outcome=="draws" else (25 if outcome=="wins" else -25)
                    await conn.execute("""INSERT INTO bpsc_nexus_arena_ratings(telegram_id,rating,wins,losses,draws) VALUES(%s,%s,%s,%s,%s)
                        ON CONFLICT(telegram_id) DO UPDATE SET rating=GREATEST(100, bpsc_nexus_arena_ratings.rating+%s), wins=bpsc_nexus_arena_ratings.wins+%s, losses=bpsc_nexus_arena_ratings.losses+%s, draws=bpsc_nexus_arena_ratings.draws+%s""",(row["telegram_id"],1000+delta,1 if outcome=="wins" else 0,1 if outcome=="losses" else 0,1 if outcome=="draws" else 0,delta,1 if outcome=="wins" else 0,1 if outcome=="losses" else 0,1 if outcome=="draws" else 0))
    return json_response({"saved":True,"score":score,"answered":done,"finished":done>=int(m["question_count"])})


ALL_ACCESS_PRICES = {60: 90, 90: 130, 120: 150, 180: 180, 249: 220, 365: 300}
# Product codes are stable entitlement identifiers; retiring a product only hides new sales.
INDIVIDUAL_PRODUCTS = {
    "ghatnachakra_ecology": {"name": "Ghatnachakra Ecology — Lifetime Edition", "price": 39, "duration_days": 0, "entitlement_key": "ghatnachakra_ecology", "active": True},
}

async def payment_schema(conn):
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_payment_requests (
        id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL, product_code TEXT NOT NULL,
        product_name TEXT NOT NULL, duration_days INTEGER NOT NULL, amount_inr INTEGER NOT NULL,
        transaction_ref TEXT NOT NULL, screenshot_data TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
        submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), reviewed_at TIMESTAMPTZ,
        reviewed_by BIGINT, review_note TEXT, activated_pass_id BIGINT, UNIQUE(transaction_ref))""")
    await conn.execute("ALTER TABLE bpsc_payment_requests ADD COLUMN IF NOT EXISTS product_code TEXT NOT NULL DEFAULT 'all_access'")
    await conn.execute("CREATE INDEX IF NOT EXISTS bpsc_payment_requests_status_idx ON bpsc_payment_requests(status, submitted_at DESC)")
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_individual_products (
        product_code TEXT PRIMARY KEY, product_name TEXT NOT NULL, amount_inr INTEGER NOT NULL CHECK(amount_inr >= 0),
        entitlement_key TEXT NOT NULL, duration_days INTEGER NOT NULL DEFAULT 0, is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), retired_at TIMESTAMPTZ NULL)""")
    await conn.execute("""INSERT INTO bpsc_individual_products(product_code,product_name,amount_inr,entitlement_key,duration_days,is_active)
        VALUES
        ('bpsc_pyq','BPSC PYQ — Lifetime Edition',49,'bpsc_pyq',0,TRUE),
        ('upsc_cse_pyq','UPSC CSE PYQ — Lifetime Edition',49,'upsc_cse_pyq',0,TRUE),
        ('ssc_pyq','SSC PYQ — Lifetime Edition',49,'ssc_pyq',0,TRUE),
        ('upsc_cds_capf_other','UPSC CDS/CAPF/Other Exams — Lifetime Edition',99,'upsc_cds_capf_other',0,TRUE),
        ('bihar_exams_gk_gs','Bihar Exams GK/GS — Lifetime Edition',29,'bihar_exams_gk_gs',0,TRUE),
        ('lucent','Lucent — Lifetime Edition',69,'lucent',0,TRUE),
        ('ghatnachakra_science','Ghatnachakra Science — Lifetime Edition',39,'ghatnachakra_science',0,TRUE),
        ('ghatnachakra_geography','Ghatnachakra Geography — Lifetime Edition',39,'ghatnachakra_geography',0,TRUE),
        ('ghatnachakra_economy','Ghatnachakra Economy — Lifetime Edition',39,'ghatnachakra_economy',0,TRUE),
        ('ghatnachakra_polity','Ghatnachakra Polity — Lifetime Edition',39,'ghatnachakra_polity',0,TRUE),
        ('ghatnachakra_history','Ghatnachakra History — Lifetime Edition',39,'ghatnachakra_history',0,TRUE),
        ('ghatnachakra_ecology','Ghatnachakra Ecology — Lifetime Edition',39,'ghatnachakra_ecology',0,TRUE)
        ON CONFLICT(product_code) DO UPDATE SET product_name=EXCLUDED.product_name,amount_inr=EXCLUDED.amount_inr,entitlement_key=EXCLUDED.entitlement_key,duration_days=0""")

async def membership_page(request):
    page = ROOT / "static" / "membership.html"
    if not page.is_file(): raise web.HTTPNotFound()
    return web.FileResponse(page)

async def payment_options(request):
    uid=request["telegram_user"]["id"]
    async with request.app["pool"].connection() as conn:
        await payment_schema(conn)
        cur=await conn.execute("SELECT id,product_name,duration_days,amount_inr,product_code,transaction_ref,status,submitted_at,reviewed_at FROM bpsc_payment_requests WHERE telegram_id=%s ORDER BY submitted_at DESC LIMIT 20",(uid,))
        history=await cur.fetchall()
        cur=await conn.execute("SELECT product_code,product_name,amount_inr,entitlement_key,duration_days FROM bpsc_individual_products WHERE is_active=TRUE ORDER BY product_name")
        products=await cur.fetchall()
    return json_response({"prices":[{"days":d,"price":p} for d,p in ALL_ACCESS_PRICES.items()],"products":[dict(r) for r in products],"history":[dict(r) for r in history]})

async def submit_payment(request):
    user=request["telegram_user"]
    try: body=await request.json()
    except Exception: return json_response({"error":"Invalid submission."},400)
    product_code=str(body.get("product_code") or "all_access").strip().lower()
    ref=str(body.get("transaction_ref") or "").strip().upper()
    screenshot=str(body.get("screenshot_data") or "")
    if not re.fullmatch(r"[A-Z0-9-]{6,40}",ref): return json_response({"error":"Enter a valid UPI transaction/reference ID (6–40 letters, digits or hyphens)."},400)
    if not screenshot.startswith("data:image/") or len(screenshot)>900000: return json_response({"error":"Upload a clear payment screenshot under 650 KB."},400)
    async with request.app["pool"].connection() as conn:
        await payment_schema(conn)
        if product_code == "all_access":
            try:
                days=int(body.get("duration_days")); amount=ALL_ACCESS_PRICES[days]
            except Exception: return json_response({"error":"Choose a valid All Access duration."},400)
            product_name=f"All Access · {days} days"
        else:
            cur=await conn.execute("SELECT product_code,product_name,amount_inr,duration_days FROM bpsc_individual_products WHERE product_code=%s AND is_active=TRUE",(product_code,))
            product=await cur.fetchone()
            if not product: return json_response({"error":"This individual question bank is unavailable for new purchases."},404)
            amount=int(product["amount_inr"]); days=int(product["duration_days"]); product_name=product["product_name"]
        cur=await conn.execute("SELECT status FROM bpsc_payment_requests WHERE transaction_ref=%s",(ref,))
        if await cur.fetchone(): return json_response({"error":"This transaction/reference ID has already been submitted."},409)
        cur=await conn.execute("INSERT INTO bpsc_payment_requests(telegram_id,product_code,product_name,duration_days,amount_inr,transaction_ref,screenshot_data) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,submitted_at",(user["id"],product_code,product_name,days,amount,ref,screenshot))
        r=await cur.fetchone()
    return json_response({"request_id":int(r["id"]),"status":"pending","amount":amount,"product_code":product_code,"product_name":product_name,"submitted_at":r["submitted_at"].isoformat()},201)

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


async def ensure_revision_schema(conn):
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_revision_items (
        telegram_id BIGINT NOT NULL, question_id BIGINT NOT NULL,
        reason TEXT NOT NULL CHECK(reason IN ('wrong','skipped','bookmark')),
        first_saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        review_count INTEGER NOT NULL DEFAULT 0, is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY(telegram_id,question_id,reason))""")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_nexus_revision_owner ON bpsc_nexus_revision_items(telegram_id,is_resolved,reason)")


async def list_revision(request):
    uid=request['telegram_user']['id']
    async with request.app['pool'].connection() as conn:
        await ensure_revision_schema(conn)
        cur=await conn.execute("""SELECT r.question_id,r.reason,r.first_saved_at,r.last_saved_at,r.review_count,r.is_resolved,
            q.subject,q.topic,q.question_en,q.question_hi,q.options,q.correct_option,q.explanation_en,q.explanation_hi
            FROM bpsc_nexus_revision_items r JOIN question_bank q ON q.id=r.question_id
            WHERE r.telegram_id=%s AND r.is_resolved=FALSE ORDER BY r.last_saved_at DESC LIMIT 2000""",(uid,))
        rows=await cur.fetchall()
    items=[]
    for r in rows:
        opts=r['options'] if isinstance(r['options'],dict) else json.loads(r['options'] or '{}')
        items.append({'question_id':int(r['question_id']),'reason':r['reason'],'first_saved_at':r['first_saved_at'].isoformat(),'last_saved_at':r['last_saved_at'].isoformat(),'review_count':int(r['review_count']),'subject':r['subject'],'topic':r['topic'],'question_en':r['question_en'],'question_hi':r['question_hi'],'options':opts,'correct_option':r['correct_option'],'explanation_en':r['explanation_en'],'explanation_hi':r['explanation_hi']})
    return json_response({'items':items,'count':len(items)})


async def set_revision_item(request):
    uid=request['telegram_user']['id']
    try:
        body=await request.json(); qid=int(body.get('question_id')); reason=str(body.get('reason') or 'bookmark'); enabled=bool(body.get('enabled',True))
        if reason not in ('bookmark','wrong','skipped'): raise ValueError()
    except Exception: return json_response({'error':'Invalid revision item payload.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_revision_schema(conn)
        cur=await conn.execute('SELECT 1 FROM question_bank WHERE id=%s',(qid,))
        if not await cur.fetchone(): return json_response({'error':'Question not found.'},404)
        if enabled:
            await conn.execute("""INSERT INTO bpsc_nexus_revision_items(telegram_id,question_id,reason)
                VALUES(%s,%s,%s) ON CONFLICT(telegram_id,question_id,reason) DO UPDATE SET last_saved_at=NOW(),is_resolved=FALSE""",(uid,qid,reason))
        else:
            await conn.execute('DELETE FROM bpsc_nexus_revision_items WHERE telegram_id=%s AND question_id=%s AND reason=%s',(uid,qid,reason))
    return json_response({'saved':enabled,'question_id':qid,'reason':reason})


async def resolve_revision_item(request):
    uid=request['telegram_user']['id']
    try:
        body=await request.json(); qid=int(body.get('question_id')); reason=str(body.get('reason') or 'wrong')
        if reason not in ('bookmark','wrong','skipped'): raise ValueError()
    except Exception: return json_response({'error':'Invalid revision payload.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_revision_schema(conn)
        cur=await conn.execute('UPDATE bpsc_nexus_revision_items SET is_resolved=TRUE,last_saved_at=NOW() WHERE telegram_id=%s AND question_id=%s AND reason=%s RETURNING question_id',(uid,qid,reason))
        if not await cur.fetchone(): return json_response({'error':'Revision item not found for this account.'},404)
    return json_response({'resolved':True})



async def ensure_test_series_schema(conn):
    """Additive schema for neutral-branded multi-exam test-series libraries."""
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_test_series (
        id BIGSERIAL PRIMARY KEY,
        title VARCHAR(180) NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        exam_type VARCHAR(16) NOT NULL CHECK (exam_type IN ('UPSC','BPSC')),
        series_type VARCHAR(20) NOT NULL DEFAULT 'full_length' CHECK (series_type IN ('full_length','sectional','subject_wise')),
        subject VARCHAR(100) NOT NULL DEFAULT 'General Studies',
        difficulty VARCHAR(24) NOT NULL DEFAULT 'Mixed',
        access_type VARCHAR(12) NOT NULL DEFAULT 'paid' CHECK (access_type IN ('free','paid')),
        is_published BOOLEAN NOT NULL DEFAULT FALSE,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_by_telegram_id BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_test_series_tests (
        series_id BIGINT NOT NULL REFERENCES bpsc_nexus_test_series(id) ON DELETE CASCADE,
        quiz_id BIGINT NOT NULL REFERENCES bank_quizzes(id) ON DELETE CASCADE,
        position INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(series_id, quiz_id)
    )""")
    await conn.execute("CREATE INDEX IF NOT EXISTS bpsc_nexus_test_series_public_idx ON bpsc_nexus_test_series(is_published,exam_type,sort_order,id)")
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_test_series_entitlements (
        series_id BIGINT NOT NULL REFERENCES bpsc_nexus_test_series(id) ON DELETE CASCADE,
        telegram_id BIGINT NOT NULL,
        granted_by_telegram_id BIGINT NOT NULL,
        grant_note TEXT NOT NULL DEFAULT '',
        granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NULL,
        revoked_at TIMESTAMPTZ NULL,
        PRIMARY KEY(series_id, telegram_id)
    )""")


def _is_series_admin(user):
    return int(user.get('id', 0)) in ADMIN_TELEGRAM_IDS


async def list_test_series(request):
    user_id = request['telegram_user']['id']
    exam = request.query.get('exam', '').upper()
    kind = request.query.get('type', '')
    clauses = ["s.is_published=TRUE"]
    params = [user_id]
    if exam in ('UPSC', 'BPSC'):
        clauses.append('s.exam_type=%s'); params.append(exam)
    if kind in ('full_length', 'sectional', 'subject_wise'):
        clauses.append('s.series_type=%s'); params.append(kind)
    async with request.app['pool'].connection() as conn:
        cur = await conn.execute(f"""SELECT s.id,s.title,s.description,s.exam_type,s.series_type,s.subject,s.difficulty,s.access_type,
            COUNT(st.quiz_id) AS test_count,
            BOOL_OR(e.telegram_id IS NOT NULL AND e.revoked_at IS NULL AND (e.expires_at IS NULL OR e.expires_at>NOW())) AS entitled
            FROM bpsc_nexus_test_series s
            LEFT JOIN bpsc_nexus_test_series_tests st ON st.series_id=s.id
            LEFT JOIN bpsc_nexus_test_series_entitlements e ON e.series_id=s.id AND e.telegram_id=%s
            WHERE {' AND '.join(clauses)}
            GROUP BY s.id ORDER BY s.sort_order,s.id DESC LIMIT 200""", tuple(params))
        series_rows = await cur.fetchall()
        out=[]
        for s in series_rows:
            entitled = s['access_type']=='free' or bool(s['entitled'])
            tc = await conn.execute("""SELECT q.id,q.title,q.description,q.subject,q.duration_minutes,q.positive_marks,q.negative_marks,
                COUNT(qq.question_id) AS total_questions, st.position
                FROM bpsc_nexus_test_series_tests st JOIN bank_quizzes q ON q.id=st.quiz_id
                LEFT JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
                LEFT JOIN question_bank qb ON qb.id=qq.question_id AND qb.status='ready'
                WHERE st.series_id=%s AND q.is_published=TRUE
                GROUP BY q.id,st.position ORDER BY st.position,q.id""", (s['id'],))
            tests=[]
            for t in await tc.fetchall():
                tests.append({'quiz_id':int(t['id']),'title':t['title'],'description':t['description'] or '',
                    'subject':t['subject'],'duration_minutes':int(t['duration_minutes']),
                    'positive_marks':float(t['positive_marks']),'negative_marks':float(t['negative_marks']),
                    'total_questions':int(t['total_questions'] or 0),'locked':not entitled})
            out.append({'id':int(s['id']),'title':s['title'],'description':s['description'],'exam_type':s['exam_type'],
                'series_type':s['series_type'],'subject':s['subject'],'difficulty':s['difficulty'],'access_type':s['access_type'],
                'test_count':int(s['test_count'] or 0),'entitled':entitled,'tests':tests})
    return json_response({'series':out,'admin':_is_series_admin(request['telegram_user'])})


async def admin_create_test_series(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        b=await request.json(); title=str(b.get('title','')).strip(); description=str(b.get('description','')).strip()
        exam=str(b.get('exam_type','BPSC')).upper(); stype=str(b.get('series_type','full_length'))
        subject=str(b.get('subject','General Studies')).strip() or 'General Studies'
        difficulty=str(b.get('difficulty','Mixed')).strip() or 'Mixed'; published=bool(b.get('is_published',False))
        if not title or len(title)>180: raise ValueError('Provide a series title up to 180 characters.')
        if exam not in ('UPSC','BPSC'): raise ValueError('Exam must be UPSC or BPSC.')
        if stype not in ('full_length','sectional','subject_wise'): raise ValueError('Invalid series type.')
        if len(description)>3000: raise ValueError('Description is too long.')
    except (ValueError,TypeError) as e: return json_response({'error':str(e) or 'Invalid series details.'},400)
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("""INSERT INTO bpsc_nexus_test_series(title,description,exam_type,series_type,subject,difficulty,access_type,is_published,created_by_telegram_id)
            VALUES(%s,%s,%s,%s,%s,%s,'paid',%s,%s) RETURNING id""",(title,description,exam,stype,subject,difficulty,published,user['id']))
        sid=int((await cur.fetchone())['id'])
    return json_response({'id':sid,'message':'Series draft created. Add tests, review content rights, then publish.'},201)


async def admin_link_series_test(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        sid=int(request.match_info['series_id']); body=await request.json(); qid=int(body.get('quiz_id')); position=max(0,int(body.get('position',0)))
    except Exception: return json_response({'error':'Provide valid series_id and quiz_id.'},400)
    async with request.app['pool'].connection() as conn:
        s=await conn.execute('SELECT id FROM bpsc_nexus_test_series WHERE id=%s',(sid,))
        if not await s.fetchone(): return json_response({'error':'Series not found.'},404)
        q=await conn.execute('SELECT id FROM bank_quizzes WHERE id=%s AND is_published=TRUE',(qid,))
        if not await q.fetchone(): return json_response({'error':'Only an existing published quiz can be attached.'},400)
        await conn.execute("""INSERT INTO bpsc_nexus_test_series_tests(series_id,quiz_id,position) VALUES(%s,%s,%s)
            ON CONFLICT(series_id,quiz_id) DO UPDATE SET position=EXCLUDED.position""",(sid,qid,position))
    return json_response({'linked':True,'series_id':sid,'quiz_id':qid})


async def admin_publish_test_series(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        sid=int(request.match_info['series_id']); body=await request.json(); publish=bool(body.get('publish',True))
    except Exception: return json_response({'error':'Provide a valid series ID and publish flag.'},400)
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute('SELECT id FROM bpsc_nexus_test_series WHERE id=%s',(sid,))
        if not await cur.fetchone(): return json_response({'error':'Series not found.'},404)
        if publish:
            cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_nexus_test_series_tests st JOIN bank_quizzes q ON q.id=st.quiz_id AND q.is_published=TRUE JOIN bank_quiz_questions qq ON qq.quiz_id=q.id JOIN question_bank qb ON qb.id=qq.question_id AND qb.status='ready' WHERE st.series_id=%s",(sid,))
            if int((await cur.fetchone())['n'])<1: return json_response({'error':'Attach at least one published quiz with ready questions before publishing.'},400)
        await conn.execute('UPDATE bpsc_nexus_test_series SET is_published=%s,updated_at=NOW() WHERE id=%s',(publish,sid))
    return json_response({'series_id':sid,'published':publish})


async def admin_series_entitlement(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        sid=int(request.match_info['series_id']); body=await request.json(); target=int(body.get('telegram_id')); grant=bool(body.get('grant',True)); note=str(body.get('note','Admin grant'))[:500]
    except Exception: return json_response({'error':'Provide valid series, telegram ID and grant flag.'},400)
    async with request.app['pool'].connection() as conn:
        if grant:
            await conn.execute("""INSERT INTO bpsc_nexus_test_series_entitlements(series_id,telegram_id,granted_by_telegram_id,grant_note,revoked_at)
                VALUES(%s,%s,%s,%s,NULL) ON CONFLICT(series_id,telegram_id) DO UPDATE SET granted_by_telegram_id=EXCLUDED.granted_by_telegram_id,
                grant_note=EXCLUDED.grant_note,granted_at=NOW(),revoked_at=NULL,expires_at=NULL""",(sid,target,user['id'],note))
        else:
            await conn.execute("UPDATE bpsc_nexus_test_series_entitlements SET revoked_at=NOW() WHERE series_id=%s AND telegram_id=%s",(sid,target))
    return json_response({'series_id':sid,'telegram_id':target,'granted':grant})


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


async def attempt_history(request):
    """Return only the authenticated user's completed attempts for the progress panel."""
    user_id=request["telegram_user"]["id"]
    try: limit=max(1,min(30,int(request.query.get("limit","15"))))
    except ValueError: limit=15
    async with request.app["pool"].connection() as conn:
        cur=await conn.execute("""SELECT a.id,a.quiz_id,a.mode,a.started_at,a.submitted_at,a.total_questions,a.correct_count,a.incorrect_count,a.skipped_count,a.score,
            q.title,q.subject,q.positive_marks
            FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id
            WHERE a.telegram_id=%s AND a.submitted_at IS NOT NULL
            ORDER BY a.submitted_at DESC LIMIT %s""",(user_id,limit))
        rows=await cur.fetchall()
    items=[]
    for r in rows:
        attempted=int(r["correct_count"] or 0)+int(r["incorrect_count"] or 0)
        items.append({"attempt_id":int(r["id"]),"quiz_id":int(r["quiz_id"]),"title":r["title"],"subject":r["subject"],"mode":r["mode"],
            "submitted_at":r["submitted_at"].isoformat() if r["submitted_at"] else None,"total_questions":int(r["total_questions"]),
            "correct":int(r["correct_count"] or 0),"incorrect":int(r["incorrect_count"] or 0),"skipped":int(r["skipped_count"] or 0),
            "score":float(r["score"] or 0),"max_score":float(r["total_questions"])*float(r["positive_marks"]),
            "accuracy":round(int(r["correct_count"] or 0)/attempted*100,1) if attempted else 0})
    return json_response({"attempts":items})


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
        use_all_questions = bool(body.get("use_all_questions", False))
        if not 1 <= count <= 20000: raise ValueError("Question count must be between 1 and 20,000.")
        if not 1 <= duration <= 360: raise ValueError("Legacy duration setting is invalid.")
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
        if use_all_questions:
            count = eligible
        if count < 1:
            return json_response({"error": "No eligible questions match this topic/filter.", "eligible_count": eligible}, 400)
        if eligible < count:
            return json_response({"error": f"Only {eligible} eligible questions match these filters. Reduce the count or change your filters.", "eligible_count": eligible}, 400)
        cur = await conn.execute("SELECT qb.id FROM question_bank qb WHERE " + " AND ".join(conditions) + f" ORDER BY {order} LIMIT %s", tuple(params + [count]))
        ids = [int(r["id"]) for r in await cur.fetchall()]
        display_subject = subject if subject != "All subjects" else "Mixed Subjects"
        title = title or "My Custom Quiz"
        cur = await conn.execute("""INSERT INTO bank_quizzes(title,description,subject,created_by_telegram_id,is_published,duration_minutes,positive_marks,negative_marks,is_custom,custom_owner_telegram_id,question_time_seconds,shuffle_options,published_at)
            VALUES(%s,%s,%s,%s,TRUE,%s,%s,%s,TRUE,%s,%s,%s,NOW()) RETURNING id""",
            (title, f"Custom {mode} quiz · {count} questions · no full-test timer", display_subject, user["id"], 360, positive, negative, user["id"], question_time, shuffle_options))
        quiz_id = int((await cur.fetchone())["id"])
        # psycopg 3 AsyncConnection has no executemany(); use an async cursor.
        async with conn.cursor() as insert_cur:
            await insert_cur.executemany(
                "INSERT INTO bank_quiz_questions(quiz_id,position,question_id) VALUES(%s,%s,%s)",
                [(quiz_id, position, question_id) for position, question_id in enumerate(ids)],
            )
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
        cur = await conn.execute("""SELECT s.id,s.access_type FROM bpsc_nexus_test_series_tests st
            JOIN bpsc_nexus_test_series s ON s.id=st.series_id
            WHERE st.quiz_id=%s AND s.is_published=TRUE AND s.access_type='paid'
            ORDER BY s.id LIMIT 1""", (quiz_id,))
        paid_series = await cur.fetchone()
        if paid_series:
            ent = await conn.execute("""SELECT 1 FROM bpsc_nexus_test_series_entitlements
                WHERE series_id=%s AND telegram_id=%s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>NOW())""",
                (paid_series['id'], user['id']))
            if not await ent.fetchone():
                return json_response({"error": "This test belongs to a paid series. Access is not active for your account yet."}, 403)
        cur = await conn.execute("""SELECT qb.id FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
            WHERE qq.quiz_id=%s AND qb.status='ready' ORDER BY qq.position""", (quiz_id,))
        ids = await cur.fetchall()
        if not ids:
            return json_response({"error": "This quiz has no verified questions."}, 400)
        cur = await conn.execute("INSERT INTO bank_quiz_attempts(quiz_id,telegram_id,mode,total_questions,question_time_seconds,quiz_settings) VALUES(%s,%s,%s,%s,%s,%s::jsonb) RETURNING id,started_at", (quiz_id, user["id"], mode, len(ids), int(quiz["question_time_seconds"] or 0), json.dumps({"custom": bool(quiz["is_custom"]), "mode_label": requested_mode})))
        attempt = await cur.fetchone()
        await conn.execute("""INSERT INTO bank_quiz_attempt_question_snapshots(attempt_id,position,question_id,subject,question_en,question_hi,question_type,question_structure,question_flags,options,correct_option,explanation_en,explanation_hi)
            SELECT %s,qq.position,qb.id,qb.subject,qb.question_en,qb.question_hi,qb.question_type,qb.question_structure,qb.question_flags,qb.options,qb.correct_option, qb.explanation_en,qb.explanation_hi
            FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
            WHERE qq.quiz_id=%s AND qb.status='ready' ORDER BY qq.position""", (attempt["id"], quiz_id))
    return json_response({"attempt_id": int(attempt["id"]), "quiz_id": int(quiz_id), "started_at": attempt["started_at"].isoformat(), "duration_minutes": int(quiz["duration_minutes"]), "question_time_seconds": int(quiz["question_time_seconds"] or 0), "shuffle_options": bool(quiz["shuffle_options"]), "mode": mode, "mode_label": requested_mode})


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
    deadline = None if attempt["is_custom"] else attempt["started_at"].timestamp() + int(attempt["duration_minutes"]) * 60
    if attempt["mode"] == "exam" and deadline is not None and time.time() >= deadline:
        result = await _grade_attempt(pool, attempt_id, user["id"], user.get("first_name", "Student"))
        return json_response(result)
    async with pool.connection() as conn:
        cur = await conn.execute("""SELECT s.question_id AS id,s.question_en,s.question_hi,s.question_type,s.question_structure,s.question_flags,s.options,aa.selected_option,aa.is_correct,aa.is_marked_review,
            s.correct_option,s.explanation_en,s.explanation_hi
            FROM bank_quiz_attempt_question_snapshots s
            LEFT JOIN bank_quiz_attempt_answers aa ON aa.attempt_id=s.attempt_id AND aa.question_id=s.question_id
            WHERE s.attempt_id=%s ORDER BY s.position""", (attempt_id,))
        rows = await cur.fetchall()
    questions = []
    for r in rows:
        opts = r["options"] if isinstance(r["options"], dict) else json.loads(r["options"] or "{}")
        structure = r["question_structure"] if isinstance(r["question_structure"], dict) else json.loads(r["question_structure"] or "{}")
        flags = r["question_flags"] if isinstance(r["question_flags"], dict) else json.loads(r["question_flags"] or "{}")
        item = {"id": int(r["id"]), "question_en": r["question_en"], "question_hi": r["question_hi"], "question_type": r["question_type"], "question_structure": structure, "question_flags": flags, "options": opts, "selected_option": r["selected_option"], "marked_review": bool(r["is_marked_review"])}
        if attempt["mode"] in ("practice", "revision", "rapidfire") and r["selected_option"]:
            item["feedback"] = {"is_correct": bool(r["correct_option"] == r["selected_option"]), "correct_option": r["correct_option"],
                                 "explanation_en": r["explanation_en"], "explanation_hi": r["explanation_hi"]}
        questions.append(item)
    settings = attempt["quiz_settings"] if isinstance(attempt["quiz_settings"], dict) else json.loads(attempt["quiz_settings"] or "{}")
    return json_response({"submitted": False, "attempt_id": attempt_id, "quiz_id": int(attempt["quiz_id"]), "title": attempt["title"], "subject": attempt["subject"], "mode": attempt["mode"], "mode_label": settings.get("mode_label", attempt["mode"]),
                          "duration_minutes": int(attempt["duration_minutes"]), "question_time_seconds": int(attempt["question_time_seconds"] or 0), "shuffle_options": bool(attempt["shuffle_options"]), "started_at": attempt["started_at"].isoformat(), "deadline": datetime.fromtimestamp(deadline, timezone.utc).isoformat() if deadline is not None else None,
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
    if attempt["mode"] == "exam" and not attempt["is_custom"] and time.time() >= attempt["started_at"].timestamp() + int(attempt["duration_minutes"]) * 60:
        result = await _grade_attempt(pool, attempt_id, user["id"], user.get("first_name", "Student"))
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
    if attempt["mode"] == "exam" and not attempt["is_custom"] and time.time() >= attempt["started_at"].timestamp() + int(attempt["duration_minutes"]) * 60:
        result = await _grade_attempt(pool, attempt_id, user["id"], user.get("first_name", "Student"))
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
    result = await _grade_attempt(request.app["pool"], attempt_id, user["id"], user.get("first_name", "Student"))
    if result is None:
        return json_response({"error": "Attempt not found."}, 404)
    return json_response(result)


async def startup(app):
    if not BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError("Set BOT_TOKEN and DATABASE_URL in the Render environment.")
    pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, open=False, kwargs={"row_factory": dict_row, "connect_timeout": 10})
    await pool.open(); await pool.wait()
    await qb.ensure_schema(pool)
    await lb.ensure_schema(pool)
    await lb.backfill_bank_attempts(pool)
    async with pool.connection() as conn:
        await payment_schema(conn)
        await ensure_test_series_schema(conn)
        await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_user_preferences (
            telegram_id BIGINT NOT NULL, preference_key TEXT NOT NULL, preference_value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (telegram_id, preference_key))""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_tasks (
            id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL, title VARCHAR(180) NOT NULL,
            task_type VARCHAR(12) NOT NULL DEFAULT 'custom', quiz_id BIGINT NULL, due_date DATE NULL,
            is_completed BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), completed_at TIMESTAMPTZ NULL)""")
        await conn.execute("CREATE INDEX IF NOT EXISTS bpsc_nexus_tasks_user_idx ON bpsc_nexus_tasks(telegram_id,is_completed,due_date)")
        await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_arena_matches (
            id BIGSERIAL PRIMARY KEY, invite_code VARCHAR(8) UNIQUE NOT NULL, match_type VARCHAR(20) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'waiting', question_count INTEGER NOT NULL DEFAULT 10,
            created_by_telegram_id BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), started_at TIMESTAMPTZ NULL, finished_at TIMESTAMPTZ NULL)""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_arena_players (
            match_id BIGINT NOT NULL REFERENCES bpsc_nexus_arena_matches(id) ON DELETE CASCADE, telegram_id BIGINT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0, joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ NULL,
            PRIMARY KEY(match_id,telegram_id))""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_arena_questions (
            match_id BIGINT NOT NULL REFERENCES bpsc_nexus_arena_matches(id) ON DELETE CASCADE, position INTEGER NOT NULL,
            question_id BIGINT NOT NULL, PRIMARY KEY(match_id,position))""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_arena_answers (
            match_id BIGINT NOT NULL, telegram_id BIGINT NOT NULL, position INTEGER NOT NULL, selected_option VARCHAR(1) NOT NULL,
            is_correct BOOLEAN NOT NULL, answered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(match_id,telegram_id,position))""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_arena_ratings (
            telegram_id BIGINT PRIMARY KEY, rating INTEGER NOT NULL DEFAULT 1000, wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0, draws INTEGER NOT NULL DEFAULT 0)""")
    app["pool"] = pool
    log.info("Mini App API connected; question bank schema and user preferences verified.")


async def cleanup(app):
    pool = app.get("pool")
    if pool:
        await pool.close()


def make_app():
    app = web.Application(middlewares=[telegram_auth], client_max_size=1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/membership", membership_page)
    app.router.add_get("/api/payments", payment_options)
    app.router.add_post("/api/payments", submit_payment)
    app.router.add_get("/static/{filename}", static_asset)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/tasks", list_tasks)
    app.router.add_post("/api/tasks", create_task)
    app.router.add_patch("/api/tasks/{task_id}", update_task)
    app.router.add_delete("/api/tasks/{task_id}", delete_task)
    app.router.add_get("/api/arena", arena_list)
    app.router.add_post("/api/arena/matches", arena_create)
    app.router.add_post("/api/arena/join", arena_join)
    app.router.add_get("/api/arena/matches/{match_id}", arena_match)
    app.router.add_post("/api/arena/matches/{match_id}/answers", arena_answer)
    app.router.add_get("/api/preferences/theme", get_theme_preference)
    app.router.add_put("/api/preferences/theme", save_theme_preference)
    app.router.add_get("/api/leaderboard", leaderboard_data)
    app.router.add_get("/api/leaderboard/admin/challenges", leaderboard_admin_challenges)
    app.router.add_post("/api/leaderboard/admin/challenges", leaderboard_admin_challenges)
    app.router.add_get("/api/leaderboard/admin/attempts", leaderboard_admin_attempts)
    app.router.add_get("/api/leaderboard/admin/stats", leaderboard_admin_stats)
    app.router.add_post("/api/leaderboard/admin/exclude", leaderboard_admin_exclude)
    app.router.add_get("/api/test-series", list_test_series)
    app.router.add_post("/api/admin/test-series", admin_create_test_series)
    app.router.add_post("/api/admin/test-series/{series_id}/tests", admin_link_series_test)
    app.router.add_post("/api/admin/test-series/{series_id}/publish", admin_publish_test_series)
    app.router.add_post("/api/admin/test-series/{series_id}/entitlement", admin_series_entitlement)
    app.router.add_get("/api/quizzes", list_quizzes)
    app.router.add_get("/api/history", attempt_history)
    app.router.add_get("/api/revision", list_revision)
    app.router.add_post("/api/revision", set_revision_item)
    app.router.add_post("/api/revision/resolve", resolve_revision_item)
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
