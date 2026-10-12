"""Verified, idempotent BPSC Nexus global leaderboard ingestion and summaries.

Ranking points are normalized to a 100-point scale per completed assessment:
score / maximum positive marks * 100. This avoids directly comparing tests of
very different lengths. Raw marks/counts remain stored separately.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

log = logging.getLogger("bpsc_nexus_leaderboard")

DDL = [
    """CREATE TABLE IF NOT EXISTS bpsc_global_leaderboard_attempts (
        id BIGSERIAL PRIMARY KEY,
        source_key TEXT NOT NULL UNIQUE,
        source_type TEXT NOT NULL,
        telegram_id BIGINT NOT NULL,
        display_name TEXT NOT NULL DEFAULT 'Student',
        quiz_id BIGINT,
        quiz_title TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT 'Other / Unclassified',
        started_at TIMESTAMPTZ NOT NULL,
        submitted_at TIMESTAMPTZ NOT NULL,
        correct_count INTEGER NOT NULL DEFAULT 0,
        incorrect_count INTEGER NOT NULL DEFAULT 0,
        skipped_count INTEGER NOT NULL DEFAULT 0,
        score NUMERIC(12,3) NOT NULL,
        max_score NUMERIC(12,3) NOT NULL,
        accuracy NUMERIC(7,3) NOT NULL DEFAULT 0,
        time_taken_seconds INTEGER NOT NULL DEFAULT 0,
        normalized_points NUMERIC(12,4) NOT NULL DEFAULT 0,
        validation_status TEXT NOT NULL DEFAULT 'verified' CHECK (validation_status IN ('verified','invalid','review')),
        eligible BOOLEAN NOT NULL DEFAULT TRUE,
        exclusion_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_bpsc_lb_attempts_period ON bpsc_global_leaderboard_attempts(eligible, validation_status, submitted_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_bpsc_lb_attempts_user ON bpsc_global_leaderboard_attempts(telegram_id, submitted_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_bpsc_lb_attempts_subject ON bpsc_global_leaderboard_attempts(subject, submitted_at DESC)",
    """CREATE TABLE IF NOT EXISTS bpsc_leaderboard_period_totals (
        period_type TEXT NOT NULL CHECK (period_type IN ('weekly','monthly','lifetime')),
        period_key TEXT NOT NULL,
        subject TEXT NOT NULL,
        telegram_id BIGINT NOT NULL,
        display_name TEXT NOT NULL DEFAULT 'Student',
        normalized_points NUMERIC(16,4) NOT NULL DEFAULT 0,
        correct_count BIGINT NOT NULL DEFAULT 0,
        incorrect_count BIGINT NOT NULL DEFAULT 0,
        skipped_count BIGINT NOT NULL DEFAULT 0,
        total_time_seconds BIGINT NOT NULL DEFAULT 0,
        total_questions_count BIGINT NOT NULL DEFAULT 0,
        attempts_count BIGINT NOT NULL DEFAULT 0,
        first_submitted_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(period_type, period_key, subject, telegram_id)
    )""",
    "ALTER TABLE bpsc_leaderboard_period_totals ADD COLUMN IF NOT EXISTS total_questions_count BIGINT NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_bpsc_lb_totals_rank ON bpsc_leaderboard_period_totals(period_type, period_key, subject, normalized_points DESC)",
    """CREATE TABLE IF NOT EXISTS bpsc_leaderboard_daily_challenges (
        id BIGSERIAL PRIMARY KEY,
        quiz_id BIGINT NOT NULL REFERENCES bank_quizzes(id) ON DELETE RESTRICT,
        challenge_date DATE NOT NULL UNIQUE,
        title TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_by_telegram_id BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS bpsc_leaderboard_audit (
        id BIGSERIAL PRIMARY KEY,
        attempt_id BIGINT NOT NULL REFERENCES bpsc_global_leaderboard_attempts(id) ON DELETE RESTRICT,
        admin_telegram_id BIGINT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
]

async def ensure_schema(pool):
    async with pool.connection() as conn:
        for stmt in DDL:
            await conn.execute(stmt)


def _period_keys(ts: datetime):
    ts = ts.astimezone(timezone.utc)
    iso = ts.isocalendar()
    return f"{iso.year}-W{iso.week:02d}", ts.strftime("%Y-%m")

async def _add_totals(conn, record):
    week_key, month_key = _period_keys(record["submitted_at"])
    subject = (record.get("subject") or "Other / Unclassified").strip() or "Other / Unclassified"
    points = record["normalized_points"]
    # Each source attempt contributes once to All Subjects and once to its tagged subject.
    for period_type, period_key in (("weekly", week_key), ("monthly", month_key), ("lifetime", "all")):
        for category in dict.fromkeys(("All subjects", subject)):
            await conn.execute("""INSERT INTO bpsc_leaderboard_period_totals
                (period_type,period_key,subject,telegram_id,display_name,normalized_points,correct_count,incorrect_count,skipped_count,total_time_seconds,total_questions_count,attempts_count,first_submitted_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s)
                ON CONFLICT(period_type,period_key,subject,telegram_id) DO UPDATE SET
                  display_name=CASE WHEN bpsc_leaderboard_period_totals.display_name='Student' THEN EXCLUDED.display_name ELSE bpsc_leaderboard_period_totals.display_name END,
                  normalized_points=bpsc_leaderboard_period_totals.normalized_points+EXCLUDED.normalized_points,
                  correct_count=bpsc_leaderboard_period_totals.correct_count+EXCLUDED.correct_count,
                  incorrect_count=bpsc_leaderboard_period_totals.incorrect_count+EXCLUDED.incorrect_count,
                  skipped_count=bpsc_leaderboard_period_totals.skipped_count+EXCLUDED.skipped_count,
                  total_time_seconds=bpsc_leaderboard_period_totals.total_time_seconds+EXCLUDED.total_time_seconds,
                  total_questions_count=bpsc_leaderboard_period_totals.total_questions_count+EXCLUDED.total_questions_count,
                  attempts_count=bpsc_leaderboard_period_totals.attempts_count+1,
                  first_submitted_at=LEAST(bpsc_leaderboard_period_totals.first_submitted_at,EXCLUDED.first_submitted_at),updated_at=NOW()""",
                (period_type, period_key, category, record["telegram_id"], record["display_name"], points,
                 record["correct_count"], record["incorrect_count"], record["skipped_count"], record["time_taken_seconds"], record["correct_count"]+record["incorrect_count"]+record["skipped_count"], record["submitted_at"]))

async def record_verified_attempt(conn, *, source_key, source_type, telegram_id, display_name, quiz_id, quiz_title,
                                  subject, started_at, submitted_at, correct_count, incorrect_count, skipped_count,
                                  score, max_score):
    """Insert one immutable source submission and update aggregates only on first insert."""
    if not submitted_at or not started_at or max_score is None or float(max_score) <= 0:
        return False
    if submitted_at < started_at or int(correct_count) < 0 or int(incorrect_count) < 0 or int(skipped_count) < 0:
        log.warning("Rejected malformed leaderboard attempt source=%s", source_key)
        return False
    attempted = int(correct_count) + int(incorrect_count)
    accuracy = (int(correct_count) / attempted * 100) if attempted else 0.0
    duration = max(0, min(7 * 24 * 3600, int((submitted_at - started_at).total_seconds())))
    max_score = float(max_score); score = float(score)
    points = score / max_score * 100.0
    record = {"source_key":str(source_key),"source_type":str(source_type),"telegram_id":int(telegram_id),
      "display_name":(" ".join(str(display_name or "Student").split()))[:80] or "Student", "quiz_id":int(quiz_id) if quiz_id else None,
      "quiz_title":(" ".join(str(quiz_title or "Quiz").split()))[:200] or "Quiz", "subject":(" ".join(str(subject or "Other / Unclassified").split()))[:120] or "Other / Unclassified",
      "started_at":started_at,"submitted_at":submitted_at,"correct_count":int(correct_count),"incorrect_count":int(incorrect_count),"skipped_count":int(skipped_count),
      "score":round(score,3),"max_score":round(max_score,3),"accuracy":round(accuracy,3),"time_taken_seconds":duration,"normalized_points":round(points,4)}
    needs_review = score > max_score + 0.01
    cur = await conn.execute("""INSERT INTO bpsc_global_leaderboard_attempts
      (source_key,source_type,telegram_id,display_name,quiz_id,quiz_title,subject,started_at,submitted_at,correct_count,incorrect_count,skipped_count,score,max_score,accuracy,time_taken_seconds,normalized_points,validation_status,eligible,exclusion_reason)
      VALUES(%(source_key)s,%(source_type)s,%(telegram_id)s,%(display_name)s,%(quiz_id)s,%(quiz_title)s,%(subject)s,%(started_at)s,%(submitted_at)s,%(correct_count)s,%(incorrect_count)s,%(skipped_count)s,%(score)s,%(max_score)s,%(accuracy)s,%(time_taken_seconds)s,%(normalized_points)s,%(validation_status)s,%(eligible)s,%(exclusion_reason)s)
      ON CONFLICT(source_key) DO NOTHING RETURNING id""", {**record,"validation_status":"review" if needs_review else "verified","eligible":not needs_review,"exclusion_reason":"Score exceeds maximum possible marks; administrator review required." if needs_review else None})
    if not await cur.fetchone():
        return False
    if not needs_review:
        await _add_totals(conn, record)
    else:
        log.warning("Leaderboard submission flagged for review: %s", source_key)
    return True

async def record_bank_attempt(conn, attempt_id: int, telegram_id: int, display_name: str = "Student"):
    cur = await conn.execute("""SELECT a.id,a.telegram_id,a.started_at,a.submitted_at,a.total_questions,a.correct_count,a.incorrect_count,a.skipped_count,a.score,
        q.id AS quiz_id,q.title,q.subject,q.positive_marks
        FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id
        WHERE a.id=%s AND a.telegram_id=%s AND a.submitted_at IS NOT NULL""", (attempt_id, telegram_id))
    row = await cur.fetchone()
    if not row: return False
    return await record_verified_attempt(conn, source_key=f"bank_attempt:{row['id']}", source_type="bank_quiz", telegram_id=row["telegram_id"],
      display_name=display_name, quiz_id=row["quiz_id"], quiz_title=row["title"], subject=row["subject"], started_at=row["started_at"], submitted_at=row["submitted_at"],
      correct_count=row["correct_count"] or 0, incorrect_count=row["incorrect_count"] or 0, skipped_count=row["skipped_count"] or 0,
      score=row["score"] or 0, max_score=int(row["total_questions"] or 0)*float(row["positive_marks"] or 0))

async def record_group_session(conn, session_id: int):
    cur = await conn.execute("""SELECT s.id,s.created_at,s.completed_at,s.questions_count,s.quiz_id,s.quiz_title,q.subject,
        a.telegram_id,MAX(a.first_name) AS display_name,
        COUNT(*) FILTER (WHERE a.selected_letter=q2.correct_option AND NOT (a.selected_letter='E' AND (COALESCE(q2.options->>'E','') ILIKE 'Not attempted%' OR COALESCE(q2.options->>'E','') LIKE 'प्रयास नहीं किया%'))) AS correct,
        COUNT(*) FILTER (WHERE a.selected_letter IS NOT NULL AND a.selected_letter<>q2.correct_option AND NOT (a.selected_letter='E' AND (COALESCE(q2.options->>'E','') ILIKE 'Not attempted%' OR COALESCE(q2.options->>'E','') LIKE 'प्रयास नहीं किया%'))) AS incorrect,
        COUNT(*) FILTER (WHERE a.selected_letter IS NOT NULL AND NOT (a.selected_letter='E' AND (COALESCE(q2.options->>'E','') ILIKE 'Not attempted%' OR COALESCE(q2.options->>'E','') LIKE 'प्रयास नहीं किया%'))) AS attempted
      FROM group_quiz_sessions s JOIN group_quiz_answers a ON a.session_id=s.id
      JOIN group_quiz_session_questions q2 ON q2.session_id=a.session_id AND q2.position=a.position
      LEFT JOIN bank_quizzes q ON q.id=s.quiz_id
      WHERE s.id=%s AND s.status='completed' AND s.completed_at IS NOT NULL
      GROUP BY s.id,s.created_at,s.completed_at,s.questions_count,s.quiz_id,s.quiz_title,q.subject,a.telegram_id""", (session_id,))
    rows = await cur.fetchall(); inserted = 0
    for r in rows:
        correct=int(r["correct"] or 0); incorrect=int(r["incorrect"] or 0); skipped=max(0,int(r["questions_count"] or 0)-correct-incorrect)
        ok = await record_verified_attempt(conn, source_key=f"group_session:{r['id']}:user:{r['telegram_id']}", source_type="group_quiz",
          telegram_id=r["telegram_id"], display_name=r["display_name"] or "Student", quiz_id=r["quiz_id"], quiz_title=r["quiz_title"],
          subject=r["subject"] or "Other / Unclassified", started_at=r["created_at"], submitted_at=r["completed_at"], correct_count=correct,
          incorrect_count=incorrect, skipped_count=skipped, score=correct-incorrect/3, max_score=max(1,int(r["questions_count"] or 0)))
        inserted += int(bool(ok))
    return inserted

async def rebuild_summaries(conn):
    """Rebuild aggregates after an audited exclusion/correction; only called by admins."""
    await conn.execute("DELETE FROM bpsc_leaderboard_period_totals")
    cur=await conn.execute("SELECT * FROM bpsc_global_leaderboard_attempts WHERE eligible=TRUE AND validation_status='verified' ORDER BY submitted_at,id")
    for r in await cur.fetchall():
        record=dict(r)
        await _add_totals(conn, record)

async def _claim_backfill(conn, name: str) -> bool:
    await conn.execute("CREATE TABLE IF NOT EXISTS bpsc_leaderboard_migrations (name TEXT PRIMARY KEY, completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
    cur=await conn.execute("SELECT 1 FROM bpsc_leaderboard_migrations WHERE name=%s",(name,))
    return not bool(await cur.fetchone())

async def backfill_bank_attempts(pool):
    """One-time idempotent backfill of completed server-scored bank/CBT attempts."""
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT pg_try_advisory_lock(hashtext('bpsc_nexus_lb_bank_backfill')) AS locked")
        if not (await cur.fetchone())["locked"]: return
        try:
            if not await _claim_backfill(conn,"bank_attempts_v1"): return
            while True:
                cur=await conn.execute("""SELECT a.id,a.telegram_id FROM bank_quiz_attempts a JOIN bank_quizzes q ON q.id=a.quiz_id
                  WHERE a.submitted_at IS NOT NULL AND a.started_at<=a.submitted_at AND a.total_questions>0 AND q.positive_marks>0
                    AND a.correct_count>=0 AND a.incorrect_count>=0 AND a.skipped_count>=0
                  AND NOT EXISTS (SELECT 1 FROM bpsc_global_leaderboard_attempts l WHERE l.source_key='bank_attempt:'||a.id::text)
                  ORDER BY a.id LIMIT 250""")
                rows=await cur.fetchall()
                if not rows: break
                for r in rows: await record_bank_attempt(conn,int(r["id"]),int(r["telegram_id"]),"Student")
            await conn.execute("INSERT INTO bpsc_leaderboard_migrations(name) VALUES('bank_attempts_v1') ON CONFLICT(name) DO NOTHING")
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext('bpsc_nexus_lb_bank_backfill'))")

async def backfill_group_sessions(pool):
    """One-time idempotent backfill from completed group sessions and stored answer keys."""
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT pg_try_advisory_lock(hashtext('bpsc_nexus_lb_group_backfill')) AS locked")
        if not (await cur.fetchone())["locked"]: return
        try:
            if not await _claim_backfill(conn,"group_sessions_v1"): return
            cur=await conn.execute("SELECT id FROM group_quiz_sessions WHERE status='completed' AND completed_at IS NOT NULL ORDER BY id")
            ids=[int(r["id"]) for r in await cur.fetchall()]
            for sid in ids: await record_group_session(conn,sid)
            await conn.execute("INSERT INTO bpsc_leaderboard_migrations(name) VALUES('group_sessions_v1') ON CONFLICT(name) DO NOTHING")
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext('bpsc_nexus_lb_group_backfill'))")
