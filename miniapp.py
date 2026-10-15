"""BPSC Nexus personal CBT Mini App API + static frontend server.

Deploy as a separate Render Web Service from the Telegram bot. Shares DATABASE_URL.
"""
from __future__ import annotations

import hashlib
import base64
import hmac
import json
import logging
import os
import random
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
ADMIN_LOGIN_TELEGRAM_ID = int(os.environ.get("ADMIN_LOGIN_TELEGRAM_ID", "0") or 0)
ADMIN_SESSION_SECRET = os.environ.get("ADMIN_SESSION_SECRET", "")
ADMIN_SESSION_COOKIE = "bpsc_nexus_admin_session"
ADMIN_SESSION_SECONDS = 12 * 60 * 60
_ADMIN_LOGIN_FAILURES = {}


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
    # A valid password session is authoritative for admin routes, even when the
    # Mini App was opened by a non-admin Telegram account. Other APIs still use
    # Telegram's signed initData.
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
    """Create an HttpOnly admin session using Render-configured credentials."""
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
    if not page.is_file() and (ROOT / "membership.html").is_file():
        page = ROOT / "membership.html"
    if not page.is_file(): raise web.HTTPNotFound()
    response = web.FileResponse(page)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

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
    if not page.is_file() and (ROOT / "index.html").is_file():
        page = ROOT / "index.html"
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
    await conn.execute("ALTER TABLE bpsc_nexus_test_series ADD COLUMN IF NOT EXISTS access_type VARCHAR(12) NOT NULL DEFAULT 'paid'")
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


async def ensure_access_schema(conn):
    """Additive demo and paywall migrations; does not reset existing content."""
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


async def _ensure_trial(conn, telegram_id: int):
    """Start the demo on the first authenticated Mini App visit, once per account."""
    await conn.execute("""INSERT INTO bpsc_nexus_demo_trials(telegram_id,started_at,expires_at)
        SELECT %s,NOW(),NOW() + (duration_hours * INTERVAL '1 hour')
        FROM bpsc_nexus_demo_config WHERE singleton=TRUE AND enabled=TRUE
        ON CONFLICT(telegram_id) DO NOTHING""", (telegram_id,))
    # Return both the row and whether it was created during this request.
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


