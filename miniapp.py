"""BPSC Nexus personal CBT Mini App API + static frontend server.

Deploy as a separate Render Web Service from the Telegram bot. Shares DATABASE_URL.
"""
from __future__ import annotations

import hashlib
import base64
import csv
import hmac
import io
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import question_bank as qb
import leaderboard as lb
import intel

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


ADMIN_LOGIN_ID = os.environ.get("ADMIN_LOGIN_ID", "").strip()
ADMIN_LOGIN_PASSWORD = os.environ.get("ADMIN_LOGIN_PASSWORD", "")
def _resolve_admin_login_tid() -> int:
    raw = os.environ.get("ADMIN_LOGIN_TELEGRAM_ID", "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            log.warning("ADMIN_LOGIN_TELEGRAM_ID is not a valid integer: %r", raw)
    # Fallback: first admin ID from ADMIN_TELEGRAM_IDS (legacy auto-detect behavior)
    if ADMIN_TELEGRAM_IDS:
        return sorted(ADMIN_TELEGRAM_IDS)[0]
    return 0

ADMIN_LOGIN_TELEGRAM_ID = _resolve_admin_login_tid()
# Ensure the resolved login ID is included in the approved admin IDs.
if ADMIN_LOGIN_TELEGRAM_ID > 0:
    ADMIN_TELEGRAM_IDS.add(ADMIN_LOGIN_TELEGRAM_ID)
ADMIN_SESSION_SECRET = os.environ.get("ADMIN_SESSION_SECRET", "")
ADMIN_SESSION_COOKIE = "bpsc_nexus_admin_session"
ADMIN_SESSION_SECONDS = 12 * 60 * 60
_ADMIN_LOGIN_FAILURES = {}

# ---------- Demo verification anti-brute-force ----------
_DEMO_VERIFY_FAILURES = {}   # {telegram_id: [timestamps]}
_DEMO_VERIFY_MAX = 5
_DEMO_VERIFY_WINDOW = 3600   # 1 hour

def _demo_verify_allowed(tid: int) -> bool:
    now = time.time()
    recent = [t for t in _DEMO_VERIFY_FAILURES.get(tid, []) if now - t < _DEMO_VERIFY_WINDOW]
    _DEMO_VERIFY_FAILURES[tid] = recent
    return len(recent) < _DEMO_VERIFY_MAX

def _demo_verify_record(tid: int):
    now = time.time()
    attempts = _DEMO_VERIFY_FAILURES.get(tid, [])
    attempts.append(now)
    _DEMO_VERIFY_FAILURES[tid] = attempts[-20:]

async def ensure_demo_verify_schema(conn):
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_demo_verify_log (
        id BIGSERIAL PRIMARY KEY,
        telegram_id BIGINT NOT NULL,
        attempted_contact TEXT NOT NULL,
        contact_type TEXT NOT NULL,
        success BOOLEAN NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    await conn.execute("""CREATE INDEX IF NOT EXISTS bpsc_nexus_demo_verify_log_tid_idx
        ON bpsc_nexus_demo_verify_log(telegram_id, created_at DESC)""")



def _admin_session_sign(payload: str) -> str:
    secret = ADMIN_SESSION_SECRET or BOT_TOKEN
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_admin_session(admin_id: int, login_id: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"id": admin_id, "login": login_id, "exp": int(time.time()) + ADMIN_SESSION_SECONDS}, separators=(",", ":")).encode()).decode().rstrip("=")
    return payload + "." + _admin_session_sign(payload)


def _read_admin_session(token: str):
    if not token or "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _admin_session_sign(payload)):
        return None
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(raw.decode("utf-8"))
        admin_id = int(data.get("id", 0))
        if int(data.get("exp", 0)) < int(time.time()) or admin_id not in ADMIN_TELEGRAM_IDS:
            return None
        return {"id": admin_id, "first_name": "Admin", "username": str(data.get("login", "admin")), "_admin_session": True}
    except Exception:
        return None


@web.middleware
async def telegram_auth(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    if request.path in ("/api/admin/login", "/api/admin/logout", "/api/admin/session"):
        return await handler(request)
    if request.path.startswith("/api/admin/") or request.path.startswith("/api/leaderboard/admin/"):
        admin_user = _read_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE, ""))
        if admin_user:
            request["telegram_user"] = admin_user
            request["admin_session"] = True
            return await handler(request)
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    try:
        request["telegram_user"] = validate_init_data(init_data)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=401)
    return await handler(request)


async def admin_login(request: web.Request):
    if not ADMIN_LOGIN_ID or not ADMIN_LOGIN_PASSWORD or not ADMIN_LOGIN_TELEGRAM_ID:
        return json_response({"error": "Admin password login is not configured. Set ADMIN_LOGIN_ID, ADMIN_LOGIN_PASSWORD and ADMIN_LOGIN_TELEGRAM_ID in Render."}, 503)
    if ADMIN_LOGIN_TELEGRAM_ID not in ADMIN_TELEGRAM_IDS:
        return json_response({"error": "ADMIN_LOGIN_TELEGRAM_ID must also be included in ADMIN_TELEGRAM_IDS."}, 503)
    ip = request.remote or "unknown"
    now = time.time()
    attempts = [t for t in _ADMIN_LOGIN_FAILURES.get(ip, []) if now - t < 900]
    if len(attempts) >= 8:
        _ADMIN_LOGIN_FAILURES[ip] = attempts
        return json_response({"error": "Too many login attempts. Wait 15 minutes and try again."}, 429)
    try:
        data = await request.json()
    except Exception:
        data = {}
    login_id = str(data.get("id", "")).strip()
    password = str(data.get("password", ""))
    ok = hmac.compare_digest(login_id, ADMIN_LOGIN_ID) and hmac.compare_digest(password, ADMIN_LOGIN_PASSWORD)
    if not ok:
        attempts.append(now)
        _ADMIN_LOGIN_FAILURES[ip] = attempts
        return json_response({"error": "Invalid admin ID or password."}, 401)
    _ADMIN_LOGIN_FAILURES.pop(ip, None)
    response = json_response({"ok": True, "expires_in": ADMIN_SESSION_SECONDS, "admin": True})
    response.set_cookie(ADMIN_SESSION_COOKIE, _make_admin_session(ADMIN_LOGIN_TELEGRAM_ID, ADMIN_LOGIN_ID), max_age=ADMIN_SESSION_SECONDS,
                        httponly=True, secure=True, samesite="Strict", path="/")
    return response


async def admin_logout(request: web.Request):
    response = json_response({"ok": True})
    response.del_cookie(ADMIN_SESSION_COOKIE, path="/")
    return response


async def admin_session_status(request: web.Request):
    user = _read_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE, ""))
    if user:
        return json_response({"authenticated": True, "login_id": user.get("username"), "expires_in": ADMIN_SESSION_SECONDS})
    return json_response({"authenticated": False})


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

VALID_THEMES = {"arcade", "arghya", "nuclear-core", "alchemy-x"}
LEGACY_THEME_ALIASES = {
    "obsidian":"nuclear-core","graphite":"nuclear-core","classic":"nuclear-core",
    "emerald":"arghya","ivory":"arghya","cobalt":"arcade","amethyst":"arcade","midnight":"arcade","frost":"alchemy-x",
}

async def get_theme_preference(request):
    user_id = request["telegram_user"]["id"]
    async with request.app["pool"].connection() as conn:
        cur = await conn.execute("SELECT preference_value FROM bpsc_nexus_user_preferences WHERE telegram_id=%s AND preference_key='theme'", (user_id,))
        row = await cur.fetchone()
    theme = row["preference_value"] if row else "arcade"
    theme = LEGACY_THEME_ALIASES.get(theme, theme)
    if theme not in VALID_THEMES:
        theme = "arcade"
    return json_response({"theme": theme})

async def save_theme_preference(request):
    user_id = request["telegram_user"]["id"]
    try:
        body = await request.json()
        theme = str(body.get("theme", ""))
    except Exception:
        return json_response({"error": "Invalid preference payload."}, 400)
    theme = LEGACY_THEME_ALIASES.get(theme, theme)
    if theme not in VALID_THEMES:
        return json_response({"error": "Unsupported theme."}, 400)
    async with request.app["pool"].connection() as conn:
        await conn.execute("""INSERT INTO bpsc_nexus_user_preferences(telegram_id, preference_key, preference_value, updated_at)
            VALUES(%s, 'theme', %s, NOW())
            ON CONFLICT(telegram_id, preference_key) DO UPDATE SET preference_value=EXCLUDED.preference_value, updated_at=NOW()""", (user_id, theme))
    return json_response({"saved": True, "theme": theme})

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
        gate_ok,gate_reason=await _demo_gate(conn,int(uid),_is_series_admin(request['telegram_user']))
        if not gate_ok:
            return json_response({'error':('Register for the free demo to enter the Arena.' if gate_reason in ('register','no_trial') else 'Your demo has ended. Unlock a pass to keep battling.'),'demo_gate':'register' if gate_reason in ('register','no_trial') else 'expired'},403)
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
        gate_ok,gate_reason=await _demo_gate(conn,int(uid),_is_series_admin(request['telegram_user']))
        if not gate_ok:
            return json_response({'error':('Register for the free demo to enter the Arena.' if gate_reason in ('register','no_trial') else 'Your demo has ended. Unlock a pass to keep battling.'),'demo_gate':'register' if gate_reason in ('register','no_trial') else 'expired'},403)
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
INDIVIDUAL_PRODUCTS = {
    "ghatnachakra_ecology": {"name": "Ghatnachakra Ecology — Lifetime Edition", "price": 39, "duration_days": 0, "entitlement_key": "ghatnachakra_ecology", "active": True},
}

GROUP_AND_INDIVIDUAL_PLANS = [
    {"product_code":"group_starter","product_name":"Group Starter","amount_inr":99,"duration_days":30,"duration_label":"30 days · one Telegram group","scope":"group","description":"Activates group-quiz access for one selected Telegram group."},
    {"product_code":"group_elite","product_name":"Group Elite","amount_inr":249,"duration_days":0,"duration_label":"Lifetime · one Telegram group","scope":"group","description":"Lifetime group-quiz access for one selected Telegram group."},
    {"product_code":"individual_pro","product_name":"Individual Pro","amount_inr":79,"duration_days":60,"duration_label":"60 days · personal All Access","scope":"individual","description":"Personal All Access for your own Telegram account."},
    {"product_code":"individual_prime","product_name":"Individual Prime","amount_inr":149,"duration_days":0,"duration_label":"Lifetime · personal All Access","scope":"individual","description":"Lifetime personal All Access for your own Telegram account."},
]


async def payment_schema(conn):
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_payment_requests (
        id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL, product_code TEXT NOT NULL,
        product_name TEXT NOT NULL, duration_days INTEGER NOT NULL, amount_inr INTEGER NOT NULL,
        transaction_ref TEXT NOT NULL, screenshot_data TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
        submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), reviewed_at TIMESTAMPTZ,
        reviewed_by BIGINT, review_note TEXT, activated_pass_id BIGINT, UNIQUE(transaction_ref))""")
    await conn.execute("ALTER TABLE bpsc_payment_requests ADD COLUMN IF NOT EXISTS product_code TEXT NOT NULL DEFAULT 'all_access'")
    await conn.execute("ALTER TABLE bpsc_payment_requests ADD COLUMN IF NOT EXISTS group_chat_id BIGINT")
    await conn.execute("CREATE INDEX IF NOT EXISTS bpsc_payment_requests_status_idx ON bpsc_payment_requests(status, submitted_at DESC)")
    await conn.execute("ALTER TABLE bpsc_payment_requests ADD COLUMN IF NOT EXISTS coupon_code TEXT")
    await conn.execute("ALTER TABLE bpsc_payment_requests ADD COLUMN IF NOT EXISTS base_amount_inr INTEGER")
    await conn.execute("ALTER TABLE bpsc_payment_requests ADD COLUMN IF NOT EXISTS discount_amount_inr INTEGER NOT NULL DEFAULT 0")
    await conn.execute("ALTER TABLE bpsc_payment_requests ADD COLUMN IF NOT EXISTS pass_scope TEXT")
    await conn.execute("ALTER TABLE bpsc_payment_requests ADD COLUMN IF NOT EXISTS pass_vault_keys TEXT[]")
    await conn.execute("CREATE INDEX IF NOT EXISTS bpsc_payment_requests_coupon_idx ON bpsc_payment_requests(coupon_code, status)")
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_pass_offers (
        product_code TEXT PRIMARY KEY, pass_name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        amount_inr INTEGER NOT NULL CHECK(amount_inr >= 0), duration_days INTEGER NOT NULL DEFAULT 0 CHECK(duration_days >= 0),
        scope TEXT NOT NULL CHECK(scope IN ('all_access','individual','bank','group')),
        vault_keys TEXT[] NOT NULL DEFAULT ARRAY['*']::TEXT[], category TEXT NOT NULL DEFAULT 'Premium',
        is_active BOOLEAN NOT NULL DEFAULT TRUE, is_featured BOOLEAN NOT NULL DEFAULT FALSE,
        sort_order INTEGER NOT NULL DEFAULT 0, created_by BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_coupon_codes (
        code TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', discount_type TEXT NOT NULL CHECK(discount_type IN ('percent','fixed')),
        discount_value INTEGER NOT NULL CHECK(discount_value > 0), max_discount_inr INTEGER,
        min_order_inr INTEGER NOT NULL DEFAULT 0, starts_at TIMESTAMPTZ, expires_at TIMESTAMPTZ,
        max_redemptions INTEGER, per_user_limit INTEGER NOT NULL DEFAULT 1,
        applicable_product_codes TEXT[], is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_by BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    await conn.execute("ALTER TABLE bpsc_coupon_codes ADD COLUMN IF NOT EXISTS starts_at TIMESTAMPTZ")
    await conn.execute("ALTER TABLE bpsc_coupon_codes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
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
    if not page.is_file() and (ROOT / "membership.html").is_file():
        page = ROOT / "membership.html"
    if not page.is_file(): raise web.HTTPNotFound()
    response = web.FileResponse(page)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

async def _pass_offer_rows(conn, active_only=True):
    where = "WHERE is_active=TRUE" if active_only else ""
    cur=await conn.execute(f"SELECT product_code,pass_name,description,amount_inr,duration_days,scope,vault_keys,category,is_active,is_featured,sort_order FROM bpsc_pass_offers {where} ORDER BY sort_order,created_at DESC")
    rows=[]
    for r in await cur.fetchall():
        d=dict(r); d['vault_keys']=list(d.get('vault_keys') or [])
        d['duration_label']='Lifetime access' if int(d['duration_days'])==0 else f"{int(d['duration_days'])} days access"
        d['product_name']=d['pass_name']; d['name']=d['pass_name']
        d['description']=d.get('description') or ''; d['price']=int(d['amount_inr'])
        rows.append(d)
    return rows

async def _coupon_discount(conn, code, telegram_id, product_code, base_amount, lock=False):
    code=re.sub(r'[^A-Z0-9_-]','',str(code or '').upper())[:40]
    if not code: return {'code':'','discount':0,'final_amount':int(base_amount)}
    cur=await conn.execute("SELECT * FROM bpsc_coupon_codes WHERE code=%s"+(" FOR UPDATE" if lock else ""),(code,)); c=await cur.fetchone()
    if not c or not c['is_active']: raise ValueError('This coupon is invalid or inactive.')
    now=datetime.now(timezone.utc)
    for key,label in [('starts_at','not active yet'),('expires_at','has expired')]:
        val=c.get(key)
        if val and ((key=='starts_at' and val>now) or (key=='expires_at' and val<now)):
            raise ValueError(f'This coupon {label}.')
    if int(base_amount)<int(c['min_order_inr'] or 0): raise ValueError(f"Minimum order for this coupon is ₹{int(c['min_order_inr'])}.")
    applies=c.get('applicable_product_codes')
    if applies and product_code not in applies: raise ValueError('This coupon does not apply to the selected pass.')
    if c.get('max_redemptions') is not None:
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_payment_requests WHERE coupon_code=%s AND status IN ('pending','approved')",(code,)); used=int((await cur.fetchone())['n'])
        if used>=int(c['max_redemptions']): raise ValueError('This coupon has reached its redemption limit.')
    cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_payment_requests WHERE coupon_code=%s AND telegram_id=%s AND status IN ('pending','approved')",(code,int(telegram_id))); user_used=int((await cur.fetchone())['n'])
    if user_used>=int(c['per_user_limit'] or 1): raise ValueError('You have already used this coupon the maximum number of times.')
    if c['discount_type']=='percent': discount=(int(base_amount)*int(c['discount_value']))//100
    else: discount=int(c['discount_value'])
    if c.get('max_discount_inr') is not None: discount=min(discount,int(c['max_discount_inr']))
    discount=max(0,min(max(0,int(base_amount)-1),discount))
    return {'code':code,'discount':discount,'final_amount':int(base_amount)-discount,'description':c.get('description') or ''}

async def payment_coupon_validate(request):
    user=request['telegram_user']
    try:
        body=await request.json(); code=str(body.get('coupon_code') or ''); product_code=str(body.get('product_code') or '').strip().lower(); days=body.get('duration_days')
    except Exception: return json_response({'error':'Invalid coupon request.'},400)
    async with request.app['pool'].connection() as conn:
        await payment_schema(conn)
        base=None
        cur=await conn.execute("SELECT product_code,amount_inr,duration_days FROM bpsc_pass_offers WHERE product_code=%s AND is_active=TRUE",(product_code,)); offer=await cur.fetchone()
        if offer: base=int(offer['amount_inr'])
        elif product_code=='all_access':
            try: base=ALL_ACCESS_PRICES[int(days)]
            except Exception: return json_response({'error':'Choose a valid All Access duration.'},400)
        else:
            cur=await conn.execute("SELECT amount_inr FROM bpsc_individual_products WHERE product_code=%s AND is_active=TRUE",(product_code,)); prod=await cur.fetchone()
            plan=next((x for x in GROUP_AND_INDIVIDUAL_PLANS if x['product_code']==product_code),None)
            if prod: base=int(prod['amount_inr'])
            elif plan: base=int(plan['amount_inr'])
        if base is None: return json_response({'error':'Pass not found or inactive.'},404)
        try: result=await _coupon_discount(conn,code,user['id'],product_code,base)
        except ValueError as e: return json_response({'error':str(e)},400)
    return json_response({'valid':True,'coupon_code':result['code'],'base_amount':base,'discount_amount':result['discount'],'final_amount':result['final_amount'],'description':result.get('description','')})

async def payment_options(request):
    uid=request["telegram_user"]["id"]
    async with request.app["pool"].connection() as conn:
        await payment_schema(conn)
        cur=await conn.execute("SELECT id,product_name,duration_days,amount_inr,product_code,transaction_ref,status,submitted_at,reviewed_at FROM bpsc_payment_requests WHERE telegram_id=%s ORDER BY submitted_at DESC LIMIT 20",(uid,))
        history=await cur.fetchall()
        cur=await conn.execute("SELECT product_code,product_name,amount_inr,entitlement_key,duration_days FROM bpsc_individual_products WHERE is_active=TRUE ORDER BY product_name")
        products=await cur.fetchall()
        offers=await _pass_offer_rows(conn, True)
    plans = list(GROUP_AND_INDIVIDUAL_PLANS) + [
        {"product_code":"all_access","product_name":"All Access","amount_inr":price,"duration_days":days,
         "duration_label":f"{days} days · all vaults","scope":"individual",
         "description":"All personal question banks and vaults for the selected duration."}
        for days, price in ALL_ACCESS_PRICES.items()
    ]
    plans.extend([{k:v for k,v in o.items() if k not in ('vault_keys','category','is_active','sort_order')} | {'scope':('group' if o['scope']=='group' else 'individual' if o['scope'] in ('individual','all_access') else 'banks'),'custom_pass':True,'entitlement_key':','.join(o.get('vault_keys') or [])} for o in offers])
    return json_response({
        "plans": plans,
        "prices":[{"days":d,"price":p} for d,p in ALL_ACCESS_PRICES.items()],
        "products":[dict(r) for r in products],
        "history":[dict(r) for r in history],
    })

async def submit_payment(request):
    user=request["telegram_user"]
    try: body=await request.json()
    except Exception: return json_response({"error":"Invalid submission."},400)
    product_code=str(body.get("product_code") or "all_access").strip().lower()
    ref=str(body.get("transaction_ref") or "").strip().upper()
    screenshot=str(body.get("screenshot_data") or "")
    if not re.fullmatch(r"[A-Z0-9-]{6,40}",ref): return json_response({"error":"Enter a valid UPI transaction/reference ID (6–40 letters, digits or hyphens)."},400)
    if not screenshot.startswith("data:image/") or len(screenshot)>900000: return json_response({"error":"Upload a clear payment screenshot under 650 KB."},400)
    group_chat_id = None
    async with request.app["pool"].connection() as conn:
        await payment_schema(conn)
        cur=await conn.execute("SELECT product_code,pass_name,description,amount_inr,duration_days,scope,vault_keys FROM bpsc_pass_offers WHERE product_code=%s AND is_active=TRUE",(product_code,)); custom_offer=await cur.fetchone()
        plan = next((p for p in GROUP_AND_INDIVIDUAL_PLANS if p["product_code"] == product_code), None)
        if custom_offer:
            amount=int(custom_offer['amount_inr']); days=int(custom_offer['duration_days']); product_name=custom_offer['pass_name']
            if custom_offer['scope']=='group':
                raw=body.get('group_chat_id')
                try: group_chat_id=int(str(raw).strip())
                except (TypeError,ValueError): return json_response({'error':'Enter the numeric Telegram group ID for a Group Pass.'},400)
                try:
                    import aiohttp as _aiohttp
                    async with _aiohttp.ClientSession() as _sess:
                        async with _sess.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",params={'chat_id':group_chat_id,'user_id':user['id']},timeout=_aiohttp.ClientTimeout(total=8)) as _r: _payload=await _r.json()
                    status=((_payload or {}).get('result') or {}).get('status','')
                    if str(status).lower() not in ('administrator','creator'): return json_response({'error':'You must be an administrator of that Telegram group to purchase a Group Pass.'},403)
                except Exception: return json_response({'error':'Could not verify group admin status. Add the bot to the group and try again.'},400)
        elif product_code == "all_access":
            try:
                days=int(body.get("duration_days")); amount=ALL_ACCESS_PRICES[days]
            except Exception: return json_response({"error":"Choose a valid All Access duration."},400)
            product_name=f"All Access · {days} days"
        elif plan is not None:
            amount=int(plan["amount_inr"]); days=int(plan["duration_days"]); product_name=plan["product_name"]
            if plan["scope"]=="group":
                raw = body.get("group_chat_id")
                try: group_chat_id = int(str(raw).strip())
                except (TypeError, ValueError): return json_response({"error":"Enter the numeric Telegram group ID for a Group Pass."}, 400)
                # Verify admin status via Telegram Bot API (bot must be in the group)
                try:
                    import aiohttp as _aiohttp
                    async with _aiohttp.ClientSession() as _sess:
                        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
                        async with _sess.get(url, params={"chat_id": group_chat_id, "user_id": user["id"]}, timeout=_aiohttp.ClientTimeout(total=8)) as _r:
                            _payload = await _r.json()
                    status = ((_payload or {}).get("result") or {}).get("status", "")
                    if str(status).lower() not in ("administrator", "creator"):
                        return json_response({"error": "You must be an administrator of that Telegram group to purchase a Group Pass."}, 403)
                except Exception:
                    log.warning("Could not verify group admin status for group=%s user=%s", group_chat_id, user["id"])
                    return json_response({"error": "Could not verify your admin status in that group. Add the BPSC Nexus bot to the group and try again."}, 400)
        else:
            cur=await conn.execute("SELECT product_code,product_name,amount_inr,duration_days FROM bpsc_individual_products WHERE product_code=%s AND is_active=TRUE",(product_code,))
            product=await cur.fetchone()
            if not product: return json_response({"error":"This question bank is unavailable for new purchases."},404)
            amount=int(product["amount_inr"]); days=int(product["duration_days"]); product_name=product["product_name"]
        coupon_code=str(body.get('coupon_code') or '').strip().upper()
        try:
            async with conn.transaction():
                cur=await conn.execute("SELECT status FROM bpsc_payment_requests WHERE transaction_ref=%s",(ref,))
                if await cur.fetchone(): return json_response({"error":"This transaction/reference ID has already been submitted."},409)
                coupon={'code':'','discount':0,'final_amount':amount}
                if coupon_code:
                    coupon=await _coupon_discount(conn,coupon_code,user['id'],product_code,amount,lock=True)
                final_amount=int(coupon['final_amount'])
                cur=await conn.execute("""INSERT INTO bpsc_payment_requests(telegram_id,product_code,product_name,duration_days,amount_inr,transaction_ref,screenshot_data,group_chat_id,coupon_code,base_amount_inr,discount_amount_inr,pass_scope,pass_vault_keys)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,submitted_at""",(user["id"],product_code,product_name,days,final_amount,ref,screenshot,group_chat_id,coupon.get('code') or None,amount,int(coupon['discount']),custom_offer['scope'] if custom_offer else None,list(custom_offer.get('vault_keys') or []) if custom_offer else None))
                r=await cur.fetchone()
        except ValueError as e: return json_response({'error':str(e)},400)
    return json_response({"request_id":int(r["id"]),"status":"pending","amount":final_amount,"base_amount":amount,"discount_amount":int(coupon['discount']),"coupon_code":coupon.get('code',''),"product_code":product_code,"product_name":product_name,"submitted_at":r["submitted_at"].isoformat()},201)

async def index(request):
    page = ROOT / "static" / "index.html"
    if not page.is_file() and (ROOT / "index.html").is_file():
        page = ROOT / "index.html"
    if not page.is_file():
        log.error("Mini App frontend missing at %s", page)
        return web.Response(text="BPSC Nexus Mini App frontend is missing. Ensure static/index.html is committed.", status=500, content_type="text/plain")
    response = web.FileResponse(page)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


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
    await conn.execute("ALTER TABLE bpsc_nexus_test_series ADD COLUMN IF NOT EXISTS access_type VARCHAR(12) NOT NULL DEFAULT 'paid'")
    await conn.execute("ALTER TABLE bpsc_nexus_test_series ADD COLUMN IF NOT EXISTS logo_url TEXT NOT NULL DEFAULT ''")
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


async def _write_admin_audit(conn, admin_id: int, action: str, target: str, details: dict):
    await conn.execute("INSERT INTO bpsc_nexus_admin_audit(admin_telegram_id,action,target,details) VALUES(%s,%s,%s,%s::jsonb)",
        (admin_id,action,str(target)[:250],json.dumps(details,ensure_ascii=False)))


async def ensure_notifications_schema(conn):
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_notifications (
        id BIGSERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        link TEXT,
        link_label TEXT,
        segment TEXT NOT NULL DEFAULT 'all',
        is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
        silent BOOLEAN NOT NULL DEFAULT FALSE,
        recipients BIGINT NOT NULL DEFAULT 0,
        created_by BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    await conn.execute("CREATE INDEX IF NOT EXISTS bpsc_nexus_notifications_created_idx ON bpsc_nexus_notifications(created_at DESC)")
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_notification_reads (
        notification_id BIGINT NOT NULL REFERENCES bpsc_nexus_notifications(id) ON DELETE CASCADE,
        telegram_id BIGINT NOT NULL,
        read_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(notification_id, telegram_id)
    )""")
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_settings (
        key TEXT PRIMARY KEY,
        value JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")


async def _get_setting(conn, key: str, default=None):
    await ensure_notifications_schema(conn)
    cur = await conn.execute("SELECT value FROM bpsc_nexus_settings WHERE key=%s", (key,))
    row = await cur.fetchone()
    if not row: return default
    v = row["value"]
    return v if isinstance(v, dict) else json.loads(v or "{}")


async def _set_setting(conn, key: str, value: dict):
    await ensure_notifications_schema(conn)
    await conn.execute("""INSERT INTO bpsc_nexus_settings(key,value,updated_at) VALUES(%s,%s::jsonb,NOW())
        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()""",
        (key, json.dumps(value, ensure_ascii=False)))


async def ensure_access_schema(conn):
    await conn.execute("ALTER TABLE bank_quizzes ADD COLUMN IF NOT EXISTS access_type VARCHAR(12) NOT NULL DEFAULT 'inherit'")
    await conn.execute("ALTER TABLE bank_quizzes ADD COLUMN IF NOT EXISTS admin_hidden BOOLEAN NOT NULL DEFAULT FALSE")
    await conn.execute("ALTER TABLE question_bank_subjects ADD COLUMN IF NOT EXISTS access_type VARCHAR(12) NOT NULL DEFAULT 'free'")
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_demo_config (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton=TRUE),
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        duration_hours INTEGER NOT NULL DEFAULT 48 CHECK(duration_hours BETWEEN 1 AND 720),
        updated_by BIGINT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    await conn.execute("INSERT INTO bpsc_nexus_demo_config(singleton,enabled,duration_hours) VALUES(TRUE,TRUE,48) ON CONFLICT(singleton) DO NOTHING")
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_demo_trials (
        telegram_id BIGINT PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        reset_by BIGINT,
        reset_note TEXT NOT NULL DEFAULT ''
    )""")
    await conn.execute("CREATE INDEX IF NOT EXISTS bpsc_nexus_demo_trials_expiry_idx ON bpsc_nexus_demo_trials(expires_at)")
    await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_demo_registrations (
        telegram_id BIGINT PRIMARY KEY,
        telegram_username VARCHAR(64) NOT NULL DEFAULT '',
        submitted_contact VARCHAR(100) NOT NULL,
        contact_type VARCHAR(16) NOT NULL DEFAULT 'username',
        registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        source VARCHAR(32) NOT NULL DEFAULT 'miniapp'
    )""")
    await conn.execute("CREATE INDEX IF NOT EXISTS bpsc_nexus_demo_registrations_registered_idx ON bpsc_nexus_demo_registrations(registered_at DESC)")