async def _has_subject_entitlement(conn, telegram_id: int, subject: str) -> bool:
    """Check an active pass for this exact bank subject or an all-access pass."""
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
        trial_before=await conn.execute("SELECT 1 FROM bpsc_nexus_demo_trials WHERE telegram_id=%s",(user['id'],))
        existed=bool(await trial_before.fetchone())
        state=await _access_state(conn,user['id'],is_admin,True)
    state['trial_started_now']=bool(state['demo_started_at'] and not existed and state['demo_active'])
    state['demo_message_title']=f"Your {state['demo_duration_hours']}-hour BPSC Nexus Demo is live" if state['demo_active'] else ('Your demo has ended' if state['demo_expired'] else 'Explore BPSC Nexus')
    return json_response(state)


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
    """Return subject/test/series records for safe cleanup in the Mini App."""
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
                return json_response({'error':'This subject still has questions, quizzes, historical attempts or test-series references. Archive it instead; no data was deleted.','ready_questions':int(counts['ready_count'] or 0),'total_questions':int(counts['total_count'] or 0),'quizzes':quiz_count,'history_records':snapshot_count,'series':series_count},409)
            cur=await conn.execute("DELETE FROM question_bank_subjects WHERE LOWER(BTRIM(name))=LOWER(BTRIM(%s)) RETURNING name",(name,)); row=await cur.fetchone()
            if not row:return json_response({'error':'Empty subject record not found.'},404)
            await _write_admin_audit(conn,admin['id'],'subject_delete',row['name'],{'questions':0})
            return json_response({'name':row['name'],'action':'delete','success':True})
        # Archiving is reversible and never deletes questions or attempts.
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
    result_message=''
    async with request.app['pool'].connection() as conn:
        await payment_schema(conn)
        async with conn.transaction():
            cur=await conn.execute('SELECT * FROM bpsc_payment_requests WHERE id=%s FOR UPDATE',(rid,)); r=await cur.fetchone()
            if not r: return json_response({'error':'Payment request not found.'},404)
            if r['status']!='pending': return json_response({'error':f"This request was already {r['status']}."},409)
            if action=='reject':
                await conn.execute("UPDATE bpsc_payment_requests SET status='rejected',reviewed_at=NOW(),reviewed_by=%s,review_note=%s WHERE id=%s",(admin['id'],note or 'Rejected in Mini App Admin Console',rid))
                await _write_admin_audit(conn,admin['id'],'payment_reject',str(rid),{'telegram_id':r['telegram_id'],'product_code':r['product_code'],'note':note})
                result_message=f"Your BPSC Nexus payment request #{rid} for {r['product_name']} was rejected. Please contact an admin if you believe this is an error."
                pass_id=None
            else:
                cur=await conn.execute('SELECT id FROM app_users WHERE telegram_id=%s',(r['telegram_id'],)); app_user=await cur.fetchone()
                if not app_user: return json_response({'error':'The student is not registered yet. Ask them to open the Mini App once, then retry.'},409)
                product_code=str(r['product_code'] or 'all_access')
                if product_code=='all_access':
                    cur=await conn.execute("SELECT id FROM pass_catalogue WHERE pass_code='all_access' AND is_active=TRUE"); cat=await cur.fetchone()
                    if not cat: return json_response({'error':'All-Access pass is missing or inactive in pass_catalogue.'},500)
                    cur=await conn.execute("SELECT MAX(expires_at) AS expiry FROM premium_passes WHERE user_id=%s AND pass_code='all_access' AND revoked_at IS NULL AND expires_at>NOW()",(app_user['id'],)); expiry=(await cur.fetchone())['expiry']
                    cur=await conn.execute("""INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,granted_at,expires_at,note,pass_code,vault_keys)
                        VALUES(%s,%s,%s,COALESCE(%s,NOW()),COALESCE(%s,NOW())+(%s*INTERVAL '1 day'),%s,'all_access',ARRAY['*']::TEXT[]) RETURNING id""",
                        (app_user['id'],cat['id'],admin['id'],expiry,expiry,int(r['duration_days']),f"Mini App UPI payment #{rid}; ₹{r['amount_inr']}"))
                    pass_id=int((await cur.fetchone())['id'])
                    result_message=f"✅ Your BPSC Nexus payment is approved. All Access for {r['duration_days']} days is now active."
                else:
                    cur=await conn.execute("SELECT product_code,product_name,entitlement_key,duration_days FROM bpsc_individual_products WHERE product_code=%s",(product_code,)); product=await cur.fetchone()
                    if not product: return json_response({'error':'Purchased product no longer exists in the product catalogue; no access was granted.'},409)
                    await conn.execute("INSERT INTO pass_catalogue(pass_code,pass_name,description,is_active) VALUES(%s,%s,%s,TRUE) ON CONFLICT(pass_code) DO UPDATE SET pass_name=EXCLUDED.pass_name,is_active=TRUE",(product['product_code'],product['product_name'],f"Individual entitlement: {product['entitlement_key']}"))
                    cur=await conn.execute("SELECT id FROM pass_catalogue WHERE pass_code=%s AND is_active=TRUE",(product['product_code'],)); cat=await cur.fetchone()
                    # Lifetime individual products create a dedicated pass for that product only.
                    cur=await conn.execute("""INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,expires_at,note,pass_code,vault_keys)
                        VALUES(%s,%s,%s,NULL,%s,%s,ARRAY[%s]::TEXT[]) RETURNING id""",
                        (app_user['id'],cat['id'],admin['id'],f"Mini App UPI payment #{rid}; ₹{r['amount_inr']}",product['product_code'],product['entitlement_key']))
                    pass_id=int((await cur.fetchone())['id'])
                    result_message=f"✅ Your BPSC Nexus payment is approved. {product['product_name']} access is now active."
                await conn.execute("UPDATE bpsc_payment_requests SET status='approved',reviewed_at=NOW(),reviewed_by=%s,review_note=%s,activated_pass_id=%s WHERE id=%s",(admin['id'],note or 'Approved in Mini App Admin Console',pass_id,rid))
                await _write_admin_audit(conn,admin['id'],'payment_approve',str(rid),{'telegram_id':r['telegram_id'],'product_code':product_code,'pass_id':pass_id,'note':note})
    if action=='reject': result_message=f"Your BPSC Nexus payment request #{rid} for {r['product_name']} was rejected. Please contact an admin if you believe this is an error."
    await _notify_telegram_user(int(r['telegram_id']),result_message)
    return json_response({'request_id':rid,'status':'approved' if action=='approve' else 'rejected','activated_pass_id':pass_id if action=='approve' else None,'updated':True})


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
                expires_sql="COALESCE(%s,NOW())+(%s*INTERVAL '1 day')" if days else 'NULL'
                if days:
                    cur=await conn.execute(f"INSERT INTO premium_passes(user_id,pass_id,granted_by_telegram_id,expires_at,note,pass_code,vault_keys) VALUES(%s,%s,%s,{expires_sql},%s,'all_access',ARRAY['*']::TEXT[]) RETURNING id",(member['id'],cat['id'],admin['id'],expiry,days,note))
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
        if not row: return json_response({'error':'Pass is revoked, permanent, or not found. Revoke/grant a replacement if needed.'},404)
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
            cur=await conn.execute("SELECT id FROM question_bank WHERE question_hash=%s AND id<>%s",(digest,qid))
            if await cur.fetchone(): return json_response({'error':'A question with the same content/answer hash already exists. No changes were saved.'},409)
            cur=await conn.execute("UPDATE question_bank SET question_en=%s,question_hi=%s,options=%s::jsonb,correct_option=%s,explanation_en=%s,explanation_hi=%s,subject=%s,topic=%s,source=%s,source_url=%s,attribution=%s,status=%s,question_hash=%s,updated_at=NOW() WHERE id=%s RETURNING id",(q_en,q_hi or None,json.dumps(normalized,ensure_ascii=False),correct,explanation_en or None,explanation_hi or None,subject,topic or None,source or None,source_url or None,attribution or None,status,digest,qid))
            row=await cur.fetchone()
            if not row: return json_response({'error':'Question not found.'},404)
            await _write_admin_audit(conn,admin['id'],'question_update',str(qid),{'subject':subject,'status':status})
    return json_response({'updated':True,'question_id':qid,'status':status})

async def admin_question_archive(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try: qid=int(request.match_info['question_id'])
    except ValueError: return json_response({'error':'Invalid question ID.'},400)
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("UPDATE question_bank SET status='archived',updated_at=NOW() WHERE id=%s AND status<>'archived' RETURNING id",(qid,)); row=await cur.fetchone()
        if not row: return json_response({'error':'Question not found or already archived.'},404)
        await _write_admin_audit(conn,admin['id'],'question_archive',str(qid),{})
    return json_response({'archived':True,'question_id':qid})

async def admin_question_delete(request):
    admin=request['telegram_user']
    if not _is_series_admin(admin): return json_response({'error':'Admin access required.'},403)
    try: qid=int(request.match_info['question_id']); body=await request.json(); confirm=body.get('confirm') is True
    except Exception: return json_response({'error':'Invalid delete request.'},400)
    if not confirm: return json_response({'error':'Explicit confirmation is required.'},400)
    async with request.app['pool'].connection() as conn:
        async with conn.transaction():
            cur=await conn.execute('SELECT id,status FROM question_bank WHERE id=%s FOR UPDATE',(qid,)); row=await cur.fetchone()
            if not row: return json_response({'error':'Question not found.'},404)
            await conn.execute('DELETE FROM bank_quiz_questions WHERE question_id=%s',(qid,))
            await conn.execute('DELETE FROM bank_quiz_attempt_answers WHERE question_id=%s',(qid,))
            await conn.execute('DELETE FROM bpsc_nexus_revision_items WHERE question_id=%s',(qid,))
            await conn.execute('DELETE FROM bpsc_nexus_arena_answers a USING bpsc_nexus_arena_questions q WHERE a.match_id=q.match_id AND a.position=q.position AND q.question_id=%s',(qid,))
            await conn.execute('DELETE FROM bpsc_nexus_arena_questions WHERE question_id=%s',(qid,))
            cur=await conn.execute('DELETE FROM question_bank WHERE id=%s RETURNING id',(qid,)); deleted=await cur.fetchone()
            await _write_admin_audit(conn,admin['id'],'question_delete',str(qid),{'previous_status':row['status']})
    return json_response({'deleted':bool(deleted),'question_id':qid})

async def admin_series(request):
    user=request['telegram_user']
    if not _is_series_admin(user): return json_response({'error':'Admin access required.'},403)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        cur=await conn.execute("""SELECT s.id,s.title,s.exam_type,s.series_type,s.subject,s.access_type,s.is_published,
            COUNT(st.quiz_id) AS test_count FROM bpsc_nexus_test_series s
            LEFT JOIN bpsc_nexus_test_series_tests st ON st.series_id=s.id
            GROUP BY s.id ORDER BY s.id DESC LIMIT 200""")
        rows=[dict(r) for r in await cur.fetchall()]
    return json_response({'series':rows})


async def list_test_series(request):
    user_id = request['telegram_user']['id']
    exam = request.query.get('exam', '').upper()
    kind = request.query.get('type', '')
    clauses = ["s.is_published=TRUE", "EXISTS (SELECT 1 FROM bpsc_nexus_test_series_tests est JOIN bank_quizzes eq ON eq.id=est.quiz_id JOIN bank_quiz_questions eqq ON eqq.quiz_id=eq.id JOIN question_bank eqb ON eqb.id=eqq.question_id WHERE est.series_id=s.id AND eq.is_published=TRUE AND COALESCE(eq.admin_hidden,FALSE)=FALSE AND eqb.status='ready')"]
    params = [user_id]
    if exam in ('UPSC', 'BPSC'):
        clauses.append('s.exam_type=%s'); params.append(exam)
    if kind in ('full_length', 'sectional', 'subject_wise'):
        clauses.append('s.series_type=%s'); params.append(kind)
    async with request.app['pool'].connection() as conn:
        await ensure_access_schema(conn)
        access_state=await _access_state(conn,user_id,user_id in ADMIN_TELEGRAM_IDS,True)
        cur = await conn.execute(f"""SELECT s.id,s.title,s.description,s.exam_type,s.series_type,s.subject,s.difficulty,s.access_type,
            COUNT(DISTINCT st.quiz_id) FILTER (WHERE q0.is_published=TRUE AND COALESCE(q0.admin_hidden,FALSE)=FALSE AND EXISTS (SELECT 1 FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id WHERE qq.quiz_id=q0.id AND qb.status='ready')) AS test_count,
            BOOL_OR(e.telegram_id IS NOT NULL AND e.revoked_at IS NULL AND (e.expires_at IS NULL OR e.expires_at>NOW())) AS entitled
            FROM bpsc_nexus_test_series s
            LEFT JOIN bpsc_nexus_test_series_tests st ON st.series_id=s.id
            LEFT JOIN bank_quizzes q0 ON q0.id=st.quiz_id
            LEFT JOIN bpsc_nexus_test_series_entitlements e ON e.series_id=s.id AND e.telegram_id=%s
            WHERE {' AND '.join(clauses)}
            GROUP BY s.id ORDER BY s.sort_order,s.id DESC LIMIT 200""", tuple(params))
        series_rows = await cur.fetchall()
        out=[]
        for s in series_rows:
            entitled = s['access_type']=='free' or bool(s['entitled']) or bool(access_state['has_paid_access'])
            tc = await conn.execute("""SELECT q.id,q.title,q.description,q.subject,q.duration_minutes,q.positive_marks,q.negative_marks,
                COUNT(qq.question_id) AS total_questions, st.position
                FROM bpsc_nexus_test_series_tests st JOIN bank_quizzes q ON q.id=st.quiz_id
                LEFT JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
                LEFT JOIN question_bank qb ON qb.id=qq.question_id AND qb.status='ready'
                WHERE st.series_id=%s AND q.is_published=TRUE AND COALESCE(q.admin_hidden,FALSE)=FALSE
                GROUP BY q.id,st.position HAVING COUNT(qb.id) FILTER (WHERE qb.status='ready')>0 ORDER BY st.position,q.id""", (s['id'],))
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
    is_admin = user_id in ADMIN_TELEGRAM_IDS
    async with pool.connection() as conn:
        await ensure_access_schema(conn)
        access = await _access_state(conn,user_id,is_admin,True)
        cur = await conn.execute("""SELECT q.id,q.title,q.description,q.subject,q.duration_minutes,q.positive_marks,q.negative_marks,
            q.question_time_seconds,q.shuffle_options,q.is_custom,q.access_type,COUNT(qq.question_id) AS total_questions,
            BOOL_OR(COALESCE(s.access_type,'free')='paid') AS bank_paid,
            ARRAY_REMOVE(ARRAY_AGG(DISTINCT qb.subject) FILTER (WHERE COALESCE(s.access_type,'free')='paid'),NULL) AS paid_subjects,
            EXISTS(SELECT 1 FROM bpsc_nexus_test_series_tests st JOIN bpsc_nexus_test_series_entitlements e ON e.series_id=st.series_id
                WHERE st.quiz_id=q.id AND e.telegram_id=%s AND e.revoked_at IS NULL AND (e.expires_at IS NULL OR e.expires_at>NOW())) AS series_entitled
            FROM bank_quizzes q
            JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
            JOIN question_bank qb ON qb.id=qq.question_id AND qb.status='ready'
            LEFT JOIN question_bank_subjects s ON LOWER(BTRIM(s.name))=LOWER(BTRIM(qb.subject)) AND s.archived_at IS NULL
            WHERE q.is_published=TRUE AND COALESCE(q.admin_hidden,FALSE)=FALSE AND (q.is_custom=FALSE OR q.custom_owner_telegram_id=%s)
            GROUP BY q.id ORDER BY q.is_custom ASC,q.id DESC LIMIT 100""", (user_id,user_id))
        rows = await cur.fetchall()
        entitled_by_quiz={}
        for row in rows:
            mode=str(row['access_type'] or 'inherit')
            effective='paid' if mode=='paid' or (mode=='inherit' and bool(row['bank_paid'])) else 'free'
            subjects=list(row.get('paid_subjects') or [])
            if effective!='paid' or access['is_admin'] or access['demo_active'] or access['has_all_access_pass'] or bool(row['series_entitled']):
                entitled_by_quiz[int(row['id'])]=True
            else:
                # A product pass unlocks only its matching subject, not unrelated paid banks.
                entitlement_subjects=subjects or [str(row.get('subject') or '')]
                entitled_by_quiz[int(row['id'])]=bool(entitlement_subjects) and all([await _has_subject_entitlement(conn,user_id,subj) for subj in entitlement_subjects])
    quizzes=[]
    for r in rows:
        mode=str(r['access_type'] or 'inherit')
        effective='paid' if mode=='paid' or (mode=='inherit' and bool(r['bank_paid'])) else 'free'
        locked=effective=='paid' and not entitled_by_quiz.get(int(r['id']),False)
        quizzes.append({"id":int(r["id"]),"title":r["title"],"description":r["description"],"subject":r["subject"],
            "duration_minutes":int(r["duration_minutes"]),"positive_marks":float(r["positive_marks"]),"negative_marks":float(r["negative_marks"]),
            "question_time_seconds":int(r["question_time_seconds"] or 0),"shuffle_options":True,"shuffle_questions":True,"is_custom":bool(r["is_custom"]),
            "access_type":mode,"effective_access":effective,"locked":locked,"total_questions":int(r["total_questions"]),
            "demo_active":bool(access['demo_active']),"demo_expires_at":access['demo_expires_at']})
    return json_response({"quizzes":quizzes,"access":access})


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
        cur = await conn.execute("""SELECT qb.subject,COUNT(*) AS n FROM question_bank qb WHERE qb.status='ready'
            AND NOT EXISTS (SELECT 1 FROM question_bank_subjects s WHERE LOWER(BTRIM(s.name))=LOWER(BTRIM(qb.subject)) AND s.archived_at IS NOT NULL)
            GROUP BY qb.subject ORDER BY qb.subject""")
        subjects = [{"name": r["subject"], "count": int(r["n"])} for r in await cur.fetchall()]
        cur = await conn.execute("""SELECT DISTINCT qb.topic FROM question_bank qb WHERE qb.status='ready' AND NULLIF(BTRIM(qb.topic),'') IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM question_bank_subjects s WHERE LOWER(BTRIM(s.name))=LOWER(BTRIM(qb.subject)) AND s.archived_at IS NOT NULL)
            ORDER BY qb.topic LIMIT 500""")
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
        await ensure_access_schema(conn)
        access_state=await _access_state(conn,user['id'],user['id'] in ADMIN_TELEGRAM_IDS,True)
        cur = await conn.execute("SELECT id,title,duration_minutes,question_time_seconds,shuffle_options,is_custom,custom_owner_telegram_id,access_type FROM bank_quizzes WHERE id=%s AND is_published=TRUE AND (is_custom=FALSE OR custom_owner_telegram_id=%s)", (quiz_id, user["id"]))
        quiz = await cur.fetchone()
        if not quiz:
            return json_response({"error": "Quiz is unavailable."}, 404)
        cur = await conn.execute("""SELECT COUNT(*) FILTER (WHERE COALESCE(s.access_type,'free')='paid') AS paid_banks
            FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
            LEFT JOIN question_bank_subjects s ON LOWER(BTRIM(s.name))=LOWER(BTRIM(qb.subject)) AND s.archived_at IS NULL
            WHERE qq.quiz_id=%s AND qb.status='ready'""",(quiz_id,))
        bank_gate=bool((await cur.fetchone())['paid_banks'])
        quiz_access=str(quiz['access_type'] or 'inherit')
        quiz_paid=quiz_access=='paid' or (quiz_access=='inherit' and bank_gate)
        series_entitled=False
        cur=await conn.execute("""SELECT EXISTS(SELECT 1 FROM bpsc_nexus_test_series_tests st
            JOIN bpsc_nexus_test_series_entitlements e ON e.series_id=st.series_id
            WHERE st.quiz_id=%s AND e.telegram_id=%s AND e.revoked_at IS NULL
              AND (e.expires_at IS NULL OR e.expires_at>NOW())) AS ok""",(quiz_id,user['id']))
        series_entitled=bool((await cur.fetchone())['ok'])
        if quiz_paid and not (access_state['is_admin'] or access_state['demo_active'] or access_state['has_all_access_pass'] or series_entitled):
            cur=await conn.execute("""SELECT DISTINCT qb.subject FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
                LEFT JOIN question_bank_subjects s ON LOWER(BTRIM(s.name))=LOWER(BTRIM(qb.subject)) AND s.archived_at IS NULL
                WHERE qq.quiz_id=%s AND qb.status='ready' AND (COALESCE(s.access_type,'free')='paid' OR %s='paid')""",(quiz_id,quiz_access))
            paid_subjects=[r['subject'] for r in await cur.fetchall()]
            subject_access=bool(paid_subjects) and all([await _has_subject_entitlement(conn,user['id'],subject) for subject in paid_subjects])
            if not subject_access:
                return json_response({"error":"This question bank is locked. Your demo may have ended; activate the matching subject pass or All Access to continue.","code":"PAID_ACCESS_REQUIRED","demo_expires_at":access_state['demo_expires_at']},403)
        cur = await conn.execute("""SELECT s.id,s.access_type FROM bpsc_nexus_test_series_tests st
            JOIN bpsc_nexus_test_series s ON s.id=st.series_id
            WHERE st.quiz_id=%s AND s.is_published=TRUE AND s.access_type='paid'
            ORDER BY s.id LIMIT 1""", (quiz_id,))
        paid_series = await cur.fetchone()
        if paid_series and quiz_access!='free' and not (access_state['has_paid_access'] or series_entitled):
            return json_response({"error": "This test belongs to a paid series. Access is not active for your account yet."}, 403)
        cur = await conn.execute("""SELECT qb.id FROM bank_quiz_questions qq JOIN question_bank qb ON qb.id=qq.question_id
            WHERE qq.quiz_id=%s AND qb.status='ready' ORDER BY qq.position""", (quiz_id,))
        question_ids = [int(r['id']) for r in await cur.fetchall()]
        if not question_ids:
            return json_response({"error": "This quiz has no verified questions."}, 400)
        random.SystemRandom().shuffle(question_ids)
        cur = await conn.execute("INSERT INTO bank_quiz_attempts(quiz_id,telegram_id,mode,total_questions,question_time_seconds,quiz_settings) VALUES(%s,%s,%s,%s,%s,%s::jsonb) RETURNING id,started_at", (quiz_id, user["id"], mode, len(question_ids), int(quiz["question_time_seconds"] or 0), json.dumps({"custom": bool(quiz["is_custom"]), "mode_label": requested_mode, "shuffle_questions": True, "shuffle_options": True})))
        attempt = await cur.fetchone()
        async with conn.cursor() as snapshot_cur:
            for position, question_id in enumerate(question_ids, start=1):
                src_cur=await conn.execute("SELECT id,subject,question_en,question_hi,question_type,question_structure,question_flags,options,correct_option,explanation_en,explanation_hi FROM question_bank WHERE id=%s AND status='ready'",(question_id,))
                qrow=await src_cur.fetchone()
                if not qrow: continue
                opts=qrow['options'] if isinstance(qrow['options'],dict) else json.loads(qrow['options'] or '{}')
                original=list(opts.items())
                # Keep a dedicated E=Not Attempted sentinel in place; shuffle actual answer options.
                sentinel=None
                if 'E' in opts and (str(opts['E']).strip().lower().startswith('not attempted') or str(opts['E']).strip().startswith('प्रयास नहीं किया')):
                    sentinel=('E',opts['E']); original=[item for item in original if item[0]!='E']
                random.SystemRandom().shuffle(original)
                remapped={}
                correct_new=None
                for idx,(old_key,value) in enumerate(original):
                    new_key='ABCDE'[idx]
                    remapped[new_key]=value
                    if old_key==qrow['correct_option']: correct_new=new_key
                if sentinel:
                    remapped['E']=sentinel[1]
                elif 'E' in opts and len(original)<5:
                    # Preserve any non-sentinel fifth option if present.
                    pass
                if correct_new is None and sentinel and qrow['correct_option']=='E': correct_new='E'
                await snapshot_cur.execute("""INSERT INTO bank_quiz_attempt_question_snapshots(attempt_id,position,question_id,subject,question_en,question_hi,question_type,question_structure,question_flags,options,correct_option,explanation_en,explanation_hi)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
                    (attempt['id'],position,qrow['id'],qrow['subject'],qrow['question_en'],qrow['question_hi'],qrow['question_type'],json.dumps(qrow['question_structure'] or {}),json.dumps(qrow['question_flags'] or {}),json.dumps(remapped,ensure_ascii=False),correct_new or qrow['correct_option'],qrow['explanation_en'],qrow['explanation_hi']))
    return json_response({"attempt_id": int(attempt["id"]), "quiz_id": int(quiz_id), "started_at": attempt["started_at"].isoformat(), "duration_minutes": int(quiz["duration_minutes"]), "question_time_seconds": int(quiz["question_time_seconds"] or 0), "shuffle_options": False, "shuffle_questions": True, "server_shuffled": True, "mode": mode, "mode_label": requested_mode})


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
                          "duration_minutes": int(attempt["duration_minutes"]), "question_time_seconds": int(attempt["question_time_seconds"] or 0), "shuffle_options": False, "server_shuffled": True, "started_at": attempt["started_at"].isoformat(), "deadline": datetime.fromtimestamp(deadline, timezone.utc).isoformat() if deadline is not None else None,
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


async def intel_articles(request):
    user=request["telegram_user"]
    limit=min(50,max(1,int(request.query.get("limit","20"))))
    offset=max(0,int(request.query.get("offset","0")))
    search=(request.query.get("q") or "").strip()[:120]
    async with request.app["pool"].connection() as conn:
        cur=await conn.execute("""SELECT a.id,a.title,a.canonical_url,a.published_at,a.discovered_at,s.name AS source_name,
          v.summary,v.background,v.key_facts,v.significance,v.mains_points,v.bihar_relevance,v.language
          FROM intel_articles a JOIN intel_sources s ON s.id=a.source_id
          JOIN LATERAL (SELECT * FROM intel_content_versions WHERE article_id=a.id ORDER BY version_number DESC LIMIT 1) v ON TRUE
          WHERE a.review_status='approved' AND a.processing_status='drafted'
          AND (%s='' OR a.title ILIKE %s OR v.summary ILIKE %s)
          ORDER BY COALESCE(a.published_at,a.discovered_at) DESC LIMIT %s OFFSET %s""",
          (search,'%'+search+'%','%'+search+'%',limit,offset))
        rows=await cur.fetchall()
    return json_response({"articles":rows,"limit":limit,"offset":offset})

async def intel_article_detail(request):
    aid=int(request.match_info['article_id']); user=request['telegram_user']
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("""SELECT a.id,a.title,a.canonical_url,a.published_at,a.discovered_at,s.name AS source_name,
          v.summary,v.background,v.key_facts,v.significance,v.mains_points,v.bihar_relevance,v.mcqs,v.language
          FROM intel_articles a JOIN intel_sources s ON s.id=a.source_id
          JOIN LATERAL (SELECT * FROM intel_content_versions WHERE article_id=a.id ORDER BY version_number DESC LIMIT 1) v ON TRUE
          WHERE a.id=%s AND a.review_status='approved' AND a.processing_status='drafted'""",(aid,))
        row=await cur.fetchone()
        if not row: return json_response({"error":"Article not found."},404)
        await conn.execute("INSERT INTO intel_read_status(telegram_id,article_id) VALUES(%s,%s) ON CONFLICT(telegram_id,article_id) DO UPDATE SET read_at=NOW()",(user['id'],aid))
        cur=await conn.execute("SELECT EXISTS(SELECT 1 FROM intel_bookmarks WHERE telegram_id=%s AND article_id=%s) AS saved",(user['id'],aid)); saved=(await cur.fetchone())['saved']
    return json_response({"article":row,"saved":saved})

async def intel_bookmark(request):
    user=request['telegram_user']; data=await request.json(); aid=int(data.get('article_id',0)); saved=bool(data.get('saved',True))
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("SELECT 1 FROM intel_articles WHERE id=%s AND review_status='approved'",(aid,))
        if not await cur.fetchone(): return json_response({"error":"Article not found."},404)
        if saved: await conn.execute("INSERT INTO intel_bookmarks(telegram_id,article_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(user['id'],aid))
        else: await conn.execute("DELETE FROM intel_bookmarks WHERE telegram_id=%s AND article_id=%s",(user['id'],aid))
    return json_response({"saved":saved})

async def intel_bookmarks(request):
    user=request['telegram_user']
    async with request.app['pool'].connection() as conn:
        cur=await conn.execute("""SELECT a.id,a.title,a.canonical_url,a.published_at,s.name AS source_name,v.summary
          FROM intel_bookmarks b JOIN intel_articles a ON a.id=b.article_id JOIN intel_sources s ON s.id=a.source_id
          JOIN LATERAL(SELECT summary FROM intel_content_versions WHERE article_id=a.id ORDER BY version_number DESC LIMIT 1)v ON TRUE
          WHERE b.telegram_id=%s AND a.review_status='approved' ORDER BY b.created_at DESC LIMIT 100""",(user['id'],))
        rows=await cur.fetchall()
    return json_response({"articles":rows})

async def startup(app):
    if not BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError("Set BOT_TOKEN and DATABASE_URL in the Render environment.")
    pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, open=False, kwargs={"row_factory": dict_row, "connect_timeout": 10})
    await pool.open(); await pool.wait()
    await qb.ensure_schema(pool)
    await lb.ensure_schema(pool)
    await lb.backfill_bank_attempts(pool)
    await intel.ensure_schema(pool)
    async with pool.connection() as conn:
        await payment_schema(conn)
        await ensure_test_series_schema(conn)
        await ensure_access_schema(conn)
        await conn.execute("""CREATE TABLE IF NOT EXISTS bpsc_nexus_admin_audit (
            id BIGSERIAL PRIMARY KEY, admin_telegram_id BIGINT NOT NULL, action TEXT NOT NULL,
            target TEXT NOT NULL, details JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
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
    app = web.Application(middlewares=[telegram_auth], client_max_size=22 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/membership", membership_page)
    app.router.add_get("/api/payments", payment_options)
    app.router.add_post("/api/payments", submit_payment)
    app.router.add_get("/static/{filename}", static_asset)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/intel/articles", intel_articles)
    app.router.add_get("/api/intel/articles/{article_id}", intel_article_detail)
    app.router.add_get("/api/intel/bookmarks", intel_bookmarks)
    app.router.add_post("/api/intel/bookmarks", intel_bookmark)
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
    app.router.add_post("/api/admin/login", admin_login)
    app.router.add_post("/api/admin/logout", admin_logout)
    app.router.add_get("/api/admin/session", admin_session_status)
    app.router.add_get("/api/leaderboard", leaderboard_data)
    app.router.add_get("/api/leaderboard/admin/challenges", leaderboard_admin_challenges)
    app.router.add_post("/api/leaderboard/admin/challenges", leaderboard_admin_challenges)
    app.router.add_get("/api/leaderboard/admin/attempts", leaderboard_admin_attempts)
    app.router.add_get("/api/leaderboard/admin/stats", leaderboard_admin_stats)
    app.router.add_post("/api/leaderboard/admin/exclude", leaderboard_admin_exclude)
    app.router.add_get("/api/admin/payments", admin_payments)
    app.router.add_get("/api/admin/payments/{request_id}", admin_payment_detail)
    app.router.add_post("/api/admin/payments/{request_id}/review", admin_review_payment)
    app.router.add_get("/api/admin/members", admin_members_search)
    app.router.add_get("/api/admin/members/{telegram_id}", admin_member_detail)
    app.router.add_post("/api/admin/members/grant", admin_member_grant)
    app.router.add_post("/api/admin/passes/{pass_id}/revoke", admin_pass_revoke)
    app.router.add_post("/api/admin/passes/{pass_id}/extend", admin_pass_extend)
    app.router.add_get("/api/admin/questions", admin_questions_search)
    app.router.add_get("/api/admin/questions/{question_id}", admin_question_detail)
    app.router.add_put("/api/admin/questions/{question_id}", admin_question_update)
    app.router.add_post("/api/admin/questions/{question_id}/archive", admin_question_archive)
    app.router.add_post("/api/admin/questions/{question_id}/delete", admin_question_delete)
    app.router.add_get("/api/admin/import/subjects", admin_import_subjects)
    app.router.add_post("/api/admin/import/subjects", admin_create_import_subject)
    app.router.add_post("/api/admin/import/questions", admin_bulk_import)
    app.router.add_get("/api/test-series", list_test_series)
    app.router.add_post("/api/admin/test-series", admin_create_test_series)
    app.router.add_post("/api/admin/test-series/{series_id}/tests", admin_link_series_test)
    app.router.add_post("/api/admin/test-series/{series_id}/publish", admin_publish_test_series)
    app.router.add_post("/api/admin/test-series/{series_id}/entitlement", admin_series_entitlement)
    app.router.add_get("/api/access", access_status)
    app.router.add_get("/api/admin/overview", admin_overview)
    app.router.add_get("/api/admin/banks", admin_banks)
    app.router.add_post("/api/admin/banks/access", admin_set_bank_access)
    app.router.add_get("/api/admin/quizzes", admin_quizzes)
    app.router.add_get("/api/admin/series", admin_series)
    app.router.add_get("/api/admin/cleanup", admin_cleanup_catalog)
    app.router.add_post("/api/admin/subjects/remove", admin_remove_subject)
    app.router.add_post("/api/admin/quizzes/{quiz_id}/remove", admin_remove_quiz)
    app.router.add_post("/api/admin/test-series/{series_id}/remove", admin_remove_series)
    app.router.add_post("/api/admin/quizzes/{quiz_id}/access", admin_set_quiz_access)
    app.router.add_post("/api/admin/test-series/{series_id}/access", admin_set_series_access)
    app.router.add_post("/api/admin/demo/settings", admin_demo_settings)
    app.router.add_post("/api/admin/demo/grant", admin_demo_grant)
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