async def _ensure_trial(conn, telegram_id: int):
    await conn.execute("""INSERT INTO bpsc_nexus_demo_trials(telegram_id,started_at,expires_at)
        SELECT %s,NOW(),NOW() + (duration_hours * INTERVAL '1 hour')
        FROM bpsc_nexus_demo_config WHERE singleton=TRUE AND enabled=TRUE
        ON CONFLICT(telegram_id) DO NOTHING""", (telegram_id,))
    cur = await conn.execute("SELECT telegram_id,started_at,expires_at FROM bpsc_nexus_demo_trials WHERE telegram_id=%s", (telegram_id,))
    trial = await cur.fetchone()
    cur = await conn.execute("SELECT enabled,duration_hours FROM bpsc_nexus_demo_config WHERE singleton=TRUE")
    config = await cur.fetchone()
    return trial, config


async def _has_all_access_pass(conn, telegram_id: int) -> bool:
    cur = await conn.execute("SELECT to_regclass('public.premium_passes') AS p, to_regclass('public.app_users') AS u")
    tables = await cur.fetchone()
    if not tables or not tables['p'] or not tables['u']:
        return False
    cur = await conn.execute("""SELECT EXISTS(
        SELECT 1 FROM app_users u JOIN premium_passes pp ON pp.user_id=u.id
        WHERE u.telegram_id=%s AND pp.revoked_at IS NULL
          AND (pp.expires_at IS NULL OR pp.expires_at>NOW())
          AND (pp.pass_code IN ('all_access','legacy') OR pp.vault_keys @> ARRAY['*']::TEXT[])
    ) AS ok""", (telegram_id,))
    return bool((await cur.fetchone())['ok'])


async def _demo_gate(conn, telegram_id: int, is_admin: bool = False) -> tuple[bool, str]:
    """Server-side access gate for quiz attempts, Arena, and custom quiz creation."""
    if is_admin:
        return True, ''
    try:
        if await _has_all_access_pass(conn, telegram_id):
            return True, ''
    except Exception:
        log.exception('Pass check failed for %s', telegram_id)
    await ensure_access_schema(conn)
    cur = await conn.execute('SELECT 1 FROM bpsc_nexus_demo_registrations WHERE telegram_id=%s', (telegram_id,))
    registered = bool(await cur.fetchone())
    if not registered:
        return False, 'register'
    cur = await conn.execute('SELECT expires_at FROM bpsc_nexus_demo_trials WHERE telegram_id=%s', (telegram_id,))
    trial = await cur.fetchone()
    if not trial:
        return False, 'no_trial'
    expires_at = trial['expires_at']
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return False, 'expired'
    return True, ''


async def _has_subject_entitlement(conn, telegram_id: int, subject: str) -> bool:
    normalized=re.sub(r'[^a-z0-9]+','',str(subject or '').lower())
    if not normalized: return False
    cur=await conn.execute("""SELECT EXISTS(
        SELECT 1 FROM app_users u JOIN premium_passes pp ON pp.user_id=u.id
        WHERE u.telegram_id=%s AND pp.revoked_at IS NULL AND (pp.expires_at IS NULL OR pp.expires_at>NOW())
          AND (pp.pass_code IN ('all_access','legacy') OR pp.vault_keys @> ARRAY['*']::TEXT[]
            OR EXISTS(SELECT 1 FROM unnest(pp.vault_keys) k WHERE regexp_replace(lower(k),'[^a-z0-9]','','g')=%s)
            OR regexp_replace(lower(pp.pass_code),'[^a-z0-9]','','g')=%s)
    ) AS ok""",(telegram_id,normalized,normalized))
    return bool((await cur.fetchone())['ok'])


async def _access_state(conn, telegram_id: int, is_admin: bool = False, create_trial: bool = True):
    if create_trial and not is_admin:
        trial, config = await _ensure_trial(conn, telegram_id)
    else:
        cur=await conn.execute("SELECT telegram_id,started_at,expires_at FROM bpsc_nexus_demo_trials WHERE telegram_id=%s",(telegram_id,)); trial=await cur.fetchone()
        cur=await conn.execute("SELECT enabled,duration_hours FROM bpsc_nexus_demo_config WHERE singleton=TRUE"); config=await cur.fetchone()
    has_pass = await _has_all_access_pass(conn, telegram_id)
    active = bool(trial and trial['expires_at'] > datetime.now(timezone.utc))
    return {
        'is_admin': bool(is_admin), 'demo_enabled': bool(config and config['enabled']),
        'demo_duration_hours': int(config['duration_hours']) if config else 48,
        'demo_started_at': trial['started_at'].isoformat() if trial else None,
        'demo_expires_at': trial['expires_at'].isoformat() if trial else None,
        'demo_active': active, 'demo_expired': bool(trial and not active),
        'seconds_remaining': max(0, int((trial['expires_at']-datetime.now(timezone.utc)).total_seconds())) if active else 0,
        'has_paid_access': bool(is_admin or active or has_pass), 'has_all_access_pass': has_pass,
    }


async def access_status(request):
    user=request['telegram_user']; is_admin=_is_series_admin(user)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        await ensure_notifications_schema(conn)
        cur=await conn.execute('SELECT 1 FROM bpsc_nexus_demo_registrations WHERE telegram_id=%s',(user['id'],))
        registered=bool(await cur.fetchone())
        state=await _access_state(conn,user['id'],is_admin,False)
        if registered:
            await conn.execute('UPDATE bpsc_nexus_demo_registrations SET last_seen_at=NOW() WHERE telegram_id=%s',(user['id'],))
        existed=bool(state['demo_started_at'])
        if not is_admin and registered and state['demo_enabled'] and not existed:
            await _ensure_trial(conn,user['id'])
            state=await _access_state(conn,user['id'],is_admin,False)
            state['trial_started_now']=bool(state['demo_active'])
        else:
            state['trial_started_now']=False
        state['registration_required']=False
        has_pass=bool(state.get('has_all_access_pass'))
        if is_admin: state['demo_gate']='admin'
        elif has_pass: state['demo_gate']='paid'
        elif not registered:
            state['demo_gate']='register'
            state['registration_required']=True
            state['trial_started_now']=False
            state['demo_message_title']='Register to unlock your demo'
        elif state.get('demo_active'): state['demo_gate']='active'
        else: state['demo_gate']='expired' if state.get('demo_expired') else 'no_trial'
        banner=await _get_setting(conn,'promo_banner',{}) or {}
        maint=await _get_setting(conn,'maintenance',{'enabled':False}) or {}
        state['promo_banner']={'enabled':bool(banner.get('enabled')),'tag':banner.get('tag',''),'title':banner.get('title',''),'message':banner.get('message',''),'link':banner.get('link',''),'link_label':banner.get('link_label','')}
        state['maintenance_mode']=bool(maint.get('enabled'))
    state['demo_message_title']=state.get('demo_message_title') or (f"Your {state['demo_duration_hours']}-hour BPSC Nexus Demo is live" if state.get('demo_active') else ('Your demo has ended' if state.get('demo_expired') else 'Explore BPSC Nexus'))
    return json_response(state)


async def _verify_telegram_identity(user: dict, contact: str, contact_type: str) -> tuple[bool, str]:
    """Validate contact against Telegram's signed Mini App identity, then cross-check via Bot API when available."""
    try:
        self_id=int(user['id'])
    except (KeyError,TypeError,ValueError):
        return False,'Could not verify the Telegram account opening this app.'
    self_username=(user.get('username') or '').lower().lstrip('@')
    if contact_type=='telegram_id':
        try: entered=int(contact)
        except (TypeError,ValueError): return False,'Enter a numeric Telegram user ID.'
        if entered!=self_id:
            return False,'That is not your own Telegram ID. Open @userinfobot to get yours, or open this Mini App from your own account.'
    else:
        entered=contact.lower().lstrip('@')
        if not self_username:
            return False,'Your Telegram account has no public username. Please enter your numeric Telegram ID instead.'
        if entered!=self_username:
            return False,'That username does not belong to the account opening this app. Enter the @username shown in your own Telegram profile.'
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            url=f'https://api.telegram.org/bot{BOT_TOKEN}/getChat'
            async with session.get(url,params={'chat_id':self_id},timeout=aiohttp.ClientTimeout(total=6)) as response:
                payload=await response.json()
        if payload.get('ok'):
            result=payload.get('result') or {}
            api_id=int(result.get('id') or 0)
            api_username=(result.get('username') or '').lower().lstrip('@')
            if api_id and api_id!=self_id:
                return False,'Telegram identity mismatch. Re-open the Mini App from your own Telegram account.'
            if contact_type=='username' and api_username and api_username!=entered:
                return False,f'Your Telegram username is @{api_username}. Please enter that exact username.'
        else:
            log.info('Telegram getChat could not resolve %s: %s',self_id,payload.get('description'))
    except Exception as exc:
        # Signed initData remains authoritative during temporary Telegram API failures.
        log.warning('Telegram verify ping failed for %s: %s',self_id,exc)
    return True,''


async def register_demo(request):
    user=request['telegram_user']; tid=int(user['id'])
    if not _demo_verify_allowed(tid):
        return json_response({'error':'Too many verification attempts. Wait 1 hour and try again.','retry_after_seconds':_DEMO_VERIFY_WINDOW},429)
    async def _log_invalid_contact(attempted: str, kind: str, reason: str):
        try:
            async with request.app['pool'].connection() as log_conn:
                await ensure_demo_verify_schema(log_conn)
                await log_conn.execute('INSERT INTO bpsc_nexus_demo_verify_log (telegram_id,attempted_contact,contact_type,success,reason) VALUES(%s,%s,%s,FALSE,%s)',(tid,str(attempted)[:100],kind,reason[:500]))
        except Exception:
            log.exception('Could not write invalid demo verification audit for %s',tid)
    try:
        body=await request.json(); contact=str(body.get('contact','')).strip()
    except Exception:
        _demo_verify_record(tid);await _log_invalid_contact('', 'unknown', 'Invalid or missing JSON contact')
        return json_response({'error':'Enter your Telegram username or Telegram user ID.'},400)
    contact=contact[1:] if contact.startswith('@') else contact
    if not contact or len(contact)>100:
        _demo_verify_record(tid);await _log_invalid_contact(contact, 'unknown', 'Contact empty or too long')
        return json_response({'error':'Enter a valid Telegram username or Telegram user ID.'},400)
    if contact.isdigit():
        if int(contact)<=0:
            _demo_verify_record(tid);await _log_invalid_contact(contact,'telegram_id','Telegram ID must be positive')
            return json_response({'error':'Telegram user ID must be a positive number.'},400)
        contact_type='telegram_id'
    elif re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',contact):
        contact_type='username';contact='@'+contact
    else:
        _demo_verify_record(tid);await _log_invalid_contact(contact,'unknown','Invalid username or ID format')
        return json_response({'error':'Use a Telegram username (5–32 characters) or numeric Telegram user ID.'},400)
    ok,err=await _verify_telegram_identity(user,contact,contact_type)
    async with request.app['pool'].connection() as conn:
        await ensure_demo_verify_schema(conn)
        await ensure_access_schema(conn)
        await conn.execute('INSERT INTO bpsc_nexus_demo_verify_log (telegram_id,attempted_contact,contact_type,success,reason) VALUES(%s,%s,%s,%s,%s)',(tid,contact,contact_type,bool(ok),err or ''))
        if not ok:
            _demo_verify_record(tid)
            await _write_admin_audit(conn,tid,'demo_registration_rejected',str(tid),{'submitted_contact':contact,'contact_type':contact_type,'reason':err})
            return json_response({'error':err},403)
        cur=await conn.execute('SELECT enabled,duration_hours FROM bpsc_nexus_demo_config WHERE singleton=TRUE')
        config=await cur.fetchone()
        if not config or not config['enabled']:
            return json_response({'error':'The demo is currently closed. Please contact the BPSC Nexus team.'},403)
        await conn.execute('''INSERT INTO bpsc_nexus_demo_registrations
            (telegram_id,telegram_username,submitted_contact,contact_type,registered_at,last_seen_at,source)
            VALUES(%s,%s,%s,%s,NOW(),NOW(),'miniapp')
            ON CONFLICT(telegram_id) DO UPDATE SET
              telegram_username=EXCLUDED.telegram_username,submitted_contact=EXCLUDED.submitted_contact,
              contact_type=EXCLUDED.contact_type,last_seen_at=NOW()''',
            (tid,str(user.get('username') or '')[:64],contact,contact_type))
        # _ensure_trial is idempotent: re-registration never resets an existing expiry.
        await _ensure_trial(conn,tid)
        await _write_admin_audit(conn,tid,'demo_registration',str(tid),{'submitted_contact':contact,'contact_type':contact_type,'verified':True})
        state=await _access_state(conn,tid,_is_series_admin(user),False)
        state['demo_gate']='admin' if _is_series_admin(user) else ('active' if state.get('demo_active') else 'expired')
    state.update({'registration_required':False,'trial_started_now':bool(state.get('demo_active')),'demo_message_title':(f"Your {state['demo_duration_hours']}-hour BPSC Nexus Demo is live" if state.get('demo_active') else 'Your demo is registered')})
    return json_response(state)


async def demo_status(request):
    """Lightweight authoritative gate check before client-side attempt actions."""
    user=request['telegram_user']
    async with request.app['pool'].connection() as conn:
        allowed,reason=await _demo_gate(conn,int(user['id']),_is_series_admin(user))
    return json_response({'allowed':allowed,'reason':reason})


async def admin_demo_registrations(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try: days=max(1,min(90,int(request.query.get('days','2'))))
    except Exception: days=2
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        cur=await conn.execute("""SELECT COUNT(*) FILTER (WHERE registered_at >= NOW() - (%s * INTERVAL '1 day')) AS total,
            COUNT(*) FILTER (WHERE registered_at >= NOW() - INTERVAL '2 days' AND started_at >= NOW() - INTERVAL '2 days') AS started_in_window,
            COUNT(*) FILTER (WHERE expires_at>NOW()) AS active_now,
            COUNT(*) FILTER (WHERE expires_at IS NOT NULL AND expires_at<=NOW()) AS expired,
            COUNT(*) FILTER (WHERE started_at IS NULL) AS registered_not_started
            FROM (SELECT r.telegram_id,r.registered_at,t.started_at,t.expires_at FROM bpsc_nexus_demo_registrations r LEFT JOIN bpsc_nexus_demo_trials t ON t.telegram_id=r.telegram_id) x""",(days,))
        stats=dict(await cur.fetchone())
        cur=await conn.execute("""SELECT r.telegram_id,r.telegram_username,r.submitted_contact,r.contact_type,r.registered_at,
            t.started_at AS demo_started_at,t.expires_at,
            CASE WHEN t.expires_at>NOW() THEN 'active' WHEN t.expires_at IS NOT NULL THEN 'expired' ELSE 'registered' END AS demo_status
            FROM bpsc_nexus_demo_registrations r LEFT JOIN bpsc_nexus_demo_trials t ON t.telegram_id=r.telegram_id
            WHERE r.registered_at >= NOW() - (%s * INTERVAL '1 day')
            ORDER BY r.registered_at DESC LIMIT 1000""",(days,))
        rows=[dict(x) for x in await cur.fetchall()]
    for row in rows:
        for key in ('registered_at','demo_started_at','expires_at'):
            if row.get(key): row[key]=row[key].isoformat()
    return json_response({'stats':stats,'registrations':rows,'days':days})


async def admin_overview(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        state=await _access_state(conn,user['id'],True,False)
        cur=await conn.execute("SELECT (SELECT COUNT(*) FROM question_bank WHERE status='ready') AS questions,(SELECT COUNT(*) FROM bank_quizzes WHERE is_published=TRUE) AS quizzes,(SELECT COUNT(*) FROM bpsc_nexus_test_series) AS series,(SELECT COUNT(*) FROM bpsc_nexus_demo_trials WHERE expires_at>NOW()) AS active_demos,(SELECT COUNT(*) FROM bpsc_nexus_demo_trials WHERE expires_at<=NOW()) AS expired_demos,(SELECT COUNT(DISTINCT telegram_id) FROM bank_quiz_attempts) AS quiz_users")
        stats=dict(await cur.fetchone())
        cur=await conn.execute("SELECT enabled,duration_hours FROM bpsc_nexus_demo_config WHERE singleton=TRUE")
        config=dict(await cur.fetchone())
    return json_response({'stats':stats,'demo':config,'is_admin':True})


async def admin_banks(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        cur=await conn.execute("""SELECT qb.subject AS name,COUNT(*) AS question_count,
            COALESCE(MAX(s.subject_type),'subject') AS subject_type,
            COALESCE(MAX(s.access_type),'free') AS access_type
            FROM question_bank qb LEFT JOIN question_bank_subjects s ON LOWER(BTRIM(s.name))=LOWER(BTRIM(qb.subject)) AND s.archived_at IS NULL
            GROUP BY qb.subject ORDER BY LOWER(qb.subject) LIMIT 500""")
        rows=[dict(r) for r in await cur.fetchall()]
    return json_response({'banks':rows})


async def admin_set_bank_access(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json(); name=str(body.get('name','')).strip(); access=str(body.get('access_type','')).lower()
        if not name or len(name)>180 or access not in ('free','paid'): raise ValueError()
    except Exception: return json_response({'error':'Provide a valid bank name and free/paid access type.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        cur=await conn.execute("SELECT id FROM question_bank_subjects WHERE LOWER(BTRIM(name))=LOWER(BTRIM(%s)) LIMIT 1",(name,)); existing=await cur.fetchone()
        if existing:
            await conn.execute("UPDATE question_bank_subjects SET access_type=%s,archived_at=NULL WHERE id=%s",(access,existing['id']))
        else:
            await conn.execute("INSERT INTO question_bank_subjects(name,created_by_telegram_id,subject_type,access_type) VALUES(%s,%s,'subject',%s)",(name,user['id'],access))
        await _write_admin_audit(conn,user['id'],'bank_access',name,{'access_type':access})
    return json_response({'name':name,'access_type':access,'updated':True})


async def admin_quizzes(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        cur=await conn.execute("""SELECT q.id,q.title,q.subject,q.is_published,q.access_type,COUNT(qq.question_id) AS total_questions
            FROM bank_quizzes q LEFT JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
            GROUP BY q.id ORDER BY q.id DESC LIMIT 300""")
        rows=[dict(r) for r in await cur.fetchall()]
    return json_response({'quizzes':rows})


async def admin_set_quiz_access(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try: qid=int(request.match_info['quiz_id']); body=await request.json(); access=str(body.get('access_type','')).lower()
    except Exception: return json_response({'error':'Invalid quiz access update.'},400)
    if access not in ('inherit','free','paid'): return json_response({'error':'Access must be inherit, free or paid.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        cur=await conn.execute("UPDATE bank_quizzes SET access_type=%s WHERE id=%s RETURNING id,title,access_type",(access,qid)); row=await cur.fetchone()
        if not row:return json_response({'error':'Quiz not found.'},404)
        await _write_admin_audit(conn,user['id'],'quiz_access',str(qid),{'access_type':access})
    return json_response({'quiz':dict(row),'updated':True})


async def admin_set_series_access(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try: sid=int(request.match_info['series_id']); body=await request.json(); access=str(body.get('access_type','')).lower()
    except Exception: return json_response({'error':'Invalid series access update.'},400)
    if access not in ('free','paid'): return json_response({'error':'Access must be free or paid.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        cur=await conn.execute("UPDATE bpsc_nexus_test_series SET access_type=%s,updated_at=NOW() WHERE id=%s RETURNING id,title,access_type",(access,sid)); row=await cur.fetchone()
        if not row:return json_response({'error':'Series not found.'},404)
        await _write_admin_audit(conn,user['id'],'series_access',str(sid),{'access_type':access})
    return json_response({'series':dict(row),'updated':True})


async def admin_cleanup_catalog(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    await qb.ensure_schema(request.app['pool'])
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        await ensure_test_series_schema(conn)
        cur=await conn.execute("""WITH subject_names AS (
                SELECT id,name,subject_type,archived_at FROM question_bank_subjects
                UNION ALL
                SELECT NULL::BIGINT, q.subject, 'subject'::TEXT, NULL::TIMESTAMPTZ
                FROM (SELECT DISTINCT subject FROM question_bank WHERE NULLIF(BTRIM(subject),'') IS NOT NULL) q
                WHERE NOT EXISTS (SELECT 1 FROM question_bank_subjects s WHERE LOWER(BTRIM(s.name))=LOWER(BTRIM(q.subject)))
            )
            SELECT n.id,n.name,n.subject_type,(n.archived_at IS NOT NULL) AS archived,
                COALESCE(qc.ready_questions,0) AS ready_questions,COALESCE(qc.total_questions,0) AS total_questions,
                COALESCE(qc.snapshot_count,0) AS snapshot_count,
                COALESCE(qc.quiz_count,0) AS quiz_count,COALESCE(qc.series_count,0) AS series_count
            FROM subject_names n
            LEFT JOIN LATERAL (
                SELECT COUNT(*) FILTER (WHERE qb.status='ready') AS ready_questions,
                    COUNT(*) AS total_questions,
                    (SELECT COUNT(*) FROM bank_quiz_attempt_question_snapshots ss WHERE LOWER(BTRIM(ss.subject))=LOWER(BTRIM(n.name))) AS snapshot_count,
                    (SELECT COUNT(DISTINCT q.id) FROM bank_quizzes q WHERE LOWER(BTRIM(q.subject))=LOWER(BTRIM(n.name))) AS quiz_count,
                    (SELECT COUNT(*) FROM bpsc_nexus_test_series ts WHERE LOWER(BTRIM(ts.subject))=LOWER(BTRIM(n.name))) AS series_count
                FROM question_bank qb WHERE LOWER(BTRIM(qb.subject))=LOWER(BTRIM(n.name))
            ) qc ON TRUE
            ORDER BY (COALESCE(qc.ready_questions,0)=0) DESC,LOWER(n.name),n.name""")
        subjects=[dict(r) for r in await cur.fetchall()]
        cur=await conn.execute("""SELECT q.id,q.title,q.subject,q.is_published,q.admin_hidden,
                COALESCE(qc.ready_questions,0) AS ready_questions,COALESCE(qc.linked_questions,0) AS linked_questions,
                COALESCE(at.attempt_count,0) AS attempt_count,COALESCE(ch.challenge_count,0) AS challenge_count,
                COALESCE(st.series_count,0) AS series_count
            FROM bank_quizzes q
            LEFT JOIN LATERAL (SELECT COUNT(DISTINCT qb.id) FILTER (WHERE qb.status='ready') AS ready_questions,
                COUNT(DISTINCT qq.question_id) AS linked_questions FROM bank_quiz_questions qq
                LEFT JOIN question_bank qb ON qb.id=qq.question_id WHERE qq.quiz_id=q.id) qc ON TRUE
            LEFT JOIN LATERAL (SELECT COUNT(*) AS attempt_count FROM bank_quiz_attempts a WHERE a.quiz_id=q.id) at ON TRUE
            LEFT JOIN LATERAL (SELECT COUNT(*) AS challenge_count FROM bpsc_leaderboard_daily_challenges c WHERE c.quiz_id=q.id) ch ON TRUE
            LEFT JOIN LATERAL (SELECT COUNT(*) AS series_count FROM bpsc_nexus_test_series_tests st WHERE st.quiz_id=q.id) st ON TRUE
            ORDER BY (COALESCE(qc.ready_questions,0)=0) DESC,q.id DESC LIMIT 500""")
        quizzes=[dict(r) for r in await cur.fetchall()]
        cur=await conn.execute("SELECT to_regclass('public.group_quiz_sessions') AS table_name")
        group_table=await cur.fetchone()
        if group_table and group_table['table_name']:
            cur=await conn.execute("SELECT quiz_id,COUNT(*) AS n FROM group_quiz_sessions GROUP BY quiz_id")
            group_refs={int(r['quiz_id']):int(r['n']) for r in await cur.fetchall()}
            for item in quizzes: item['group_session_count']=group_refs.get(int(item['id']),0)
        else:
            for item in quizzes: item['group_session_count']=0
        cur=await conn.execute("""SELECT s.id,s.title,s.exam_type,s.is_published,
                COUNT(DISTINCT st.quiz_id) AS linked_tests,
                COUNT(DISTINCT st.quiz_id) FILTER (WHERE q.is_published=TRUE AND COALESCE(q.admin_hidden,FALSE)=FALSE AND EXISTS (
                    SELECT 1 FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
                    WHERE qq.quiz_id=q.id AND qb.status='ready')) AS ready_tests
            FROM bpsc_nexus_test_series s LEFT JOIN bpsc_nexus_test_series_tests st ON st.series_id=s.id
            LEFT JOIN bank_quizzes q ON q.id=st.quiz_id GROUP BY s.id ORDER BY (COUNT(DISTINCT st.quiz_id)=0) DESC,s.id DESC LIMIT 300""")
        series=[dict(r) for r in await cur.fetchall()]
    return json_response({'subjects':subjects,'quizzes':quizzes,'series':series})


async def admin_remove_subject(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json(); name=str(body.get('name') or '').strip(); action=str(body.get('action') or '').lower()
        if not name or len(name)>180 or action not in ('archive','delete','restore'): raise ValueError()
    except Exception: return json_response({'error':'Choose a valid subject and action.'},400)
    await qb.ensure_schema(request.app['pool'])
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        await ensure_test_series_schema(conn)
        if action=='restore':
            cur=await conn.execute("UPDATE question_bank_subjects SET archived_at=NULL WHERE LOWER(BTRIM(name))=LOWER(BTRIM(%s)) RETURNING id,name",(name,)); row=await cur.fetchone()
            if not row:return json_response({'error':'Registered subject not found to restore.'},404)
            await _write_admin_audit(conn,admin['id'],'subject_restore',row['name'],{})
            return json_response({'name':row['name'],'action':'restore','success':True})
        cur=await conn.execute("""SELECT COUNT(*) FILTER (WHERE status='ready') AS ready_count,COUNT(*) AS total_count
            FROM question_bank WHERE LOWER(BTRIM(subject))=LOWER(BTRIM(%s))""",(name,)); counts=dict(await cur.fetchone())
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bank_quizzes WHERE LOWER(BTRIM(subject))=LOWER(BTRIM(%s))",(name,)); quiz_count=int((await cur.fetchone())['n'])
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bank_quiz_attempt_question_snapshots WHERE LOWER(BTRIM(subject))=LOWER(BTRIM(%s))",(name,)); snapshot_count=int((await cur.fetchone())['n'])
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_nexus_test_series WHERE LOWER(BTRIM(subject))=LOWER(BTRIM(%s))",(name,)); series_count=int((await cur.fetchone())['n'])
        if action=='delete':
            if int(counts['total_count'] or 0) or quiz_count or snapshot_count or series_count:
                return json_response({'error':'This subject still has questions, quizzes, historical attempts or test-series references. Archive it instead.','ready_questions':int(counts['ready_count'] or 0),'total_questions':int(counts['total_count'] or 0),'quizzes':quiz_count,'history_records':snapshot_count,'series':series_count},409)
            cur=await conn.execute("DELETE FROM question_bank_subjects WHERE LOWER(BTRIM(name))=LOWER(BTRIM(%s)) RETURNING name",(name,)); row=await cur.fetchone()
            if not row:return json_response({'error':'Empty subject record not found.'},404)
            await _write_admin_audit(conn,admin['id'],'subject_delete',row['name'],{'questions':0})
            return json_response({'name':row['name'],'action':'delete','success':True})
        cur=await conn.execute("UPDATE question_bank_subjects SET archived_at=NOW() WHERE LOWER(BTRIM(name))=LOWER(BTRIM(%s)) RETURNING name",(name,)); row=await cur.fetchone()
        if not row:
            cur=await conn.execute("INSERT INTO question_bank_subjects(name,created_by_telegram_id,subject_type,access_type,archived_at) VALUES(%s,%s,'subject','free',NOW()) RETURNING name",(name,admin['id'])); row=await cur.fetchone()
        await _write_admin_audit(conn,admin['id'],'subject_archive',row['name'],{'ready_questions':int(counts['ready_count'] or 0),'total_questions':int(counts['total_count'] or 0)})
    return json_response({'name':row['name'],'action':'archive','success':True,'questions_preserved':int(counts['total_count'] or 0)})


async def admin_remove_quiz(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try: qid=int(request.match_info['quiz_id']); body=await request.json(); action=str(body.get('action') or '').lower()
    except Exception: return json_response({'error':'Invalid test action.'},400)
    if action not in ('archive','restore','delete'): return json_response({'error':'Action must be archive, restore or delete.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        cur=await conn.execute("SELECT id,title,is_published,admin_hidden FROM bank_quizzes WHERE id=%s",(qid,)); quiz=await cur.fetchone()
        if not quiz:return json_response({'error':'Test not found.'},404)
        if action=='archive':
            await conn.execute("UPDATE bank_quizzes SET admin_hidden=TRUE WHERE id=%s",(qid,))
            await _write_admin_audit(conn,admin['id'],'quiz_archive',str(qid),{'title':quiz['title']})
            return json_response({'id':qid,'title':quiz['title'],'action':'archive','success':True})
        if action=='restore':
            await conn.execute("UPDATE bank_quizzes SET admin_hidden=FALSE WHERE id=%s",(qid,))
            await _write_admin_audit(conn,admin['id'],'quiz_restore',str(qid),{'title':quiz['title']})
            return json_response({'id':qid,'title':quiz['title'],'action':'restore','success':True})
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bank_quiz_attempts WHERE quiz_id=%s",(qid,)); attempts=int((await cur.fetchone())['n'])
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_leaderboard_daily_challenges WHERE quiz_id=%s",(qid,)); challenges=int((await cur.fetchone())['n'])
        cur=await conn.execute("SELECT to_regclass('public.group_quiz_sessions') AS table_name")
        group_table=await cur.fetchone(); group_sessions=0
        if group_table and group_table['table_name']:
            cur=await conn.execute("SELECT COUNT(*) AS n FROM group_quiz_sessions WHERE quiz_id=%s",(qid,)); group_sessions=int((await cur.fetchone())['n'])
        if attempts or challenges or group_sessions:
            return json_response({'error':'This test has attempt history, leaderboard challenges or Telegram group-quiz references. Archive it to remove it from student navigation while preserving records.','attempts':attempts,'challenges':challenges,'group_sessions':group_sessions},409)
        await conn.execute("DELETE FROM bank_quizzes WHERE id=%s",(qid,))
        await _write_admin_audit(conn,admin['id'],'quiz_delete',str(qid),{'title':quiz['title'],'attempts':0})
    return json_response({'id':qid,'title':quiz['title'],'action':'delete','success':True})


async def admin_remove_series(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try: sid=int(request.match_info['series_id'])
    except Exception:return json_response({'error':'Invalid test-series ID.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_test_series_schema(conn)
        cur=await conn.execute("DELETE FROM bpsc_nexus_test_series WHERE id=%s RETURNING id,title",(sid,)); row=await cur.fetchone()
        if not row:return json_response({'error':'Test series not found.'},404)
        await _write_admin_audit(conn,admin['id'],'series_delete',str(sid),{'title':row['title']})
    return json_response({'id':sid,'title':row['title'],'success':True})


async def admin_demo_settings(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json(); enabled=bool(body.get('enabled')); hours=max(1,min(720,int(body.get('duration_hours',48))) )
    except Exception: return json_response({'error':'Provide valid demo settings.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        await conn.execute("UPDATE bpsc_nexus_demo_config SET enabled=%s,duration_hours=%s,updated_by=%s,updated_at=NOW() WHERE singleton=TRUE",(enabled,hours,user['id']))
        await _write_admin_audit(conn,user['id'],'demo_settings','global',{'enabled':enabled,'duration_hours':hours})
    return json_response({'enabled':enabled,'duration_hours':hours,'updated':True})


async def admin_demo_grant(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json(); target=int(body.get('telegram_id')); hours=max(1,min(720,int(body.get('duration_hours',48)))); note=str(body.get('note','Admin demo grant'))[:500]
        if target<=0: raise ValueError()
    except Exception: return json_response({'error':'Provide a valid Telegram user ID and duration.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        await conn.execute("""INSERT INTO bpsc_nexus_demo_trials(telegram_id,started_at,expires_at,reset_by,reset_note)
            VALUES(%s,NOW(),NOW()+(%s*INTERVAL '1 hour'),%s,%s)
            ON CONFLICT(telegram_id) DO UPDATE SET started_at=NOW(),expires_at=NOW()+(EXCLUDED.expires_at-EXCLUDED.started_at),reset_by=EXCLUDED.reset_by,reset_note=EXCLUDED.reset_note""",(target,hours,user['id'],note))
        await _write_admin_audit(conn,user['id'],'demo_grant',str(target),{'duration_hours':hours,'note':note})
    return json_response({'telegram_id':target,'duration_hours':hours,'granted':True})


async def _admin_payment_rows(conn, status: str, limit: int = 50):
    await payment_schema(conn)
    if status not in ('pending','approved','rejected','all'):
        status='pending'
    if status == 'all':
        cur=await conn.execute("""SELECT id,telegram_id,product_code,product_name,duration_days,amount_inr,transaction_ref,status,submitted_at,reviewed_at,reviewed_by,review_note,activated_pass_id
            FROM bpsc_payment_requests ORDER BY submitted_at DESC LIMIT %s""",(limit,))
    else:
        cur=await conn.execute("""SELECT id,telegram_id,product_code,product_name,duration_days,amount_inr,transaction_ref,status,submitted_at,reviewed_at,reviewed_by,review_note,activated_pass_id
            FROM bpsc_payment_requests WHERE status=%s ORDER BY submitted_at ASC LIMIT %s""",(status,limit))
    rows=[]
    for r in await cur.fetchall():
        d=dict(r)
        for k in ('submitted_at','reviewed_at'):
            if d.get(k): d[k]=d[k].isoformat()
        rows.append(d)
    return rows


async def admin_payments(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    status=str(request.query.get('status','pending')).lower()
    async with request.app['pool'].connection() as conn:
        rows=await _admin_payment_rows(conn,status,100)
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_payment_requests WHERE status='pending'")
        pending=int((await cur.fetchone())['n'])
    return json_response({'requests':rows,'pending_count':pending,'status':status})


async def admin_payment_detail(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try: rid=int(request.match_info['request_id'])
    except ValueError: return json_response({'error':'Invalid payment request ID.'},400)
    async with request.app['pool'].connection() as conn:
        await payment_schema(conn)
        cur=await conn.execute("SELECT * FROM bpsc_payment_requests WHERE id=%s",(rid,)); r=await cur.fetchone()
    if not r: return json_response({'error':'Payment request not found.'},404)
    d=dict(r)
    for k in ('submitted_at','reviewed_at'):
        if d.get(k): d[k]=d[k].isoformat()
    return json_response({'request':d})


async def _notify_telegram_user(telegram_id: int, message: str):
    token=os.environ.get('BOT_TOKEN','').strip()
    if not token: return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':int(telegram_id),'text':message},timeout=aiohttp.ClientTimeout(total=8)) as resp:
                await resp.read()
    except Exception:
        log.warning('Could not send Mini App admin notification to Telegram user %s',telegram_id)


async def admin_review_payment(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try:
        rid=int(request.match_info['request_id']); body=await request.json(); action=str(body.get('action','')).lower(); note=str(body.get('note','')).strip()[:500]
        if action not in ('approve','reject'): raise ValueError()
    except Exception: return json_response({'error':'Choose approve or reject and provide a valid request ID.'},400)
    result_message=''; pass_id=None
    async with request.app['pool'].connection() as conn:
        await payment_schema(conn)
        async with conn.transaction():
            cur=await conn.execute('SELECT * FROM bpsc_payment_requests WHERE id=%s FOR UPDATE',(rid,)); r=await cur.fetchone()
            if not r: return json_response({'error':'Payment request not found.'},404)
            if r['status']!='pending': return json_response({'error':f"This request was already {r['status']}."},409)
            if action=='reject':
                await conn.execute("UPDATE bpsc_payment_requests SET status='rejected',reviewed_at=NOW(),reviewed_by=%s,review_note=%s WHERE id=%s",(admin['id'],note or 'Rejected',rid))
                await _write_admin_audit(conn,admin['id'],'payment_reject',str(rid),{'telegram_id':r['telegram_id'],'product_code':r['product_code'],'note':note})
                result_message=f"Your BPSC Nexus payment request #{rid} for {r['product_name']} was rejected. Please contact an admin if you believe this is an error."
            else:
                cur=await conn.execute('SELECT id FROM app_users WHERE telegram_id=%s',(r['telegram_id'],)); app_user=await cur.fetchone()
                if not app_user: return json_response({'error':'The student is not registered yet. Ask them to open the Mini App once, then retry.'},409)
                product_code=str(r['product_code'] or 'all_access')
                cur=await conn.execute("SELECT product_code,pass_name,description,duration_days,scope,vault_keys FROM bpsc_pass_offers WHERE product_code=%s",(product_code,)); custom_offer=await cur.fetchone()
                saved_scope=(r.get('pass_scope') if isinstance(r,dict) else None) or (custom_offer['scope'] if custom_offer else None)
                if custom_offer and saved_scope=='group':
                    group_chat_id=int(r.get('group_chat_id') or 0)
                    if not group_chat_id: return json_response({'error':'This Group Pass has no target group ID.'},409)
                    if int(r['duration_days']):
                        await conn.execute("""INSERT INTO group_quiz_memberships(chat_id,chat_title,is_active,granted_by,granted_at,revoked_at,expires_at,plan_code) VALUES(%s,%s,TRUE,%s,NOW(),NULL,NOW()+(%s*INTERVAL '1 day'),%s) ON CONFLICT(chat_id) DO UPDATE SET is_active=TRUE,granted_by=EXCLUDED.granted_by,granted_at=NOW(),revoked_at=NULL,expires_at=EXCLUDED.expires_at,plan_code=EXCLUDED.plan_code""",(group_chat_id,f"Group · {custom_offer['pass_name']}",admin['id'],int(r['duration_days']),product_code))
                    else:
                        await conn.execute("""INSERT INTO group_quiz_memberships(chat_id,chat_title,is_active,granted_by,granted_at,revoked_at,expires_at,plan_code) VALUES(%s,%s,TRUE,%s,NOW(),NULL,NULL,%s) ON CONFLICT(chat_id) DO UPDATE SET is_active=TRUE,granted_by=EXCLUDED.granted_by,granted_at=NOW(),revoked_at=NULL,expires_at=NULL,plan_code=EXCLUDED.plan_code""",(group_chat_id,f"Group · {custom_offer['pass_name']}",admin['id'],product_code))
                    await conn.execute("UPDATE bpsc_payment_requests SET status='approved',reviewed_at=NOW(),reviewed_by=%s,review_note=%s WHERE id=%s",(admin['id'],note or 'Approved Group Pass',rid))
                    await _write_admin_audit(conn,admin['id'],'custom_group_pass_approve',str(rid),{'telegram_id':r['telegram_id'],'group_chat_id':group_chat_id,'product_code':product_code})
                    result_message=f"✅ Group Pass approved. {custom_offer['pass_name']} is active for group {group_chat_id}."
                elif custom_offer:
                    saved_keys=(r.get('pass_vault_keys') if isinstance(r,dict) else None) or list(custom_offer.get('vault_keys') or [])
                    keys=list(saved_keys)
                    if saved_scope in ('all_access','individual'): keys=['*']
                    if not keys: keys=['*'] if saved_scope=='individual' else [product_code]
                    cur=await conn.execute("INSERT INTO pass_catalogue(pass_code,pass_name,description,is_active) VALUES(%s,%s,%s,TRUE) ON CONFLICT(pass_code) DO UPDATE SET pass_name=EXCLUDED.pass_name,description=EXCLUDED.description,is_active=TRUE RETURNING id",(product_code,custom_offer['pass_name'],custom_offer['description'] or 'BPSC Nexus custom pass'))
                    cat=await cur.fetchone()
                    expiry_expr="NOW()+(%s*INTERVAL '1 day')" if int(r['duration_days']) else "NULL"
                    if int(r['duration_days']):
                        cur=await conn.execute(f"INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,granted_at,expires_at,note,pass_code,vault_keys) VALUES(%s,%s,%s,NOW(),{expiry_expr},%s,%s,%s) RETURNING id",(app_user['id'],cat['id'],admin['id'],int(r['duration_days']),f"Payment #{rid}; ₹{r['amount_inr']}; coupon {r.get('coupon_code') or '—'}",product_code,keys))
                    else:
                        cur=await conn.execute("INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,granted_at,expires_at,note,pass_code,vault_keys) VALUES(%s,%s,%s,NOW(),NULL,%s,%s,%s) RETURNING id",(app_user['id'],cat['id'],admin['id'],f"Payment #{rid}; ₹{r['amount_inr']}; coupon {r.get('coupon_code') or '—'}",product_code,keys))
                    pass_id=int((await cur.fetchone())['id'])
                    await conn.execute("UPDATE bpsc_payment_requests SET status='approved',reviewed_at=NOW(),reviewed_by=%s,review_note=%s,activated_pass_id=%s WHERE id=%s",(admin['id'],note or 'Approved',pass_id,rid))
                    await _write_admin_audit(conn,admin['id'],'custom_pass_approve',str(rid),{'telegram_id':r['telegram_id'],'product_code':product_code,'pass_id':pass_id,'coupon_code':r.get('coupon_code')})
                    result_message=f"✅ Your BPSC Nexus payment is approved. {custom_offer['pass_name']} access is now active."
                elif product_code in ('group_starter','group_elite'):
                    group_chat_id = r.get('group_chat_id') if isinstance(r, dict) else None
                    try: group_chat_id = int(group_chat_id or 0)
                    except Exception: group_chat_id = 0
                    if not group_chat_id:
                        return json_response({'error':'This Group Pass has no target group ID. Reject it and ask the buyer to resubmit.'},409)
                    expires_sql = "NOW()+(%s*INTERVAL '1 day')" if int(r['duration_days']) else "NULL"
                    params = (group_chat_id, f"Group · {r['product_name']}", admin['id'], r['duration_days']) if int(r['duration_days']) else (group_chat_id, f"Group · {r['product_name']}", admin['id'])
                    await conn.execute(
                        f"""INSERT INTO group_quiz_memberships(chat_id, chat_title, is_active, granted_by, granted_at, revoked_at, expires_at, plan_code)
                            VALUES(%s, %s, TRUE, %s, NOW(), NULL, {expires_sql}, %s)
                            ON CONFLICT(chat_id) DO UPDATE SET is_active=TRUE, granted_by=EXCLUDED.granted_by,
                              granted_at=NOW(), revoked_at=NULL, expires_at=EXCLUDED.expires_at, plan_code=EXCLUDED.plan_code""",
                        (group_chat_id, f"Group · {r['product_name']}", admin['id'], r['duration_days'], product_code) if int(r['duration_days']) else (group_chat_id, f"Group · {r['product_name']}", admin['id'], product_code),
                    )
                    await conn.execute("UPDATE bpsc_payment_requests SET status='approved',reviewed_at=NOW(),reviewed_by=%s,review_note=%s WHERE id=%s",(admin['id'],note or 'Approved Group Pass',rid))
                    await _write_admin_audit(conn,admin['id'],'group_pass_approve',str(rid),{'telegram_id':r['telegram_id'],'group_chat_id':group_chat_id,'product_code':product_code})
                    result_message=f"✅ Group Pass approved. {r['product_name']} is now active for group {group_chat_id}."
                elif product_code=='all_access':
                    cur=await conn.execute("SELECT id FROM pass_catalogue WHERE pass_code='all_access' AND is_active=TRUE"); cat=await cur.fetchone()
                    if not cat: return json_response({'error':'All-Access pass is missing or inactive in pass_catalogue.'},500)
                    cur=await conn.execute("SELECT MAX(expires_at) AS expiry FROM premium_passes WHERE user_id=%s AND pass_code='all_access' AND revoked_at IS NULL AND expires_at>NOW()",(app_user['id'],)); expiry=(await cur.fetchone())['expiry']
                    cur=await conn.execute("""INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,granted_at,expires_at,note,pass_code,vault_keys)
                        VALUES(%s,%s,%s,COALESCE(%s,NOW()),COALESCE(%s,NOW())+(%s*INTERVAL '1 day'),%s,'all_access',ARRAY['*']::TEXT[]) RETURNING id""",
                        (app_user['id'],cat['id'],admin['id'],expiry,expiry,int(r['duration_days']),f"Mini App UPI payment #{rid}; ₹{r['amount_inr']}"))
                    pass_id=int((await cur.fetchone())['id'])
                    await conn.execute("UPDATE bpsc_payment_requests SET status='approved',reviewed_at=NOW(),reviewed_by=%s,review_note=%s,activated_pass_id=%s WHERE id=%s",(admin['id'],note or 'Approved',pass_id,rid))
                    await _write_admin_audit(conn,admin['id'],'payment_approve',str(rid),{'telegram_id':r['telegram_id'],'product_code':product_code,'pass_id':pass_id,'note':note})
                    result_message=f"✅ Your BPSC Nexus payment is approved. All Access for {r['duration_days']} days is now active."
                else:
                    cur=await conn.execute("SELECT product_code,product_name,entitlement_key,duration_days FROM bpsc_individual_products WHERE product_code=%s",(product_code,)); product=await cur.fetchone()
                    if not product: return json_response({'error':'Purchased product no longer exists in the product catalogue; no access was granted.'},409)
                    await conn.execute("INSERT INTO pass_catalogue(pass_code,pass_name,description,is_active) VALUES(%s,%s,%s,TRUE) ON CONFLICT(pass_code) DO UPDATE SET pass_name=EXCLUDED.pass_name,is_active=TRUE",(product['product_code'],product['product_name'],f"Individual entitlement: {product['entitlement_key']}"))
                    cur=await conn.execute("SELECT id FROM pass_catalogue WHERE pass_code=%s AND is_active=TRUE",(product['product_code'],)); cat=await cur.fetchone()
                    cur=await conn.execute("""INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,expires_at,note,pass_code,vault_keys)
                        VALUES(%s,%s,%s,NULL,%s,%s,ARRAY[%s]::TEXT[]) RETURNING id""",
                        (app_user['id'],cat['id'],admin['id'],f"Mini App UPI payment #{rid}; ₹{r['amount_inr']}",product['product_code'],product['entitlement_key']))
                    pass_id=int((await cur.fetchone())['id'])
                    await conn.execute("UPDATE bpsc_payment_requests SET status='approved',reviewed_at=NOW(),reviewed_by=%s,review_note=%s,activated_pass_id=%s WHERE id=%s",(admin['id'],note or 'Approved',pass_id,rid))
                    await _write_admin_audit(conn,admin['id'],'payment_approve',str(rid),{'telegram_id':r['telegram_id'],'product_code':product_code,'pass_id':pass_id,'note':note})
                    result_message=f"✅ Your BPSC Nexus payment is approved. {product['product_name']} access is now active."
    if action=='reject': result_message=f"Your BPSC Nexus payment request #{rid} for {r['product_name']} was rejected. Please contact an admin if you believe this is an error."
    await _notify_telegram_user(int(r['telegram_id']),result_message)
    return json_response({'request_id':rid,'status':'approved' if action=='approve' else 'rejected','activated_pass_id':pass_id,'updated':True})


async def admin_pass_offers(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    async with request.app['pool'].connection() as conn:
        await payment_schema(conn)
        rows=await _pass_offer_rows(conn,False)
        cur=await conn.execute("SELECT code,description,discount_type,discount_value,max_discount_inr,min_order_inr,starts_at,expires_at,max_redemptions,per_user_limit,applicable_product_codes,is_active,created_at,(SELECT COUNT(*) FROM bpsc_payment_requests p WHERE p.coupon_code=c.code AND p.status IN ('pending','approved')) AS redemptions FROM bpsc_coupon_codes c ORDER BY created_at DESC")
        coupons=[]
        for r in await cur.fetchall():
            d=dict(r)
            for k in ('starts_at','expires_at','created_at'):
                if d.get(k): d[k]=d[k].isoformat()
            d['applicable_product_codes']=list(d.get('applicable_product_codes') or [])
            coupons.append(d)
    return json_response({'passes':rows,'coupons':coupons})

async def admin_pass_offer_save(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json(); code=re.sub(r'[^a-z0-9_-]','',str(body.get('product_code') or '').strip().lower())[:50]
        name=str(body.get('pass_name') or '').strip()[:160]; description=str(body.get('description') or '').strip()[:2000]
        amount=int(body.get('amount_inr')); days=int(body.get('duration_days',0)); scope=str(body.get('scope') or 'individual')
        keys=body.get('vault_keys') or (['*'] if scope=='all_access' else [code]); keys=[str(x).strip()[:120] for x in keys if str(x).strip()][:100]
        category=str(body.get('category') or 'Premium').strip()[:80]; active=bool(body.get('is_active',True)); featured=bool(body.get('is_featured',False)); order=int(body.get('sort_order',0))
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,49}',code): raise ValueError('Pass code must be 3–50 lowercase letters, numbers, underscores or hyphens.')
        if not name: raise ValueError('Pass name is required.')
        if code in {'all_access','group_starter','group_elite','individual_pro','individual_prime','bpsc_pyq','upsc_cse_pyq','ssc_pyq','lucent'} or code in INDIVIDUAL_PRODUCTS: raise ValueError('This pass code is reserved by an existing product. Choose a unique code.')
        if amount<1 or amount>1000000 or days<0 or days>36500: raise ValueError('Pass price must be at least ₹1; check price and duration.')
        if scope not in ('all_access','individual','bank','group'): raise ValueError('Invalid pass scope.')
        if scope in ('all_access','individual'): keys=['*']
        if scope=='bank' and not keys: keys=[code]
    except Exception as e: return json_response({'error':str(e) or 'Invalid pass details.'},400)
    async with request.app['pool'].connection() as conn:
        await payment_schema(conn)
        await conn.execute("""INSERT INTO bpsc_pass_offers(product_code,pass_name,description,amount_inr,duration_days,scope,vault_keys,category,is_active,is_featured,sort_order,created_by,updated_at)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()) ON CONFLICT(product_code) DO UPDATE SET pass_name=EXCLUDED.pass_name,description=EXCLUDED.description,amount_inr=EXCLUDED.amount_inr,duration_days=EXCLUDED.duration_days,scope=EXCLUDED.scope,vault_keys=EXCLUDED.vault_keys,category=EXCLUDED.category,is_active=EXCLUDED.is_active,is_featured=EXCLUDED.is_featured,sort_order=EXCLUDED.sort_order,updated_at=NOW()""",(code,name,description,amount,days,scope,keys,category,active,featured,order,user['id']))
        await _write_admin_audit(conn,user['id'],'pass_offer_save',code,{'amount_inr':amount,'duration_days':days,'scope':scope,'is_active':active})
    return json_response({'saved':True,'product_code':code,'pass_name':name})

async def admin_pass_offer_toggle(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    code=str(request.match_info.get('product_code') or '')
    try: body=await request.json(); active=bool(body.get('is_active'))
    except Exception:return json_response({'error':'Invalid status.'},400)
    async with request.app['pool'].connection() as conn:
        await payment_schema(conn)
        cur=await conn.execute("UPDATE bpsc_pass_offers SET is_active=%s,updated_at=NOW() WHERE product_code=%s RETURNING product_code",(active,code)); row=await cur.fetchone()
        if not row:return json_response({'error':'Pass not found.'},404)
        await _write_admin_audit(conn,user['id'],'pass_offer_toggle',code,{'is_active':active})
    return json_response({'updated':True,'product_code':code,'is_active':active})

async def admin_coupon_save(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json(); code=re.sub(r'[^A-Z0-9_-]','',str(body.get('code') or '').strip().upper())[:40]
        desc=str(body.get('description') or '').strip()[:300]; dtype=str(body.get('discount_type') or 'percent'); value=int(body.get('discount_value')); maxdisc=body.get('max_discount_inr'); maxdisc=int(maxdisc) if maxdisc not in (None,'') else None
        minimum=max(0,int(body.get('min_order_inr',0))); maxuses=body.get('max_redemptions'); maxuses=int(maxuses) if maxuses not in (None,'') else None
        peruser=max(1,min(100,int(body.get('per_user_limit',1)))); active=bool(body.get('is_active',True)); applies=body.get('applicable_product_codes') or None
        starts_raw=str(body.get('starts_at') or '').strip(); expires_raw=str(body.get('expires_at') or '').strip()
        starts_at=datetime.fromisoformat(starts_raw).replace(tzinfo=timezone.utc) if starts_raw and datetime.fromisoformat(starts_raw).tzinfo is None else (datetime.fromisoformat(starts_raw) if starts_raw else None)
        expires_at=datetime.fromisoformat(expires_raw).replace(tzinfo=timezone.utc) if expires_raw and datetime.fromisoformat(expires_raw).tzinfo is None else (datetime.fromisoformat(expires_raw) if expires_raw else None)
        if starts_at and expires_at and expires_at<=starts_at: raise ValueError('Coupon expiry must be after its start time.')
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9_-]{2,39}',code): raise ValueError('Coupon code must be 3–40 characters (letters, numbers, _ or -).')
        if dtype not in ('percent','fixed') or value<1 or (dtype=='percent' and value>100): raise ValueError('Use a valid fixed discount or percentage from 1–100.')
        if maxdisc is not None and maxdisc<1: raise ValueError('Maximum discount must be positive.')
        if maxuses is not None and maxuses<1: raise ValueError('Maximum redemptions must be positive.')
        if applies: applies=[str(x)[:50] for x in applies][:100]
    except Exception as e:return json_response({'error':str(e) or 'Invalid coupon details.'},400)
    async with request.app['pool'].connection() as conn:
        await payment_schema(conn)
        await conn.execute("""INSERT INTO bpsc_coupon_codes(code,description,discount_type,discount_value,max_discount_inr,min_order_inr,starts_at,expires_at,max_redemptions,per_user_limit,applicable_product_codes,is_active,created_by,updated_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()) ON CONFLICT(code) DO UPDATE SET description=EXCLUDED.description,discount_type=EXCLUDED.discount_type,discount_value=EXCLUDED.discount_value,max_discount_inr=EXCLUDED.max_discount_inr,min_order_inr=EXCLUDED.min_order_inr,starts_at=EXCLUDED.starts_at,expires_at=EXCLUDED.expires_at,max_redemptions=EXCLUDED.max_redemptions,per_user_limit=EXCLUDED.per_user_limit,applicable_product_codes=EXCLUDED.applicable_product_codes,is_active=EXCLUDED.is_active,updated_at=NOW()""",(code,desc,dtype,value,maxdisc,minimum,starts_at,expires_at,maxuses,peruser,applies,active,user['id']))
        await _write_admin_audit(conn,user['id'],'coupon_save',code,{'discount_type':dtype,'discount_value':value,'is_active':active})
    return json_response({'saved':True,'code':code})

async def admin_coupon_toggle(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    code=str(request.match_info.get('code') or '').upper()
    try: body=await request.json(); active=bool(body.get('is_active'))
    except Exception:return json_response({'error':'Invalid status.'},400)
    async with request.app['pool'].connection() as conn:
        await payment_schema(conn)
        cur=await conn.execute("UPDATE bpsc_coupon_codes SET is_active=%s,updated_at=NOW() WHERE code=%s RETURNING code",(active,code)); row=await cur.fetchone()
        if not row:return json_response({'error':'Coupon not found.'},404)
        await _write_admin_audit(conn,user['id'],'coupon_toggle',code,{'is_active':active})
    return json_response({'updated':True,'code':code,'is_active':active})

async def admin_members_search(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    query=str(request.query.get('q','')).strip()[:100]
    if not query: return json_response({'members':[]})
    async with request.app['pool'].connection() as conn:
        if query.isdigit():
            cur=await conn.execute("""SELECT u.id,u.telegram_id,u.first_name,u.username,
                COUNT(pp.id) FILTER(WHERE pp.revoked_at IS NULL AND (pp.expires_at IS NULL OR pp.expires_at>NOW())) AS active_passes
                FROM app_users u LEFT JOIN premium_passes pp ON pp.user_id=u.id WHERE u.telegram_id=%s GROUP BY u.id LIMIT 20""",(int(query),))
        else:
            pattern='%'+query.replace('%','\\%').replace('_','\\_')+'%'
            cur=await conn.execute("""SELECT u.id,u.telegram_id,u.first_name,u.username,
                COUNT(pp.id) FILTER(WHERE pp.revoked_at IS NULL AND (pp.expires_at IS NULL OR pp.expires_at>NOW())) AS active_passes
                FROM app_users u LEFT JOIN premium_passes pp ON pp.user_id=u.id
                WHERE COALESCE(u.first_name,'') ILIKE %s OR COALESCE(u.username,'') ILIKE %s OR CAST(u.telegram_id AS TEXT) ILIKE %s
                GROUP BY u.id ORDER BY u.telegram_id DESC LIMIT 50""",(pattern,pattern,pattern))
        rows=[dict(r) for r in await cur.fetchall()]
    return json_response({'members':rows})


async def admin_member_detail(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try: target=int(request.match_info['telegram_id'])
    except ValueError: return json_response({'error':'Invalid Telegram ID.'},400)
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("SELECT id,telegram_id,first_name,username FROM app_users WHERE telegram_id=%s",(target,)); member=await cur.fetchone()
        if not member: return json_response({'error':'Student not found. Ask them to open the Mini App once.'},404)
        cur=await conn.execute("""SELECT pp.id,pp.pass_code,pp.vault_keys,pp.granted_at,pp.expires_at,pp.revoked_at,pp.note,pp.granted_by_telegram_id
            FROM premium_passes pp WHERE pp.user_id=%s ORDER BY pp.granted_at DESC LIMIT 100""",(member['id'],)); passes=[]
        for r in await cur.fetchall():
            d=dict(r)
            for k in ('granted_at','expires_at','revoked_at'):
                if d.get(k): d[k]=d[k].isoformat()
            passes.append(d)
        cur=await conn.execute("SELECT started_at,expires_at FROM bpsc_nexus_demo_trials WHERE telegram_id=%s",(target,)); trial=await cur.fetchone()
    demo=dict(trial) if trial else None
    if demo:
        for k in ('started_at','expires_at'):
            if demo.get(k): demo[k]=demo[k].isoformat()
    return json_response({'member':dict(member),'passes':passes,'demo':demo})


async def admin_member_grant(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json(); target=int(body.get('telegram_id')); code=str(body.get('pass_code','')).strip().lower(); days=int(body.get('duration_days',0)); note=str(body.get('note','Admin grant from Mini App'))[:500]; selected_keys=body.get('vault_keys') or []
        if target<=0 or not re.fullmatch(r'[a-z0-9_]{2,80}',code) or days not in (0,7,30,60,90,120,180,249,365): raise ValueError()
        if code=='dual_vault':
            allowed_keys={'bpsc_pyq','ghatnachakra_pyq','upsc_pyq'}; selected_keys=list(dict.fromkeys(str(k) for k in selected_keys if str(k) in allowed_keys))
            if len(selected_keys)!=2: raise ValueError('Dual Vault requires exactly two different vaults.')
    except Exception: return json_response({'error':'Choose a valid member, pass and duration.'},400)
    async with request.app['pool'].connection() as conn:
        async with conn.transaction():
            cur=await conn.execute('SELECT id FROM app_users WHERE telegram_id=%s',(target,)); member=await cur.fetchone()
            if not member: return json_response({'error':'Student not found. Ask them to open the Mini App once.'},404)
            if code=='all_access':
                cur=await conn.execute("SELECT id FROM pass_catalogue WHERE pass_code='all_access' AND is_active=TRUE"); cat=await cur.fetchone()
                if not cat: return json_response({'error':'All-Access pass missing from catalogue.'},500)
                cur=await conn.execute("SELECT MAX(expires_at) AS expiry FROM premium_passes WHERE user_id=%s AND pass_code='all_access' AND revoked_at IS NULL AND expires_at>NOW()",(member['id'],)); expiry=(await cur.fetchone())['expiry']
                if days:
                    cur=await conn.execute("INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,expires_at,note,pass_code,vault_keys) VALUES(%s,%s,%s,COALESCE(%s,NOW())+(%s*INTERVAL '1 day'),%s,'all_access',ARRAY['*']::TEXT[]) RETURNING id",(member['id'],cat['id'],admin['id'],expiry,days,note))
                else:
                    cur=await conn.execute("INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,expires_at,note,pass_code,vault_keys) VALUES(%s,%s,%s,NULL,%s,'all_access',ARRAY['*']::TEXT[]) RETURNING id",(member['id'],cat['id'],admin['id'],note))
            else:
                cur=await conn.execute("SELECT product_code,product_name,entitlement_key FROM bpsc_individual_products WHERE product_code=%s AND is_active=TRUE",(code,)); product=await cur.fetchone()
                if product:
                    await conn.execute("INSERT INTO pass_catalogue(pass_code,pass_name,description,is_active) VALUES(%s,%s,%s,TRUE) ON CONFLICT(pass_code) DO UPDATE SET pass_name=EXCLUDED.pass_name,is_active=TRUE",(code,product['product_name'],f"Individual entitlement: {product['entitlement_key']}"))
                    cur=await conn.execute("SELECT id,pass_name FROM pass_catalogue WHERE pass_code=%s",(code,)); cat=await cur.fetchone(); vault_keys=[str(product['entitlement_key'])]
                else:
                    cur=await conn.execute("SELECT id,pass_name FROM pass_catalogue WHERE pass_code=%s AND is_active=TRUE",(code,)); cat=await cur.fetchone()
                    vault_keys=selected_keys if code=='dual_vault' else [code]
                if not cat: return json_response({'error':'Unknown or inactive pass code.'},404)
                if code=='dual_vault' and len(vault_keys)!=2: return json_response({'error':'Dual Vault requires exactly two different vaults.'},400)
                if days:
                    cur=await conn.execute("INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,expires_at,note,pass_code,vault_keys) VALUES(%s,%s,%s,NOW()+(%s*INTERVAL '1 day'),%s,%s,%s) RETURNING id",(member['id'],cat['id'],admin['id'],days,note,code,vault_keys))
                else:
                    cur=await conn.execute("INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,expires_at,note,pass_code,vault_keys) VALUES(%s,%s,%s,NULL,%s,%s,%s) RETURNING id",(member['id'],cat['id'],admin['id'],note,code,vault_keys))
            pass_id=int((await cur.fetchone())['id'])
            await _write_admin_audit(conn,admin['id'],'member_pass_grant',str(target),{'pass_id':pass_id,'pass_code':code,'duration_days':days,'note':note})
    return json_response({'granted':True,'pass_id':pass_id,'telegram_id':target})


async def admin_pass_revoke(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try: pid=int(request.match_info['pass_id'])
    except ValueError: return json_response({'error':'Invalid pass ID.'},400)
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("UPDATE premium_passes SET revoked_at=NOW(),revoked_by_telegram_id=%s WHERE id=%s AND revoked_at IS NULL RETURNING user_id,pass_code",(admin['id'],pid)); row=await cur.fetchone()
        if not row: return json_response({'error':'Pass not found or already revoked.'},404)
        cur=await conn.execute("SELECT telegram_id FROM app_users WHERE id=%s",(row['user_id'],)); member=await cur.fetchone()
        await _write_admin_audit(conn,admin['id'],'pass_revoke',str(pid),{'telegram_id':member['telegram_id'] if member else None,'pass_code':row['pass_code']})
    return json_response({'revoked':True,'pass_id':pid,'telegram_id':member['telegram_id'] if member else None})


async def admin_pass_extend(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try: pid=int(request.match_info['pass_id']); body=await request.json(); days=int(body.get('days'))
    except Exception: return json_response({'error':'Provide a valid pass ID and number of days.'},400)
    if days<1 or days>3650: return json_response({'error':'Extension must be between 1 and 3650 days.'},400)
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("UPDATE premium_passes SET expires_at=GREATEST(COALESCE(expires_at,NOW()),NOW())+(%s*INTERVAL '1 day') WHERE id=%s AND revoked_at IS NULL AND expires_at IS NOT NULL RETURNING user_id,pass_code",(days,pid)); row=await cur.fetchone()
        if not row: return json_response({'error':'Pass is revoked, permanent, or not found.'},404)
        cur=await conn.execute("SELECT telegram_id FROM app_users WHERE id=%s",(row['user_id'],)); member=await cur.fetchone()
        await _write_admin_audit(conn,admin['id'],'pass_extend',str(pid),{'days':days,'telegram_id':member['telegram_id'] if member else None})
    return json_response({'extended':True,'pass_id':pid,'days':days})


async def admin_import_subjects(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    async with request.app['pool'].connection() as conn:
        await qb.ensure_schema(request.app['pool'])
        cur=await conn.execute("SELECT name,subject_type FROM question_bank_subjects WHERE archived_at IS NULL ORDER BY LOWER(name),name")
        rows=[dict(r) for r in await cur.fetchall()]
    return json_response({'subjects':rows})


async def admin_create_import_subject(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json(); name=str(body.get('name','')).strip(); kind=str(body.get('subject_type','subject')).lower()
        if not name or len(name)>100 or kind not in ('subject','collection'): raise ValueError()
    except Exception: return json_response({'error':'Enter a name up to 100 characters and choose subject or collection.'},400)
    await qb.ensure_schema(request.app['pool'])
    async with request.app['pool'].connection() as conn:
        try:
            await conn.execute("INSERT INTO question_bank_subjects(name,created_by_telegram_id,subject_type,access_type) VALUES(%s,%s,%s,'free')",(name,admin['id'],kind))
        except Exception as e:
            if 'unique' in str(e).lower(): return json_response({'error':'A subject or collection with this name already exists.'},409)
            raise
        await _write_admin_audit(conn,admin['id'],'import_subject_create',name,{'subject_type':kind})
    return json_response({'name':name,'subject_type':kind,'created':True},201)


async def admin_bulk_import(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try:
        form=await request.multipart(); fields={}; upload=None
        async for part in form:
            if part.name=='file': upload=part
            else: fields[part.name]=(await part.text())
        if upload is None: return json_response({'error':'Choose an HTML, JSON or CSV question file.'},400)
        filename=str(upload.filename or 'questions.csv').replace('\\','/').split('/')[-1][:180]
        raw=await upload.read(decode=False)
        if not raw: return json_response({'error':'The selected file is empty.'},400)
        if len(raw)>20*1024*1024: return json_response({'error':'Import files are limited to 20 MB.'},413)
        subject=str(fields.get('subject','')).strip()
        if not subject: return json_response({'error':'Choose the destination subject/collection.'},400)
        records,diag=qb.parse_file(filename,raw,subject,int(admin['id']))
        match=re.search(r'invalid=(\d+)',diag); invalid=int(match.group(1)) if match else 0
        file_type=filename.rsplit('.',1)[-1].lower() if '.' in filename else 'unknown'
        result=await qb.import_records(request.app['pool'],records,int(admin['id']),filename,file_type,subject,invalid)
        async with request.app['pool'].connection() as conn:
            await _write_admin_audit(conn,admin['id'],'bulk_question_import',filename,result)
        return json_response({'result':result,'filename':filename},201)
    except ValueError as e: return json_response({'error':str(e)},400)
    except Exception as e:
        log.exception('Mini App bulk question import failed')
        return json_response({'error':'Import failed safely. Check that the file uses the supported template and the destination subject still exists.'},500)


async def admin_questions_search(request):
    if not _is_series_admin(request['telegram_user']): return json_response({'error':'Admin access required.'},403)
    query=str(request.query.get('q','')).strip()[:250]
    subject=str(request.query.get('subject','')).strip()[:150]
    status=str(request.query.get('status','all')).lower()
    if status not in {'all','ready','pending','archived'}: status='all'
    try: limit=max(1,min(50,int(request.query.get('limit','25'))))
    except ValueError: limit=25
    conditions=[]; params=[]
    if query:
        if query.isdigit(): conditions.append('q.id=%s'); params.append(int(query))
        else:
            conditions.append("(q.question_en ILIKE %s OR COALESCE(q.question_hi,'') ILIKE %s OR COALESCE(q.topic,'') ILIKE %s OR q.subject ILIKE %s)")
            like='%'+query.replace('%','\\%').replace('_','\\_')+'%'; params.extend([like,like,like,like])
    if subject: conditions.append('q.subject=%s'); params.append(subject)
    if status!='all': conditions.append('q.status=%s'); params.append(status)
    where=(' WHERE '+' AND '.join(conditions)) if conditions else ''
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute(f"SELECT q.id,q.question_en,q.question_hi,q.subject,q.topic,q.status,q.correct_option,q.options FROM question_bank q{where} ORDER BY q.id DESC LIMIT %s",tuple(params+[limit]))
        rows=[]
        for r in await cur.fetchall():
            d=dict(r); opts=d.get('options') or {}; d['options']=opts if isinstance(opts,dict) else json.loads(opts); d['id']=int(d['id']); rows.append(d)
    return json_response({'questions':rows,'count':len(rows)})


async def admin_question_detail(request):
    if not _is_series_admin(request['telegram_user']): return json_response({'error':'Admin access required.'},403)
    try: qid=int(request.match_info['question_id'])
    except ValueError: return json_response({'error':'Invalid question ID.'},400)
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("SELECT id,question_en,question_hi,question_type,question_structure,question_flags,options,correct_option,explanation_en,explanation_hi,subject,topic,source,source_url,attribution,status FROM question_bank WHERE id=%s",(qid,)); r=await cur.fetchone()
    if not r: return json_response({'error':'Question not found.'},404)
    d=dict(r)
    for key in ('question_structure','question_flags','options'):
        if not isinstance(d.get(key),dict): d[key]=json.loads(d.get(key) or '{}')
    return json_response({'question':d})


async def admin_question_update(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try:
        qid=int(request.match_info['question_id']); body=await request.json()
        q_en=str(body.get('question_en','')).strip(); q_hi=str(body.get('question_hi','')).strip(); options=body.get('options')
        if not isinstance(options,dict): raise ValueError('Options must be an object with A, B, C and D keys.')
        normalized={str(k).upper():str(v).strip() for k,v in options.items() if str(k).upper() in {'A','B','C','D','E'} and str(v).strip()}
        if not q_en: raise ValueError('English question text is required.')
        if len(normalized)<2: raise ValueError('At least two non-empty options are required.')
        correct=str(body.get('correct_option') or '').upper().strip() or None
        if correct and correct not in normalized: raise ValueError('Correct answer must match one of the supplied option keys.')
        status=str(body.get('status','pending')).lower()
        if status not in {'ready','pending','archived'}: raise ValueError('Choose ready, pending or archived status.')
        if status=='ready' and not correct: raise ValueError('A ready question must have a correct answer.')
        subject=str(body.get('subject','')).strip(); topic=str(body.get('topic','')).strip()[:250]
        if not subject: raise ValueError('Choose a subject/collection.')
        explanation_en=str(body.get('explanation_en','')).strip(); explanation_hi=str(body.get('explanation_hi','')).strip(); source=str(body.get('source','')).strip()[:500]; source_url=str(body.get('source_url','')).strip()[:1000]; attribution=str(body.get('attribution','')).strip()[:500]
        digest=hashlib.sha256((q_en.casefold()+'|'+json.dumps(normalized,ensure_ascii=False,sort_keys=True)+'|'+str(correct or '')).encode('utf-8')).hexdigest()
    except ValueError as e: return json_response({'error':str(e)},400)
    except Exception: return json_response({'error':'Invalid question payload.'},400)
    async with request.app['pool'].connection() as conn:
        async with conn.transaction():
            cur=await conn.execute("SELECT id FROM question_bank_subjects WHERE name=%s AND archived_at IS NULL",(subject,))
            if not await cur.fetchone(): return json_response({'error':'The selected subject/collection is not active.'},400)
            cur=await conn.execute("SELECT id FROM question_bank WHERE question_hash=%s AND id<>%s LIMIT 1",(digest,qid))
            dup=await cur.fetchone()
            if dup: return json_response({'error':f'This question already exists as #{int(dup["id"])}.','duplicate_id':int(dup['id'])},409)
            cur=await conn.execute("""UPDATE question_bank SET question_en=%s,question_hi=%s,options=%s::jsonb,correct_option=%s,
                explanation_en=%s,explanation_hi=%s,subject=%s,topic=%s,source=%s,source_url=%s,attribution=%s,
                status=%s,question_hash=%s,updated_at=NOW()
                WHERE id=%s RETURNING id,status""",
                (q_en,q_hi,json.dumps(normalized,ensure_ascii=False),correct,explanation_en or None,explanation_hi or None,subject,topic or None,
                 source or None,source_url or None,attribution or None,status,digest,qid))
            row=await cur.fetchone()
            if not row: return json_response({'error':'Question not found.'},404)
            await _write_admin_audit(conn,admin['id'],'question_update',str(qid),{'status':status,'subject':subject})
    return json_response({'question':{'id':int(row['id']),'status':row['status']},'updated':True})


async def admin_question_delete(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try: qid=int(request.match_info['question_id'])
    except ValueError: return json_response({'error':'Invalid question ID.'},400)
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bank_quiz_questions WHERE question_id=%s",(qid,)); linked=int((await cur.fetchone())['n'])
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bank_quiz_attempt_question_snapshots WHERE question_id=%s",(qid,)); snaps=int((await cur.fetchone())['n'])
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bpsc_nexus_revision_items WHERE question_id=%s",(qid,)); revs=int((await cur.fetchone())['n'])
        if linked or snaps or revs:
            await conn.execute("UPDATE question_bank SET status='archived',updated_at=NOW() WHERE id=%s",(qid,))
            await _write_admin_audit(conn,admin['id'],'question_archive',str(qid),{'linked_quizzes':linked,'snapshots':snaps,'revision_items':revs})
            return json_response({'id':qid,'action':'archived','success':True,'reason':'Question has usage history; archived instead of deleted.','linked_quizzes':linked,'snapshots':snaps,'revision_items':revs})
        cur=await conn.execute("DELETE FROM question_bank WHERE id=%s RETURNING id",(qid,))
        if not await cur.fetchone(): return json_response({'error':'Question not found.'},404)
        await _write_admin_audit(conn,admin['id'],'question_delete',str(qid),{})
    return json_response({'id':qid,'action':'deleted','success':True})


async def admin_subjects_list(request):
    if not _is_series_admin(request['telegram_user']): return json_response({'error':'Admin access required.'},403)
    async with request.app['pool'].connection() as conn:
        await qb.ensure_schema(request.app['pool'])
        cur=await conn.execute("""SELECT s.id,s.name,s.subject_type,s.access_type,s.archived_at,
            (SELECT COUNT(*) FROM question_bank q WHERE q.subject=s.name) AS question_count,
            (SELECT COUNT(*) FROM question_bank q WHERE q.subject=s.name AND q.status='ready') AS ready_count
            FROM question_bank_subjects s WHERE s.archived_at IS NULL ORDER BY LOWER(s.name)""")
        rows=[dict(r) for r in await cur.fetchall()]
    return json_response({'subjects':rows})


async def admin_notifications_list(request):
    if not _is_series_admin(request['telegram_user']): return json_response({'error':'Admin access required.'},403)
    async with request.app['pool'].connection() as conn:
        await ensure_notifications_schema(conn)
        cur=await conn.execute("SELECT id,title,message,link,link_label,segment,is_pinned,silent,recipients,created_by,created_at FROM bpsc_nexus_notifications ORDER BY created_at DESC LIMIT 100")
        rows=[dict(r) for r in await cur.fetchall()]
    for r in rows:
        if r.get('created_at'): r['created_at']=r['created_at'].isoformat()
    return json_response({'notifications':rows})


async def admin_notification_create(request):
    admin = request['telegram_user']
    if not _is_series_admin(admin):
        return json_response({'error': 'Admin access required.'}, 403)
    try:
        body = await request.json()
        title = str(body.get('title', '')).strip()[:180]
        message = str(body.get('message', '')).strip()[:2000]
        link = str(body.get('link', '')).strip()[:500] or None
        link_label = str(body.get('link_label', '')).strip()[:80] or None
        segment = str(body.get('segment', 'all')).lower()
        is_pinned = bool(body.get('is_pinned'))
        silent = bool(body.get('silent'))
        if not title or not message:
            raise ValueError('Title and message are required.')
        if segment not in ('all', 'demo', 'paid', 'admin'):
            segment = 'all'
    except (ValueError, TypeError):
        return json_response({'error': 'Provide a title and message.'}, 400)
    async with request.app['pool'].connection() as conn:
        await ensure_notifications_schema(conn)
        await ensure_access_schema(conn)
        cur = await conn.execute("""INSERT INTO bpsc_nexus_notifications
            (title,message,link,link_label,segment,is_pinned,silent,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,created_at""",
            (title, message, link, link_label, segment, is_pinned, silent, admin['id']))
        row = await cur.fetchone()
        recipients = 0
        try:
            if segment == 'demo':
                cur = await conn.execute("SELECT COUNT(*) AS n FROM bpsc_nexus_demo_registrations")
            elif segment == 'paid':
                cur = await conn.execute("""SELECT COUNT(DISTINCT u.telegram_id) AS n
                    FROM app_users u JOIN premium_passes pp ON pp.user_id=u.id
                    WHERE pp.revoked_at IS NULL AND (pp.expires_at IS NULL OR pp.expires_at>NOW())""")
            elif segment == 'admin':
                recipients = len(ADMIN_TELEGRAM_IDS)
                cur = None
            else:
                cur = await conn.execute("SELECT COUNT(*) AS n FROM app_users")
            if cur is not None:
                recipients = int((await cur.fetchone())['n'] or 0)
            await conn.execute("UPDATE bpsc_nexus_notifications SET recipients=%s WHERE id=%s", (recipients, row['id']))
        except Exception:
            log.exception('Could not compute notification recipients for %s', row['id'])
        await _write_admin_audit(conn, admin['id'], 'notification_create', str(row['id']),
                                 {'segment': segment, 'title': title, 'recipients': recipients})
    return json_response({'id': int(row['id']), 'created_at': row['created_at'].isoformat(),
                          'created': True, 'recipients': recipients}, 201)

async def admin_notification_delete(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try: nid=int(request.match_info['notification_id'])
    except ValueError: return json_response({'error':'Invalid notification ID.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_notifications_schema(conn)
        cur=await conn.execute("DELETE FROM bpsc_nexus_notifications WHERE id=%s RETURNING id",(nid,))
        if not await cur.fetchone(): return json_response({'error':'Notification not found.'},404)
        await _write_admin_audit(conn,admin['id'],'notification_delete',str(nid),{})
    return json_response({'deleted':True,'id':nid})


async def notifications_list(request):
    uid = int(request['telegram_user']['id'])
    async with request.app['pool'].connection() as conn:
        await ensure_notifications_schema(conn)
        cur = await conn.execute("""SELECT n.id,n.title,n.message,n.link,n.link_label,n.segment,
                n.is_pinned,n.silent,n.created_at,
                (r.telegram_id IS NOT NULL) AS is_read
            FROM bpsc_nexus_notifications n
            LEFT JOIN bpsc_nexus_notification_reads r
              ON r.notification_id=n.id AND r.telegram_id=%s
            WHERE n.segment='all'
              OR (n.segment='admin' AND %s=ANY(%s))
              OR (n.segment='demo' AND EXISTS (SELECT 1 FROM bpsc_nexus_demo_registrations dr WHERE dr.telegram_id=%s))
              OR (n.segment='paid' AND EXISTS (SELECT 1 FROM app_users u JOIN premium_passes pp ON pp.user_id=u.id
                    WHERE u.telegram_id=%s AND pp.revoked_at IS NULL AND (pp.expires_at IS NULL OR pp.expires_at>NOW())))
            ORDER BY n.is_pinned DESC,n.created_at DESC LIMIT 60""",
            (uid, uid, list(ADMIN_TELEGRAM_IDS), uid, uid))
        rows = [dict(r) for r in await cur.fetchall()]
    for row in rows:
        if row.get('created_at'):
            row['created_at'] = row['created_at'].isoformat()
    unread = sum(1 for row in rows if not row.get('is_read'))
    return json_response({'notifications': rows, 'unread_count': unread})


async def notifications_mark_all_read(request):
    uid = int(request['telegram_user']['id'])
    async with request.app['pool'].connection() as conn:
        await ensure_notifications_schema(conn)
        cur = await conn.execute("""SELECT n.id FROM bpsc_nexus_notifications n
            LEFT JOIN bpsc_nexus_notification_reads r
              ON r.notification_id=n.id AND r.telegram_id=%s
            WHERE r.telegram_id IS NULL AND (
                n.segment='all'
                OR (n.segment='admin' AND %s=ANY(%s))
                OR (n.segment='demo' AND EXISTS (SELECT 1 FROM bpsc_nexus_demo_registrations dr WHERE dr.telegram_id=%s))
                OR (n.segment='paid' AND EXISTS (SELECT 1 FROM app_users u JOIN premium_passes pp ON pp.user_id=u.id
                      WHERE u.telegram_id=%s AND pp.revoked_at IS NULL AND (pp.expires_at IS NULL OR pp.expires_at>NOW())))
              )""",
            (uid, uid, list(ADMIN_TELEGRAM_IDS), uid, uid))
        ids = [int(row['id']) for row in await cur.fetchall()]
        for nid in ids:
            await conn.execute("""INSERT INTO bpsc_nexus_notification_reads(notification_id,telegram_id)
                VALUES(%s,%s) ON CONFLICT DO NOTHING""", (nid, uid))
    return json_response({'marked': len(ids)})

async def notification_mark_read(request):
    uid=request['telegram_user']['id']
    try: nid=int(request.match_info['notification_id'])
    except ValueError: return json_response({'error':'Invalid notification ID.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_notifications_schema(conn)
        await conn.execute("INSERT INTO bpsc_nexus_notification_reads(notification_id,telegram_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(nid,uid))
    return json_response({'read':True,'id':nid})


async def admin_get_promo(request):
    if not _is_series_admin(request['telegram_user']): return json_response({'error':'Admin access required.'},403)
    async with request.app['pool'].connection() as conn:
        banner=await _get_setting(conn,'promo_banner',{}) or {}
        maint=await _get_setting(conn,'maintenance',{'enabled':False}) or {}
    return json_response({'promo_banner':banner,'maintenance':maint})


async def admin_set_promo(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json(); banner={
            'enabled':bool(body.get('enabled')),
            'tag':str(body.get('tag',''))[:60],
            'title':str(body.get('title',''))[:180],
            'message':str(body.get('message',''))[:500],
            'link':str(body.get('link',''))[:500],
            'link_label':str(body.get('link_label',''))[:80],
        }
    except Exception: return json_response({'error':'Invalid banner payload.'},400)
    async with request.app['pool'].connection() as conn:
        await _set_setting(conn,'promo_banner',banner)
        await _write_admin_audit(conn,admin['id'],'promo_banner_update','global',banner)
    return json_response({'promo_banner':banner,'updated':True})


async def admin_set_maintenance(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json(); enabled=bool(body.get('enabled')); message=str(body.get('message',''))[:500]
    except Exception: return json_response({'error':'Invalid maintenance payload.'},400)
    async with request.app['pool'].connection() as conn:
        await _set_setting(conn,'maintenance',{'enabled':enabled,'message':message,'updated_at':datetime.now(timezone.utc).isoformat()})
        await _write_admin_audit(conn,admin['id'],'maintenance_toggle','global',{'enabled':enabled})
    return json_response({'maintenance':{'enabled':enabled,'message':message},'updated':True})


async def list_series(request):
    user=request['telegram_user']
    async with request.app['pool'].connection() as conn:
        await ensure_test_series_schema(conn)
        cur=await conn.execute("""SELECT s.id,s.title,s.description,s.logo_url,s.exam_type,s.series_type,s.subject,s.difficulty,s.access_type,s.sort_order,
            COUNT(DISTINCT st.quiz_id) AS test_count
            FROM bpsc_nexus_test_series s LEFT JOIN bpsc_nexus_test_series_tests st ON st.series_id=s.id
            WHERE s.is_published=TRUE GROUP BY s.id ORDER BY s.sort_order ASC,s.id DESC LIMIT 100""")
        rows=[dict(r) for r in await cur.fetchall()]
    return json_response({'series':rows})


async def series_detail(request):
    user=request['telegram_user']
    try: sid=int(request.match_info['series_id'])
    except ValueError: return json_response({'error':'Invalid series ID.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_test_series_schema(conn)
        cur=await conn.execute("SELECT id,title,description,logo_url,exam_type,series_type,subject,difficulty,access_type FROM bpsc_nexus_test_series WHERE id=%s AND is_published=TRUE",(sid,))
        series=await cur.fetchone()
        if not series: return json_response({'error':'Test series not found.'},404)
        cur=await conn.execute("""SELECT q.id,q.title,q.subject,q.duration_minutes,
                (SELECT COUNT(*) FROM bank_quiz_questions qq WHERE qq.quiz_id=q.id) AS total_questions,
                q.is_published,st.position
            FROM bpsc_nexus_test_series_tests st JOIN bank_quizzes q ON q.id=st.quiz_id
            WHERE st.series_id=%s ORDER BY st.position ASC,q.id ASC""",(sid,))
        tests=[dict(r) for r in await cur.fetchall()]
    return json_response({'series':dict(series),'tests':tests})


async def browse_banks(request):
    user=request['telegram_user']
    is_admin=_is_series_admin(user)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        state=await _access_state(conn,user['id'],is_admin,True)
        cur=await conn.execute("""SELECT qb.subject AS name,COUNT(*) AS total,
            COUNT(*) FILTER (WHERE qb.status='ready') AS ready,
            COALESCE(MAX(s.access_type),'free') AS access_type
            FROM question_bank qb LEFT JOIN question_bank_subjects s ON LOWER(BTRIM(s.name))=LOWER(BTRIM(qb.subject)) AND s.archived_at IS NULL
            WHERE qb.status='ready' GROUP BY qb.subject HAVING COUNT(*) FILTER (WHERE qb.status='ready')>0
            ORDER BY LOWER(qb.subject)""")
        banks=[]
        for r in await cur.fetchall():
            d=dict(r); d['locked']= (d['access_type']=='paid' and not state['has_paid_access'])
            banks.append(d)
    return json_response({'banks':banks,'access':state})


async def browse_quizzes(request):
    user=request['telegram_user']
    is_admin=_is_series_admin(user)
    subject=str(request.query.get('subject','')).strip()[:180]
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        if not subject:
            cur=await conn.execute("SELECT DISTINCT subject FROM bank_quizzes WHERE is_published=TRUE AND is_custom=FALSE AND COALESCE(admin_hidden,FALSE)=FALSE ORDER BY subject")
            subjects=[r['subject'] for r in await cur.fetchall() if r['subject']]
            return json_response({'subjects':subjects,'quizzes':[]})
        cur=await conn.execute("""SELECT q.id,q.title,q.subject,q.duration_minutes,
            COALESCE((SELECT COUNT(*) FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id WHERE qq.quiz_id=q.id AND qb.status='ready'),0) AS total_questions,
            q.access_type,q.is_custom,
            COALESCE((SELECT COUNT(*) FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id WHERE qq.quiz_id=q.id AND qb.status='ready'),0) AS ready_count
            FROM bank_quizzes q WHERE q.is_published=TRUE AND q.subject=%s AND COALESCE(q.admin_hidden,FALSE)=FALSE
            AND (q.is_custom=FALSE OR q.custom_owner_telegram_id=%s) ORDER BY q.id DESC LIMIT 200""",(subject,user['id']))
        quizzes=[dict(r) for r in await cur.fetchall()]
        cur=await conn.execute("SELECT access_type FROM question_bank_subjects WHERE LOWER(BTRIM(name))=LOWER(BTRIM(%s)) AND archived_at IS NULL LIMIT 1",(subject,))
        row=await cur.fetchone()
        bank_access=(row['access_type'] if row else 'free')
        state=await _access_state(conn,user['id'],is_admin,True)
        for q in quizzes:
            effective = q['access_type'] if q['access_type'] in ('free','paid') else bank_access
            q['effective_access']=effective
            q['locked']= (effective=='paid' and not state['has_paid_access'])
    return json_response({'subject':subject,'quizzes':quizzes,'access':state})


async def all_quizzes_alias(request):
    """Compatibility endpoint used by the current frontend quiz catalog."""
    user = request["telegram_user"]
    is_admin = _is_series_admin(user)
    async with request.app["pool"].connection() as conn:
        await ensure_access_schema(conn)
        cur = await conn.execute("""
            SELECT q.id, q.title, '' AS description,
                   q.subject, q.duration_minutes,
                   COALESCE((SELECT COUNT(*) FROM bank_quiz_questions qq
                             JOIN question_bank qb ON qb.id = qq.question_id
                             WHERE qq.quiz_id = q.id AND qb.status = 'ready'), 0) AS total_questions,
                   q.positive_marks, q.negative_marks, q.access_type, q.is_custom,
                   COALESCE((SELECT COUNT(*) FROM bank_quiz_questions qq
                             JOIN question_bank qb ON qb.id = qq.question_id
                             WHERE qq.quiz_id = q.id AND qb.status = 'ready'), 0) AS ready_count
            FROM bank_quizzes q
            WHERE q.is_published = TRUE
              AND COALESCE(q.admin_hidden, FALSE) = FALSE
              AND (q.is_custom = FALSE OR q.custom_owner_telegram_id = %s)
            ORDER BY q.id DESC
            LIMIT 200
        """, (user["id"],))
        quizzes = [dict(row) for row in await cur.fetchall()]
        state = await _access_state(conn, user["id"], is_admin, True)
        for quiz in quizzes:
            if quiz["access_type"] == "inherit":
                cur2 = await conn.execute(
                    "SELECT access_type FROM question_bank_subjects "
                    "WHERE LOWER(BTRIM(name)) = LOWER(BTRIM(%s)) "
                    "AND archived_at IS NULL LIMIT 1",
                    (quiz["subject"],),
                )
                row2 = await cur2.fetchone()
                effective = row2["access_type"] if row2 else "free"
            else:
                effective = quiz["access_type"]
            quiz["effective_access"] = effective
            quiz["locked"] = effective == "paid" and not state["has_paid_access"]
    return json_response({"quizzes": quizzes, "access": state})


async def access_alias(request):
    return await access_status(request)


async def test_series_alias(request):
    return await list_series(request)


async def admin_series_list(request):
    user = request['telegram_user']
    if not _is_series_admin(user):
        return json_response({'error': 'Admin access required.'}, 403)
    async with request.app['pool'].connection() as conn:
        await ensure_test_series_schema(conn)
        cur = await conn.execute("""SELECT s.id,s.title,s.description,s.exam_type,s.series_type,s.subject,s.difficulty,s.access_type,s.is_published,s.sort_order,s.logo_url,
            COUNT(DISTINCT st.quiz_id) AS test_count,
            COALESCE(ARRAY_AGG(DISTINCT st.quiz_id) FILTER (WHERE st.quiz_id IS NOT NULL), ARRAY[]::BIGINT[]) AS quiz_ids
            FROM bpsc_nexus_test_series s
            LEFT JOIN bpsc_nexus_test_series_tests st ON st.series_id=s.id GROUP BY s.id ORDER BY s.sort_order,s.id DESC LIMIT 300""")
        rows = [dict(r) for r in await cur.fetchall()]
    return json_response({'series': rows})


async def admin_series_publish(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        sid=int(request.match_info['series_id']); body=await request.json(); published=bool(body.get('is_published',False))
    except Exception: return json_response({'error':'Invalid publish status.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_test_series_schema(conn)
        cur=await conn.execute("UPDATE bpsc_nexus_test_series SET is_published=%s,updated_at=NOW() WHERE id=%s RETURNING id,title,is_published",(published,sid))
        row=await cur.fetchone()
        if not row:return json_response({'error':'Series not found.'},404)
        await _write_admin_audit(conn,user['id'],'series_publish',str(sid),{'is_published':published})
    return json_response({'series':dict(row),'updated':True})


async def admin_series_quiz_catalog(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        cur=await conn.execute("""SELECT q.id,q.title,q.subject,q.is_published,q.access_type,
            COUNT(qq.question_id) AS total_questions FROM bank_quizzes q
            LEFT JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
            GROUP BY q.id ORDER BY q.id DESC LIMIT 500""")
        rows=[dict(r) for r in await cur.fetchall()]
    return json_response({'quizzes':rows})


async def admin_series_create(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        body=await request.json()
        title=str(body.get('title') or '').strip()[:180]
        description=str(body.get('description') or '').strip()[:4000]
        logo_url=str(body.get('logo_url') or '').strip()[:1000]
        exam_type=str(body.get('exam_type') or 'BPSC').upper()
        series_type=str(body.get('series_type') or 'subject_wise').lower()
        subject=str(body.get('subject') or 'General Studies').strip()[:100]
        difficulty=str(body.get('difficulty') or 'Mixed').strip()[:24]
        access_type=str(body.get('access_type') or 'free').lower()
        is_published=bool(body.get('is_published',False))
        sort_order=max(-100000,min(100000,int(body.get('sort_order',0))))
        quiz_ids=list(dict.fromkeys(int(x) for x in (body.get('quiz_ids') or []) if int(x)>0))[:300]
        if not title: raise ValueError('Series title is required.')
        if exam_type not in ('UPSC','BPSC'): raise ValueError('Exam type must be UPSC or BPSC.')
        if series_type not in ('full_length','sectional','subject_wise'): raise ValueError('Invalid series type.')
        if access_type not in ('free','paid'): raise ValueError('Access must be free or paid.')
        if logo_url and not (logo_url.startswith('https://') or logo_url.startswith('http://') or logo_url.startswith('data:image/')): raise ValueError('Logo must be an image URL or data image.')
    except ValueError as e: return json_response({'error':str(e)},400)
    except Exception: return json_response({'error':'Invalid series details.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_test_series_schema(conn)
        if quiz_ids:
            cur=await conn.execute('SELECT id FROM bank_quizzes WHERE id = ANY(%s)',(quiz_ids,))
            found={int(r['id']) for r in await cur.fetchall()}
            missing=[qid for qid in quiz_ids if qid not in found]
            if missing:return json_response({'error':'Quiz not found: '+', '.join('#'+str(x) for x in missing)},400)
        async with conn.transaction():
            cur=await conn.execute("""INSERT INTO bpsc_nexus_test_series
                (title,description,exam_type,series_type,subject,difficulty,access_type,is_published,sort_order,created_by_telegram_id,logo_url)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (title,description,exam_type,series_type,subject,difficulty,access_type,is_published,sort_order,int(user['id']),logo_url))
            sid=int((await cur.fetchone())['id'])
            for pos,qid in enumerate(quiz_ids,1):
                await conn.execute("INSERT INTO bpsc_nexus_test_series_tests(series_id,quiz_id,position) VALUES(%s,%s,%s) ON CONFLICT(series_id,quiz_id) DO UPDATE SET position=EXCLUDED.position",(sid,qid,pos))
            await _write_admin_audit(conn,user['id'],'series_create',str(sid),{'title':title,'quiz_ids':quiz_ids,'is_published':is_published,'access_type':access_type})
    return json_response({'created':True,'series_id':sid,'title':title,'test_count':len(quiz_ids)})


async def admin_series_update(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    try:
        sid=int(request.match_info['series_id']); body=await request.json()
        title=str(body.get('title') or '').strip()[:180]
        description=str(body.get('description') or '').strip()[:4000]
        logo_url=str(body.get('logo_url') or '').strip()[:1000]
        exam_type=str(body.get('exam_type') or 'BPSC').upper()
        series_type=str(body.get('series_type') or 'subject_wise').lower()
        subject=str(body.get('subject') or 'General Studies').strip()[:100]
        difficulty=str(body.get('difficulty') or 'Mixed').strip()[:24]
        access_type=str(body.get('access_type') or 'free').lower()
        is_published=bool(body.get('is_published',False))
        sort_order=max(-100000,min(100000,int(body.get('sort_order',0))))
        quiz_ids=list(dict.fromkeys(int(x) for x in (body.get('quiz_ids') or []) if int(x)>0))[:300]
        if not title: raise ValueError('Series title is required.')
        if exam_type not in ('UPSC','BPSC') or series_type not in ('full_length','sectional','subject_wise') or access_type not in ('free','paid'): raise ValueError('Invalid exam, series or access type.')
        if logo_url and not (logo_url.startswith('https://') or logo_url.startswith('http://') or logo_url.startswith('data:image/')): raise ValueError('Logo must be an image URL or data image.')
    except ValueError as e: return json_response({'error':str(e)},400)
    except Exception: return json_response({'error':'Invalid series update.'},400)
    async with request.app['pool'].connection() as conn:
        await ensure_test_series_schema(conn)
        if quiz_ids:
            cur=await conn.execute('SELECT id FROM bank_quizzes WHERE id = ANY(%s)',(quiz_ids,))
            found={int(r['id']) for r in await cur.fetchall()}
            missing=[qid for qid in quiz_ids if qid not in found]
            if missing:return json_response({'error':'Quiz not found: '+', '.join('#'+str(x) for x in missing)},400)
        async with conn.transaction():
            cur=await conn.execute("""UPDATE bpsc_nexus_test_series SET title=%s,description=%s,logo_url=%s,exam_type=%s,series_type=%s,subject=%s,difficulty=%s,access_type=%s,is_published=%s,sort_order=%s,updated_at=NOW() WHERE id=%s RETURNING id""",
                (title,description,logo_url,exam_type,series_type,subject,difficulty,access_type,is_published,sort_order,sid))
            if not await cur.fetchone(): return json_response({'error':'Series not found.'},404)
            await conn.execute('DELETE FROM bpsc_nexus_test_series_tests WHERE series_id=%s',(sid,))
            for pos,qid in enumerate(quiz_ids,1):
                await conn.execute('INSERT INTO bpsc_nexus_test_series_tests(series_id,quiz_id,position) VALUES(%s,%s,%s)',(sid,qid,pos))
            await _write_admin_audit(conn,user['id'],'series_update',str(sid),{'title':title,'quiz_ids':quiz_ids,'is_published':is_published,'access_type':access_type})
    return json_response({'updated':True,'series_id':sid,'title':title,'test_count':len(quiz_ids)})


async def debug_env(request):
    """Admin-only, non-secret deployment diagnostics."""
    user = request["telegram_user"]
    if int(user.get("id", 0)) not in ADMIN_TELEGRAM_IDS:
        return json_response({"error": "Administrator access required."}, 403)
    return json_response({
        "ADMIN_LOGIN_ID_set": bool(ADMIN_LOGIN_ID),
        "ADMIN_LOGIN_PASSWORD_set": bool(ADMIN_LOGIN_PASSWORD),
        "ADMIN_LOGIN_TELEGRAM_ID_configured": ADMIN_LOGIN_TELEGRAM_ID > 0,
        "ADMIN_TELEGRAM_IDS_count": len(ADMIN_TELEGRAM_IDS),
        "BOT_TOKEN_set": bool(BOT_TOKEN),
        "DATABASE_URL_set": bool(DATABASE_URL),
    })


async def _create_quiz_attempt(request, qid: int, mode: str):
    user = request['telegram_user']
    mode = str(mode or 'exam').strip().lower()
    if mode not in ('practice', 'exam'):
        return json_response({'error': 'Mode must be practice or exam.'}, 400)
    pool = request.app['pool']
    async with pool.connection() as conn:
        await qb.ensure_schema(pool)
        await ensure_access_schema(conn)
        gate_ok,gate_reason=await _demo_gate(conn,int(user['id']),_is_series_admin(user))
        if not gate_ok:
            if gate_reason in ('register','no_trial'):
                return json_response({'error':'Please register for the free demo first to attempt quizzes.','demo_gate':'register'},403)
            return json_response({'error':'Your free demo has ended. Unlock a pass to keep attempting quizzes.','demo_gate':'expired'},403)
        cur = await conn.execute("SELECT id,title,subject,duration_minutes,positive_marks,negative_marks,question_time_seconds,shuffle_options,is_custom,custom_owner_telegram_id,is_published,access_type FROM bank_quizzes WHERE id=%s", (qid,))
        quiz = await cur.fetchone()
        if not quiz:
            return json_response({'error': 'Quiz not found.'}, 404)
        if quiz['is_custom'] and int(quiz['custom_owner_telegram_id'] or 0) != user['id'] and not _is_series_admin(user):
            return json_response({'error': 'This is a private quiz.'}, 403)
        if not quiz['is_published'] and not _is_series_admin(user):
            return json_response({'error': 'This quiz is not published.'}, 403)
        cur = await conn.execute("SELECT qq.question_id FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id WHERE qq.quiz_id=%s AND qb.status='ready' ORDER BY qq.position,qq.question_id", (qid,))
        qids = [int(r['question_id']) for r in await cur.fetchall()]
        if not qids:
            return json_response({'error': 'This quiz has no verified questions yet.'}, 503)
        # Enforce paid access unless the Telegram user is an admin.
        cur = await conn.execute("SELECT access_type FROM question_bank_subjects WHERE LOWER(BTRIM(name))=LOWER(BTRIM(%s)) AND archived_at IS NULL LIMIT 1", (quiz['subject'],))
        bank_row = await cur.fetchone()
        effective_access = quiz['access_type'] if quiz['access_type'] in ('free', 'paid') else (bank_row['access_type'] if bank_row else 'free')
        access = await _access_state(conn, user['id'], _is_series_admin(user), True)
        if effective_access == 'paid' and not access['has_paid_access']:
            return json_response({'error': 'This quiz requires paid access. Open membership options to continue.'}, 403)
        settings = {'mode_label': 'Practice' if mode == 'practice' else 'Exam', 'negative_marks': float(quiz['negative_marks'])}
        cur = await conn.execute("""INSERT INTO bank_quiz_attempts(telegram_id,quiz_id,mode,quiz_settings,total_questions,started_at,question_time_seconds)
            VALUES(%s,%s,%s,%s::jsonb,%s,NOW(),%s) RETURNING id,started_at""",
            (user['id'], qid, mode, json.dumps(settings, ensure_ascii=False), len(qids), int(quiz['question_time_seconds'] or 0)))
        attempt = await cur.fetchone()
        attempt_id = int(attempt['id'])
        for pos, question_id in enumerate(qids, 1):
            cur = await conn.execute("SELECT id,subject,question_en,question_hi,question_type,question_structure,question_flags,options,correct_option,explanation_en,explanation_hi FROM question_bank WHERE id=%s", (question_id,))
            q = await cur.fetchone()
            if not q:
                continue
            opts = q['options'] if isinstance(q['options'], (dict, list)) else json.loads(q['options'] or '{}')
            await conn.execute("""INSERT INTO bank_quiz_attempt_question_snapshots(attempt_id,position,question_id,subject,question_en,question_hi,question_type,question_structure,question_flags,options,correct_option,explanation_en,explanation_hi)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s)""",
                (attempt_id, pos, question_id, q['subject'], q['question_en'], q['question_hi'], q['question_type'],
                 json.dumps(q['question_structure'] or {}, ensure_ascii=False), json.dumps(q['question_flags'] or {}, ensure_ascii=False),
                 json.dumps(opts, ensure_ascii=False), q['correct_option'], q['explanation_en'], q['explanation_hi']))
    return json_response({'attempt_id': attempt_id, 'quiz_id': qid, 'title': quiz['title'], 'subject': quiz['subject'],
        'mode': mode, 'mode_label': settings['mode_label'], 'total_questions': len(qids), 'duration_minutes': quiz['duration_minutes'],
        'question_time_seconds': int(quiz['question_time_seconds'] or 0), 'positive_marks': float(quiz['positive_marks']),
        'negative_marks': float(quiz['negative_marks']), 'shuffle_options': bool(quiz['shuffle_options']),
        'started_at': attempt['started_at'].isoformat()}, 201)


async def quiz_start(request):
    try:
        qid = int(request.match_info['quiz_id'])
    except (ValueError, KeyError):
        return json_response({'error': 'Invalid quiz ID.'}, 400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _create_quiz_attempt(request, qid, body.get('mode', 'exam'))


async def create_attempt_compat(request):
    try:
        body = await request.json()
        qid = int(body.get('quiz_id'))
    except Exception:
        return json_response({'error': 'Provide a valid quiz_id.'}, 400)
    return await _create_quiz_attempt(request, qid, body.get('mode', 'exam'))


async def attempt_history_compat(request):
    user_id = request['telegram_user']['id']
    try:
        limit = max(1, min(100, int(request.query.get('limit', '12'))))
    except ValueError:
        limit = 12
    async with request.app['pool'].connection() as conn:
        cur = await conn.execute("""SELECT a.id,a.quiz_id,a.mode,a.total_questions,a.correct_count,a.incorrect_count,a.skipped_count,a.score,a.started_at,a.submitted_at,q.title,q.subject,q.positive_marks
            FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id
            WHERE a.telegram_id=%s AND a.submitted_at IS NOT NULL ORDER BY a.submitted_at DESC LIMIT %s""", (user_id, limit))
        rows = []
        for r in await cur.fetchall():
            attempted = int(r['correct_count'] or 0) + int(r['incorrect_count'] or 0)
            rows.append({'attempt_id': int(r['id']), 'id': int(r['id']), 'quiz_id': int(r['quiz_id']), 'mode': r['mode'],
                         'total_questions': int(r['total_questions']), 'correct': int(r['correct_count'] or 0),
                         'incorrect': int(r['incorrect_count'] or 0), 'skipped': int(r['skipped_count'] or 0),
                         'score': float(r['score'] or 0), 'max_score': float(r['total_questions']) * float(r['positive_marks']),
                         'accuracy': round(int(r['correct_count'] or 0) / attempted * 100, 1) if attempted else 0,
                         'started_at': r['started_at'].isoformat() if r['started_at'] else None,
                         'submitted_at': r['submitted_at'].isoformat() if r['submitted_at'] else None,
                         'title': r['title'], 'subject': r['subject']})
    return json_response({'attempts': rows})


async def attempt_review(request):
    user = request['telegram_user']
    try:
        aid = int(request.match_info['attempt_id'])
        body = await request.json()
        qid = int(body.get('question_id'))
        marked = bool(body.get('marked_for_review', False))
    except Exception:
        return json_response({'error': 'Invalid review payload.'}, 400)
    async with request.app['pool'].connection() as conn:
        cur = await conn.execute("SELECT a.submitted_at FROM bank_quiz_attempts a JOIN bank_quiz_attempt_question_snapshots s ON s.attempt_id=a.id AND s.question_id=%s WHERE a.id=%s AND a.telegram_id=%s", (qid, aid, user['id']))
        row = await cur.fetchone()
        if not row:
            return json_response({'error': 'Question not part of this attempt.'}, 404)
        if row['submitted_at']:
            return json_response({'error': 'Attempt already submitted.'}, 409)
        await conn.execute("""INSERT INTO bank_quiz_attempt_answers(attempt_id,question_id,selected_option,is_marked_review)
            VALUES(%s,%s,NULL,%s) ON CONFLICT(attempt_id,question_id) DO UPDATE SET is_marked_review=EXCLUDED.is_marked_review""", (aid, qid, marked))
    return json_response({'saved': True, 'question_id': qid, 'marked_for_review': marked})


async def attempt_state(request):
    user=request['telegram_user']
    try: aid=int(request.match_info['attempt_id'])
    except ValueError: return json_response({'error':'Invalid attempt ID.'},400)
    pool=request.app['pool']
    async with pool.connection() as conn:
        attempt=await _attempt_row(pool,aid,user['id'])
        if not attempt: return json_response({'error':'Attempt not found.'},404)
        if attempt['submitted_at']:
            payload=await _result_payload(conn,aid,user['id'])
            return json_response(payload or {'error':'Result unavailable.'},404 if not payload else 200)
        cur=await conn.execute("""SELECT s.position,s.question_id AS id,s.subject,s.question_en,s.question_hi,s.question_type,s.question_structure,s.question_flags,s.options,aa.selected_option,COALESCE(aa.is_marked_review,FALSE) AS is_marked_review
            FROM bank_quiz_attempt_question_snapshots s
            LEFT JOIN bank_quiz_attempt_answers aa ON aa.attempt_id=s.attempt_id AND aa.question_id=s.question_id
            WHERE s.attempt_id=%s ORDER BY s.position""",(aid,))
        questions=[]
        for r in await cur.fetchall():
            opts=r['options'] if isinstance(r['options'],dict) else json.loads(r['options'] or '{}')
            questions.append({'position':int(r['position']),'id':int(r['id']),'subject':r['subject'],'question_en':r['question_en'],'question_hi':r['question_hi'],
                'question_type':r['question_type'],
                'question_structure':r['question_structure'] if isinstance(r['question_structure'],dict) else json.loads(r['question_structure'] or '{}'),
                'question_flags':r['question_flags'] if isinstance(r['question_flags'],dict) else json.loads(r['question_flags'] or '{}'),
                'options':opts,'selected_option':r['selected_option'],'marked_review':bool(r['is_marked_review'])})
    deadline = (attempt['started_at'] + timedelta(minutes=int(attempt['duration_minutes'] or 60))).isoformat() if attempt['mode'] == 'exam' else None
    settings = attempt['quiz_settings'] if isinstance(attempt['quiz_settings'], dict) else json.loads(attempt['quiz_settings'] or '{}')
    return json_response({'attempt_id':aid,'quiz_id':int(attempt['quiz_id']),'title':attempt['title'],'subject':attempt['subject'],'mode':attempt['mode'],'mode_label':settings.get('mode_label', attempt['mode']),'deadline':deadline,
        'total_questions':int(attempt['total_questions']),'duration_minutes':attempt['duration_minutes'],
        'question_time_seconds':int(attempt['question_time_seconds'] or 60),
        'positive_marks':float(attempt['positive_marks']),'negative_marks':float(attempt['negative_marks']),
        'shuffle_options':bool(attempt['shuffle_options']),'started_at':attempt['started_at'].isoformat(),'questions':questions})


async def attempt_answer(request):
    user = request['telegram_user']
    try:
        aid = int(request.match_info['attempt_id'])
        body = await request.json()
        qid = int(body.get('question_id'))
        selected = str(body.get('selected_option') or '').strip().upper() or None
    except Exception:
        return json_response({'error': 'Invalid answer payload.'}, 400)
    if selected and selected not in {'A', 'B', 'C', 'D', 'E'}:
        return json_response({'error': 'Choose A, B, C, D or E.'}, 400)
    async with request.app['pool'].connection() as conn:
        cur = await conn.execute("""SELECT a.id,a.mode,a.submitted_at,s.correct_option,s.explanation_en,s.explanation_hi
            FROM bank_quiz_attempts a JOIN bank_quiz_attempt_question_snapshots s ON s.attempt_id=a.id AND s.question_id=%s
            WHERE a.id=%s AND a.telegram_id=%s""", (qid, aid, user['id']))
        row = await cur.fetchone()
        if not row:
            return json_response({'error': 'Question not part of this attempt.'}, 404)
        if row['submitted_at']:
            return json_response({'error': 'Attempt already submitted.'}, 409)
        await conn.execute("""INSERT INTO bank_quiz_attempt_answers(attempt_id,question_id,selected_option)
            VALUES(%s,%s,%s) ON CONFLICT(attempt_id,question_id) DO UPDATE SET selected_option=EXCLUDED.selected_option""", (aid, qid, selected))
        result = {'saved': True, 'question_id': qid, 'selected_option': selected}
        if row['mode'] == 'practice':
            result['feedback'] = {'correct_option': row['correct_option'], 'is_correct': bool(selected and selected == row['correct_option']),
                                  'explanation_en': row['explanation_en'] or '', 'explanation_hi': row['explanation_hi'] or ''}
    return json_response(result)


async def attempt_submit(request):
    user=request['telegram_user']
    try: aid=int(request.match_info['attempt_id'])
    except ValueError: return json_response({'error':'Invalid attempt ID.'},400)
    result=await _grade_attempt(request.app['pool'],aid,user['id'],user.get('first_name','Student'))
    if not result: return json_response({'error':'Attempt not found.'},404)
    return json_response(result)


async def attempt_result(request):
    user=request['telegram_user']
    try: aid=int(request.match_info['attempt_id'])
    except ValueError: return json_response({'error':'Invalid attempt ID.'},400)
    async with request.app['pool'].connection() as conn:
        payload=await _result_payload(conn,aid,user['id'])
    if not payload: return json_response({'error':'Result not found.'},404)
    return json_response(payload)


async def my_attempts(request):
    user=request['telegram_user']['id']
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("""SELECT a.id,a.quiz_id,a.mode,a.total_questions,a.correct_count,a.incorrect_count,a.skipped_count,a.score,a.started_at,a.submitted_at,q.title,q.subject,q.positive_marks
            FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id
            WHERE a.telegram_id=%s AND a.submitted_at IS NOT NULL ORDER BY a.submitted_at DESC LIMIT 100""",(user,))
        rows=[]
        for r in await cur.fetchall():
            d=dict(r); d['id']=int(d['id']); d['quiz_id']=int(d['quiz_id']); d['score']=float(d['score'] or 0)
            d['max_score']=float(d['total_questions'])*float(d['positive_marks'])
            attempted=int(d['correct_count'] or 0)+int(d['incorrect_count'] or 0)
            d['accuracy']=round(int(d['correct_count'] or 0)/attempted*100,1) if attempted else 0
            for k in ('started_at','submitted_at'):
                if d.get(k): d[k]=d[k].isoformat()
            rows.append(d)
    return json_response({'attempts':rows})


async def app_me(request):
    user=request['telegram_user']
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        state=await _access_state(conn,user['id'],_is_series_admin(user),True)
    return json_response({'user':user,'is_admin':_is_series_admin(user),'access':state})


async def register_user(request):
    user=request['telegram_user']
    async with request.app['pool'].connection() as conn:
        await conn.execute("""INSERT INTO app_users(telegram_id,first_name,username,last_seen_at)
            VALUES(%s,%s,%s,NOW())
            ON CONFLICT(telegram_id) DO UPDATE SET first_name=EXCLUDED.first_name,username=EXCLUDED.username,last_seen_at=NOW()""",
            (user['id'],user.get('first_name','Student'),user.get('username')))
    return json_response({'registered':True})



async def admin_bulk_delete_quizzes(request):
    admin = request['telegram_user']
    if not _is_series_admin(admin):
        return json_response({'error': 'Admin access required.'}, 403)
    try:
        body = await request.json()
        quiz_ids = sorted({int(x) for x in (body.get('quiz_ids') or []) if int(x) > 0})
    except Exception:
        return json_response({'error': 'Provide a quiz_ids array.'}, 400)
    if not quiz_ids:
        return json_response({'error': 'No quizzes selected.'}, 400)
    deleted = archived = errors = 0
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        await ensure_test_series_schema(conn)
        await lb.ensure_schema(request.app['pool'])
        for qid in quiz_ids:
            try:
                cur = await conn.execute('SELECT id,title FROM bank_quizzes WHERE id=%s', (qid,))
                quiz = await cur.fetchone()
                if not quiz:
                    errors += 1
                    continue
                cur = await conn.execute('SELECT COUNT(*) AS n FROM bank_quiz_attempts WHERE quiz_id=%s', (qid,))
                attempts = int((await cur.fetchone())['n'] or 0)
                cur = await conn.execute('SELECT COUNT(*) AS n FROM bpsc_leaderboard_daily_challenges WHERE quiz_id=%s', (qid,))
                challenges = int((await cur.fetchone())['n'] or 0)
                cur = await conn.execute("SELECT to_regclass('public.group_quiz_sessions') AS t")
                group_table = await cur.fetchone()
                group_sessions = 0
                if group_table and group_table['t']:
                    cur = await conn.execute('SELECT COUNT(*) AS n FROM group_quiz_sessions WHERE quiz_id=%s', (qid,))
                    group_sessions = int((await cur.fetchone())['n'] or 0)
                if attempts or challenges or group_sessions:
                    await conn.execute('UPDATE bank_quizzes SET admin_hidden=TRUE,is_published=FALSE WHERE id=%s', (qid,))
                    archived += 1
                else:
                    await conn.execute('DELETE FROM bpsc_nexus_test_series_tests WHERE quiz_id=%s', (qid,))
                    await conn.execute('DELETE FROM bank_quiz_questions WHERE quiz_id=%s', (qid,))
                    await conn.execute('DELETE FROM bank_quizzes WHERE id=%s', (qid,))
                    deleted += 1
            except Exception:
                log.exception('Bulk quiz delete failed for %s', qid)
                errors += 1
        await _write_admin_audit(conn, admin['id'], 'bulk_quiz_delete', 'bulk',
                                 {'deleted': deleted, 'archived': archived, 'errors': errors})
    return json_response({'deleted': deleted, 'archived': archived, 'errors': errors})


async def admin_activity(request):
    if not _is_series_admin(request['telegram_user']):
        return json_response({'error': 'Admin access required.'}, 403)
    async with request.app['pool'].connection() as conn:
        cur = await conn.execute("""SELECT
            COUNT(DISTINCT telegram_id) FILTER (WHERE last_seen_at>NOW()-INTERVAL '5 minutes') AS active_5min,
            COUNT(DISTINCT telegram_id) FILTER (WHERE last_seen_at>NOW()-INTERVAL '1 hour') AS active_hour
            FROM app_users""")
        user_stats = dict(await cur.fetchone() or {})
        cur = await conn.execute("""SELECT COUNT(*) AS n FROM bank_quiz_attempts
            WHERE started_at>=date_trunc('day',NOW())""")
        attempts_today = int((await cur.fetchone())['n'] or 0)
        cur = await conn.execute("""SELECT COUNT(*) AS n FROM app_users
            WHERE last_seen_at>=NOW()-INTERVAL '1 day'""")
        seen_today = int((await cur.fetchone())['n'] or 0)
        cur = await conn.execute("""SELECT telegram_id,first_name,username,last_seen_at
            FROM app_users ORDER BY last_seen_at DESC NULLS LAST LIMIT 15""")
        feed = []
        for row in await cur.fetchall():
            feed.append({'icon': '👤', 'title': row['first_name'] or 'Student',
                         'subtitle': ('@' + row['username']) if row['username'] else ('TG ' + str(row['telegram_id'])),
                         'time': row['last_seen_at'].strftime('%H:%M') if row['last_seen_at'] else ''})
    return json_response({'active_5min': int(user_stats.get('active_5min') or 0),
                          'active_hour': int(user_stats.get('active_hour') or 0),
                          'attempts_today': attempts_today, 'new_users_today': seen_today, 'feed': feed})


async def admin_revenue(request):
    if not _is_series_admin(request['telegram_user']):
        return json_response({'error': 'Admin access required.'}, 403)
    async with request.app['pool'].connection() as conn:
        await payment_schema(conn)
        cur = await conn.execute("""SELECT
            COALESCE(SUM(amount_inr) FILTER (WHERE status='approved'),0) AS total,
            COUNT(*) FILTER (WHERE status='approved') AS approvals,
            COALESCE(SUM(amount_inr) FILTER (WHERE status='approved' AND reviewed_at>=date_trunc('month',NOW())),0) AS this_month,
            COALESCE(SUM(amount_inr) FILTER (WHERE status='approved' AND reviewed_at>=date_trunc('day',NOW())),0) AS today,
            COALESCE(SUM(amount_inr) FILTER (WHERE status='pending'),0) AS pending_value
            FROM bpsc_payment_requests""")
        row = dict(await cur.fetchone())
        cur = await conn.execute("""SELECT DATE(reviewed_at) AS d,SUM(amount_inr) AS amount
            FROM bpsc_payment_requests WHERE status='approved' AND reviewed_at>=NOW()-INTERVAL '7 days'
            GROUP BY DATE(reviewed_at) ORDER BY d""")
        daily = [{'label': r['d'].strftime('%a'), 'amount': float(r['amount'] or 0)} for r in await cur.fetchall()]
        cur = await conn.execute("""SELECT product_name AS name,COUNT(*) AS count,SUM(amount_inr) AS amount
            FROM bpsc_payment_requests WHERE status='approved' GROUP BY product_name ORDER BY amount DESC LIMIT 5""")
        top = [{'name': r['name'], 'count': int(r['count']), 'amount': float(r['amount'] or 0)} for r in await cur.fetchall()]
    for key in ('total','this_month','today','pending_value'):
        row[key] = float(row.get(key) or 0)
    row['approvals'] = int(row.get('approvals') or 0)
    row['daily'] = daily
    row['top_products'] = top
    return json_response(row)


async def admin_export(request):
    if not _is_series_admin(request['telegram_user']):
        return json_response({'error': 'Admin access required.'}, 403)
    kind = request.match_info['kind']
    allowed = {'users','payments','attempts','demo','notifications','all'}
    if kind not in allowed:
        return json_response({'error': 'Unknown export type.'}, 400)
    buf = io.StringIO()
    writer = csv.writer(buf)
    async with request.app['pool'].connection() as conn:
        if kind == 'users':
            writer.writerow(['telegram_id','first_name','username','last_seen_at'])
            cur = await conn.execute('SELECT telegram_id,first_name,username,last_seen_at FROM app_users ORDER BY telegram_id')
            for r in await cur.fetchall(): writer.writerow([r['telegram_id'],r['first_name'],r['username'],r['last_seen_at']])
        elif kind == 'payments':
            await payment_schema(conn)
            await ensure_demo_verify_schema(conn)
            writer.writerow(['id','telegram_id','product_name','amount_inr','status','transaction_ref','submitted_at','reviewed_at'])
            cur = await conn.execute('SELECT id,telegram_id,product_name,amount_inr,status,transaction_ref,submitted_at,reviewed_at FROM bpsc_payment_requests ORDER BY id DESC')
            for r in await cur.fetchall(): writer.writerow([r['id'],r['telegram_id'],r['product_name'],r['amount_inr'],r['status'],r['transaction_ref'],r['submitted_at'],r['reviewed_at']])
        elif kind == 'attempts':
            writer.writerow(['id','telegram_id','quiz_id','mode','score','correct','incorrect','skipped','submitted_at'])
            cur = await conn.execute('SELECT id,telegram_id,quiz_id,mode,score,correct_count,incorrect_count,skipped_count,submitted_at FROM bank_quiz_attempts WHERE submitted_at IS NOT NULL ORDER BY id DESC LIMIT 50000')
            for r in await cur.fetchall(): writer.writerow([r['id'],r['telegram_id'],r['quiz_id'],r['mode'],r['score'],r['correct_count'],r['incorrect_count'],r['skipped_count'],r['submitted_at']])
        elif kind == 'demo':
            await ensure_access_schema(conn)
            writer.writerow(['telegram_id','username','contact','contact_type','registered_at','demo_started','expires_at'])
            cur = await conn.execute("SELECT r.telegram_id,r.telegram_username,r.submitted_contact,r.contact_type,r.registered_at,t.started_at,t.expires_at FROM bpsc_nexus_demo_registrations r LEFT JOIN bpsc_nexus_demo_trials t ON t.telegram_id=r.telegram_id ORDER BY r.registered_at DESC")
            for r in await cur.fetchall(): writer.writerow([r['telegram_id'],r['telegram_username'],r['submitted_contact'],r['contact_type'],r['registered_at'],r['started_at'],r['expires_at']])
        elif kind == 'notifications':
            await ensure_notifications_schema(conn)
            writer.writerow(['id','title','segment','recipients','created_at'])
            cur = await conn.execute('SELECT id,title,segment,recipients,created_at FROM bpsc_nexus_notifications ORDER BY id DESC')
            for r in await cur.fetchall(): writer.writerow([r['id'],r['title'],r['segment'],r['recipients'],r['created_at']])
        else:
            writer.writerow(['section','count'])
            for table in ('app_users','bank_quiz_attempts','bpsc_payment_requests','bpsc_nexus_demo_registrations','bpsc_nexus_notifications'):
                cur = await conn.execute('SELECT to_regclass(%s) AS table_name', (table,))
                exists = await cur.fetchone()
                if not exists or not exists['table_name']:
                    writer.writerow([table, 'unavailable'])
                else:
                    cur = await conn.execute(f'SELECT COUNT(*) AS n FROM {table}')
                    writer.writerow([table, int((await cur.fetchone())['n'])])
    return web.Response(body=buf.getvalue().encode('utf-8'), content_type='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="bpsc_nexus_{kind}.csv"'})


async def admin_get_banner_flat(request):
    if not _is_series_admin(request['telegram_user']):
        return json_response({'error': 'Admin access required.'}, 403)
    async with request.app['pool'].connection() as conn:
        banner = await _get_setting(conn, 'promo_banner', {}) or {}
    return json_response({'enabled': bool(banner.get('enabled')), 'tag': banner.get('tag',''),
                          'title': banner.get('title',''), 'message': banner.get('message',''),
                          'link': banner.get('link',''), 'link_label': banner.get('link_label','')})


async def admin_get_maintenance(request):
    if not _is_series_admin(request['telegram_user']):
        return json_response({'error': 'Admin access required.'}, 403)
    async with request.app['pool'].connection() as conn:
        value = await _get_setting(conn, 'maintenance', {'enabled': False}) or {}
    return json_response({'enabled': bool(value.get('enabled')), 'message': value.get('message','')})


async def admin_members_search_alias(request):
    return await admin_members_search(request)


async def quiz_builder_meta(request):
    await qb.ensure_schema(request.app['pool'])
    async with request.app['pool'].connection() as conn:
        cur = await conn.execute("SELECT subject AS name,COUNT(*) AS count FROM question_bank WHERE status='ready' GROUP BY subject ORDER BY subject")
        subjects = [{'name': r['name'], 'count': int(r['count'])} for r in await cur.fetchall() if r['name']]
        cur = await conn.execute("SELECT DISTINCT topic FROM question_bank WHERE status='ready' AND topic IS NOT NULL ORDER BY topic LIMIT 200")
        topics = [r['topic'] for r in await cur.fetchall() if r['topic']]
        cur = await conn.execute("SELECT COUNT(*) AS n FROM question_bank WHERE status='ready'")
        total = int((await cur.fetchone())['n'] or 0)
    return json_response({'subjects': subjects, 'topics': topics, 'total_ready': total})


async def quiz_builder_create(request):
    user = request['telegram_user']
    async with request.app['pool'].connection() as _gate_conn:
        gate_ok,gate_reason=await _demo_gate(_gate_conn,int(user['id']),_is_series_admin(user))
    if not gate_ok:
        return json_response({'error':('Register for the free demo first to build custom quizzes.' if gate_reason in ('register','no_trial') else 'Your demo has ended. Unlock a pass to keep building.'),'demo_gate':'register' if gate_reason in ('register','no_trial') else 'expired'},403)
    try:
        body = await request.json()
        subject = str(body.get('subject') or 'All subjects').strip()
        topic = str(body.get('topic') or '').strip()
        count = max(1, min(20000, int(body.get('count', 10))))
        selection = str(body.get('selection') or 'random').lower()
        mode = str(body.get('mode') or 'practice').lower()
        title = str(body.get('title') or 'My Custom Quiz').strip()[:180]
        positive = max(0.0, min(100.0, float(body.get('positive_marks', 1))))
        negative = max(0.0, min(100.0, float(body.get('negative_marks', 0.33))))
        qtime = max(0, min(3600, int(body.get('question_time_seconds', 0))))
        duration_minutes = max(1, min(720, int(body.get('duration_minutes', 360))))
        shuffle = bool(body.get('shuffle_options', True))
        use_all = bool(body.get('use_all_questions', False))
        if mode not in ('practice','exam'): mode = 'practice'
    except Exception:
        return json_response({'error': 'Invalid quiz builder settings.'}, 400)
    await qb.ensure_schema(request.app['pool'])
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        clauses = ["status='ready'"]
        params = []
        if subject and subject != 'All subjects':
            clauses.append('subject=%s'); params.append(subject)
        if topic:
            clauses.append('topic=%s'); params.append(topic)
        order = 'ORDER BY RANDOM()' if selection == 'random' else 'ORDER BY id'
        limit = '' if use_all else f' LIMIT {count}'
        cur = await conn.execute(f"SELECT id FROM question_bank WHERE {' AND '.join(clauses)} {order}{limit}", tuple(params))
        ids = [int(r['id']) for r in await cur.fetchall()]
        if not ids:
            return json_response({'error': 'No matching questions found.'}, 404)
        cur = await conn.execute("""INSERT INTO bank_quizzes
            (title,description,subject,created_by_telegram_id,duration_minutes,positive_marks,negative_marks,
             question_time_seconds,shuffle_options,is_custom,custom_owner_telegram_id,is_published,access_type)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,TRUE,'free') RETURNING id""",
            (title, f'Custom quiz with {len(ids)} questions', subject if subject != 'All subjects' else 'Custom',
             user['id'], duration_minutes, positive, negative, qtime, shuffle, user['id']))
        qid = int((await cur.fetchone())['id'])
        for pos, question_id in enumerate(ids, 1):
            await conn.execute("INSERT INTO bank_quiz_questions(quiz_id,question_id,position) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (qid, question_id, pos))
    return json_response({'quiz_id': qid, 'count': len(ids), 'mode': mode})


async def arena_matches_compat(request):
    if request.method == 'POST':
        return await arena_create(request)
    return await arena_list(request)


async def arena_match_compat(request):
    return await arena_match(request)


async def arena_answer_compat(request):
    return await arena_answer(request)


async def cutoff_xray(request):
    user = request['telegram_user']
    await lb.ensure_schema(request.app['pool'])
    async with request.app['pool'].connection() as conn:
        cur = await conn.execute("""WITH ranked AS (
            SELECT telegram_id,normalized_points,
                   PERCENT_RANK() OVER (ORDER BY normalized_points ASC)*100 AS pct
            FROM bpsc_leaderboard_period_totals
            WHERE period_type='lifetime' AND period_key='all' AND subject='All subjects'
        )
        SELECT pct,normalized_points,
               (SELECT COUNT(*) FROM ranked) AS total_users,
               (SELECT COUNT(*) FROM ranked r2 WHERE r2.normalized_points>ranked.normalized_points)+1 AS rank
        FROM ranked WHERE telegram_id=%s""", (user['id'],))
        row = await cur.fetchone()
        if not row:
            return json_response({'available': False, 'message': 'Complete at least one full test to unlock your Cut-Off X-Ray.'})
        percentile = round(float(row['pct'] or 0), 1)
        cutoff = 85.0
        gap = round(cutoff-percentile, 1)
        cur = await conn.execute("""SELECT subject,
            SUM(correct_count)::float/NULLIF(SUM(correct_count+incorrect_count),0)*100 AS accuracy,
            SUM(correct_count+incorrect_count) AS attempted
            FROM bpsc_leaderboard_period_totals
            WHERE telegram_id=%s AND period_type='lifetime' AND subject<>'All subjects'
            GROUP BY subject HAVING SUM(correct_count+incorrect_count)>=5
            ORDER BY accuracy ASC LIMIT 3""", (user['id'],))
        weak = [{'subject': r['subject'], 'accuracy': round(float(r['accuracy'] or 0), 1),
                 'attempted': int(r['attempted'] or 0)} for r in await cur.fetchall()]
    eligible = percentile >= cutoff
    headline = "You're inside the qualifying band." if eligible else f"You're {gap:.1f} percentile points below cut-off."
    subline = 'Push your weakest subject up 5% to lock your place.' if eligible else 'Close the gap with a targeted 30-day plan.'
    return json_response({'available': True, 'percentile': percentile, 'cutoff_percentile': cutoff,
                          'gap': gap, 'rank': int(row['rank'] or 0), 'total_users': int(row['total_users'] or 0),
                          'weak_subjects': weak, 'is_eligible': eligible, 'headline': headline, 'subline': subline,
                          'share_text': f"I scored {percentile} percentile on BPSC Nexus — {gap:+.1f} from cut-off zone. Beat me 👉 t.me/BPSCNexusBot"})


async def weakness_arsenal(request):
    user = request['telegram_user']
    try:
        body = await request.json() if request.can_read_body else {}
    except Exception:
        body = {}
    generate = bool(body.get('generate'))
    try:
        total_questions = max(10, min(100, int(body.get('count', 50))))
    except Exception:
        total_questions = 50
    await qb.ensure_schema(request.app['pool'])
    async with request.app['pool'].connection() as conn:
        cur = await conn.execute("""WITH recent AS (
            SELECT id FROM bank_quiz_attempts WHERE telegram_id=%s AND submitted_at IS NOT NULL
            ORDER BY submitted_at DESC LIMIT 10
        )
        SELECT s.subject,COUNT(*) AS total,
               SUM(CASE WHEN aa.selected_option=s.correct_option THEN 1 ELSE 0 END) AS correct
        FROM bank_quiz_attempt_question_snapshots s JOIN recent r ON r.id=s.attempt_id
        LEFT JOIN bank_quiz_attempt_answers aa ON aa.attempt_id=s.attempt_id AND aa.question_id=s.question_id
        WHERE NULLIF(BTRIM(s.subject),'') IS NOT NULL
        GROUP BY s.subject HAVING COUNT(*)>=3
        ORDER BY (SUM(CASE WHEN aa.selected_option=s.correct_option THEN 1 ELSE 0 END)::float/NULLIF(COUNT(*),0)) ASC""", (user['id'],))
        rows = await cur.fetchall()
        if not rows:
            return json_response({'available': False, 'message': 'Complete 3 tests first so we can learn your weak areas.'})
        subjects = []
        for row in rows:
            total = max(1, int(row['total'] or 0))
            accuracy = (int(row['correct'] or 0) / total) * 100
            subjects.append({'subject': row['subject'], 'accuracy': round(accuracy, 1), 'weight': max(0.15, 1-accuracy/100)})
        # Keep at most one subject per 3 questions so the requested count remains exact.
        subjects = sorted(subjects, key=lambda x: x['accuracy'])[:max(1, total_questions // 3)]
        total_weight = sum(s['weight'] for s in subjects) or 1
        raw = [(s['weight']/total_weight)*total_questions for s in subjects]
        allocated = [int(v) for v in raw]
        for i in sorted(range(len(raw)), key=lambda i: raw[i]-allocated[i], reverse=True)[:total_questions-sum(allocated)]:
            allocated[i] += 1
        for item, n in zip(subjects, allocated): item['allocated'] = n
        subjects_sorted = sorted(subjects, key=lambda x: x['accuracy'])
        if not generate:
            return json_response({'available': True, 'preview_only': True, 'mix': subjects_sorted, 'total': total_questions})
        selected_ids = []
        for subject in subjects:
            cur = await conn.execute("SELECT id FROM question_bank WHERE status='ready' AND subject=%s ORDER BY RANDOM() LIMIT %s", (subject['subject'], subject['allocated']))
            selected_ids.extend(int(r['id']) for r in await cur.fetchall())
        if len(selected_ids) < 5:
            return json_response({'error': 'Not enough verified questions in your weak subjects yet. Try again after more banks are added.'}, 503)
        cur = await conn.execute("""INSERT INTO bank_quizzes
            (title,description,subject,created_by_telegram_id,duration_minutes,positive_marks,negative_marks,
             question_time_seconds,shuffle_options,is_custom,custom_owner_telegram_id,is_published,access_type)
            VALUES(%s,%s,%s,%s,%s,1,0.33,60,TRUE,TRUE,%s,TRUE,'free') RETURNING id""",
            (f'Weakness Arsenal · {len(selected_ids)} Q', 'Personalised weighted practice test', 'Arsenal', user['id'], max(30, len(selected_ids)), user['id']))
        qid = int((await cur.fetchone())['id'])
        for pos, question_id in enumerate(selected_ids, 1):
            await conn.execute("INSERT INTO bank_quiz_questions(quiz_id,question_id,position) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (qid, question_id, pos))
    return json_response({'available': True, 'generated': True, 'quiz_id': qid, 'count': len(selected_ids), 'mix': subjects_sorted})

def build_app() -> web.Application:
    app=web.Application(middlewares=[telegram_auth], client_max_size=25*1024*1024)
    app.add_routes([
        web.get('/', index),
        web.get('/membership', membership_page),
        web.get('/health', health),
        web.get('/api/debug/env', debug_env),

        web.get('/api/me', app_me),
        web.post('/api/register', register_user),
        web.get('/api/access/status', access_status),
        web.get('/api/demo/status', demo_status),
        web.get('/api/access', access_alias),
        web.post('/api/access/register-demo', register_demo),

        web.get('/api/leaderboard', leaderboard_data),
        web.get('/api/leaderboard/admin/challenges', leaderboard_admin_challenges),
        web.post('/api/leaderboard/admin/challenges', leaderboard_admin_challenges),
        web.get('/api/leaderboard/admin/attempts', leaderboard_admin_attempts),
        web.get('/api/leaderboard/admin/stats', leaderboard_admin_stats),
        web.post('/api/leaderboard/admin/exclude', leaderboard_admin_exclude),

        web.get('/api/admin/session', admin_session_status),
        web.post('/api/admin/login', admin_login),
        web.post('/api/admin/logout', admin_logout),

        web.get('/api/admin/overview', admin_overview),
        web.get('/api/admin/banks', admin_banks),
        web.post('/api/admin/banks/access', admin_set_bank_access),
        web.post('/api/admin/bank-access', admin_set_bank_access),
        web.get('/api/admin/quizzes', admin_quizzes),
        web.get('/api/admin/series', admin_series_list),
        web.get('/api/admin/series/quizzes', admin_series_quiz_catalog),
        web.post('/api/admin/series/{series_id}/publish', admin_series_publish),
        web.post('/api/admin/series/create', admin_series_create),
        web.post('/api/admin/series/{series_id}/update', admin_series_update),
        web.get('/api/admin/cleanup', admin_cleanup_catalog),
        web.post('/api/admin/test-series/{series_id}/access', admin_set_series_access),
        web.post('/api/admin/quizzes/{quiz_id}/access', admin_set_quiz_access),
        web.post('/api/admin/series/{series_id}/access', admin_set_series_access),
        web.get('/api/admin/cleanup-catalog', admin_cleanup_catalog),
        web.post('/api/admin/remove-subject', admin_remove_subject),
        web.post('/api/admin/remove-quiz/{quiz_id}', admin_remove_quiz),
        web.delete('/api/admin/remove-series/{series_id}', admin_remove_series),

        web.get('/api/admin/demo/settings', admin_overview),
        web.post('/api/admin/demo/settings', admin_demo_settings),
        web.post('/api/admin/demo/grant', admin_demo_grant),
        web.get('/api/admin/demo/registrations', admin_demo_registrations),

        web.get('/api/admin/passes', admin_pass_offers),
        web.post('/api/admin/passes', admin_pass_offer_save),
        web.post('/api/admin/passes/{product_code}/toggle', admin_pass_offer_toggle),
        web.post('/api/admin/coupons', admin_coupon_save),
        web.post('/api/admin/coupons/{code}/toggle', admin_coupon_toggle),
        web.get('/api/admin/payments', admin_payments),
        web.get('/api/admin/payments/{request_id}', admin_payment_detail),
        web.post('/api/admin/payments/{request_id}/review', admin_review_payment),

        web.get('/api/admin/members', admin_members_search_alias),
        web.get('/api/admin/members/search', admin_members_search),
        web.get('/api/admin/members/{telegram_id}', admin_member_detail),
        web.post('/api/admin/members/grant', admin_member_grant),
        web.post('/api/admin/passes/{pass_id}/revoke', admin_pass_revoke),
        web.post('/api/admin/passes/{pass_id}/extend', admin_pass_extend),

        web.get('/api/admin/subjects', admin_subjects_list),
        web.get('/api/admin/import-subjects', admin_import_subjects),
        web.post('/api/admin/import-subjects', admin_create_import_subject),
        web.get('/api/admin/import/subjects', admin_import_subjects),
        web.post('/api/admin/import/subjects', admin_create_import_subject),
        web.post('/api/admin/import/questions', admin_bulk_import),
        web.post('/api/admin/bulk-import', admin_bulk_import),

        web.get('/api/admin/questions/search', admin_questions_search),
        web.get('/api/admin/questions', admin_questions_search),
        web.get('/api/admin/questions/{question_id}', admin_question_detail),
        web.post('/api/admin/questions/{question_id}', admin_question_update),
        web.delete('/api/admin/questions/{question_id}', admin_question_delete),

        web.get('/api/admin/notifications', admin_notifications_list),
        web.post('/api/admin/notifications', admin_notification_create),
        web.delete('/api/admin/notifications/{notification_id}', admin_notification_delete),
        web.get('/api/admin/promo', admin_get_promo),
        web.post('/api/admin/promo', admin_set_promo),
        web.get('/api/admin/banner', admin_get_banner_flat),
        web.post('/api/admin/banner', admin_set_promo),
        web.get('/api/admin/maintenance', admin_get_maintenance),
        web.get('/api/admin/activity', admin_activity),
        web.get('/api/admin/revenue', admin_revenue),
        web.get('/api/admin/export/{kind}', admin_export),
        web.post('/api/admin/quizzes/bulk-delete', admin_bulk_delete_quizzes),
        web.post('/api/admin/maintenance', admin_set_maintenance),

        web.get('/api/notifications', notifications_list),
        web.post('/api/notifications/read-all', notifications_mark_all_read),
        web.post('/api/notifications/{notification_id}/read', notification_mark_read),

        web.get('/api/series', list_series),
        web.get('/api/test-series', test_series_alias),
        web.get('/api/series/{series_id}', series_detail),

        web.get('/api/quizzes', all_quizzes_alias),
        web.get('/api/browse/banks', browse_banks),
        web.get('/api/browse/quizzes', browse_quizzes),
        web.post('/api/quiz/{quiz_id}/start', quiz_start),
        web.get('/api/attempt/{attempt_id}', attempt_state),
        web.post('/api/attempt/{attempt_id}/answer', attempt_answer),
        web.post('/api/attempt/{attempt_id}/submit', attempt_submit),
        web.get('/api/attempt/{attempt_id}/result', attempt_result),
        web.get('/api/attempts', my_attempts),
        web.post('/api/attempts', create_attempt_compat),
        web.get('/api/attempts/{attempt_id}', attempt_state),
        web.post('/api/attempts/{attempt_id}/answer', attempt_answer),
        web.post('/api/attempts/{attempt_id}/review', attempt_review),
        web.post('/api/attempts/{attempt_id}/submit', attempt_submit),
        web.get('/api/quiz-builder/meta', quiz_builder_meta),
        web.post('/api/quiz-builder/create', quiz_builder_create),
        web.get('/api/xray', cutoff_xray),
        web.get('/api/arsenal', weakness_arsenal),
        web.post('/api/arsenal', weakness_arsenal),
        web.get('/api/history', attempt_history_compat),

        web.get('/api/theme', get_theme_preference),
        web.post('/api/theme', save_theme_preference),
        web.get('/api/preferences/theme', get_theme_preference),
        web.put('/api/preferences/theme', save_theme_preference),

        web.get('/api/tasks', list_tasks),
        web.post('/api/tasks', create_task),
        web.patch('/api/tasks/{task_id}', update_task),
        web.delete('/api/tasks/{task_id}', delete_task),

        web.get('/api/revision', list_revision),
        web.post('/api/revision', set_revision_item),
        web.post('/api/revision/resolve', resolve_revision_item),

        web.get('/api/arena', arena_list),
        web.post('/api/arena/create', arena_create),
        web.post('/api/arena/join', arena_join),
        web.get('/api/arena/matches', arena_matches_compat),
        web.post('/api/arena/matches', arena_matches_compat),
        web.get('/api/arena/matches/{match_id}', arena_match_compat),
        web.post('/api/arena/matches/{match_id}/answers', arena_answer_compat),
        web.get('/api/arena/{match_id}', arena_match),
        web.post('/api/arena/{match_id}/answer', arena_answer),

        web.get('/api/payments/options', payment_options),
        web.post('/api/payments/coupon/validate', payment_coupon_validate),
        web.post('/api/payments/submit', submit_payment),
    ])
    static_dir=ROOT/'static'
    if static_dir.is_dir():
        app.router.add_static('/static/', static_dir, name='static')
    return app


async def on_startup(app):
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL is required.')
    app['pool']=AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=8, open=False, kwargs={'row_factory':dict_row})
    await app['pool'].open()
    async with app['pool'].connection() as conn:
        try:
            await qb.ensure_schema(app['pool'])
            await ensure_revision_schema(conn)
            await ensure_access_schema(conn)
            await ensure_test_series_schema(conn)
            await ensure_notifications_schema(conn)
            await payment_schema(conn)
        except Exception:
            log.exception('Schema bootstrap reported errors; continuing startup.')
    log.info('BPSC Nexus Mini App ready on port %s', PORT)


async def on_cleanup(app):
    pool=app.get('pool')
    if pool: await pool.close()


def main():
    app=build_app()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host='0.0.0.0', port=PORT)


if __name__ == '__main__':
    main()
