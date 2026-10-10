"""Reusable question bank, safe bulk import, quiz builder and Telegram quiz sessions.

This module deliberately does not use AI for imports. HTML/JSON/CSV are parsed locally.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import html
import io
import json
import logging
import random
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

log = logging.getLogger("bpsc_nexus_question_bank")
MAX_QUESTIONS_PER_IMPORT = 20000
MAX_FILE_BYTES = 10 * 1024 * 1024
LETTERS = "ABCDE"
SUBJECTS = ["General Science", "Bihar Special", "Modern Indian History", "Indian Polity", "Geography", "Economy", "Ancient & Medieval History", "Art & Culture", "Other / Unclassified"]

DDL = [
    """CREATE TABLE IF NOT EXISTS question_import_batches (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_telegram_id BIGINT NOT NULL,
        file_name TEXT NOT NULL,
        file_type TEXT NOT NULL,
        selected_subject TEXT,
        total_detected INTEGER NOT NULL DEFAULT 0,
        imported_count INTEGER NOT NULL DEFAULT 0,
        duplicate_count INTEGER NOT NULL DEFAULT 0,
        invalid_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'completed',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS question_bank (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        question_en TEXT NOT NULL,
        question_hi TEXT,
        options JSONB NOT NULL DEFAULT '{}'::jsonb,
        correct_option TEXT,
        explanation_en TEXT,
        explanation_hi TEXT,
        subject TEXT NOT NULL DEFAULT 'Other / Unclassified',
        topic TEXT,
        source TEXT,
        source_url TEXT,
        attribution TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        question_hash TEXT NOT NULL,
        import_batch_id BIGINT REFERENCES question_import_batches(id) ON DELETE SET NULL,
        created_by_telegram_id BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT question_bank_status_check CHECK (status IN ('ready','pending','archived')),
        CONSTRAINT question_bank_correct_check CHECK (correct_option IS NULL OR correct_option IN ('A','B','C','D','E'))
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_question_bank_hash ON question_bank(question_hash)",
    "CREATE INDEX IF NOT EXISTS idx_question_bank_subject_status ON question_bank(subject, status, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_question_bank_batch ON question_bank(import_batch_id)",
    """CREATE TABLE IF NOT EXISTS bank_quizzes (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        subject TEXT NOT NULL,
        created_by_telegram_id BIGINT NOT NULL,
        is_published BOOLEAN NOT NULL DEFAULT FALSE,
        duration_minutes INTEGER NOT NULL DEFAULT 60,
        positive_marks NUMERIC(8,6) NOT NULL DEFAULT 1,
        negative_marks NUMERIC(8,6) NOT NULL DEFAULT 0.333333,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        published_at TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS bank_quiz_questions (
        quiz_id BIGINT NOT NULL REFERENCES bank_quizzes(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        question_id BIGINT NOT NULL REFERENCES question_bank(id) ON DELETE RESTRICT,
        PRIMARY KEY (quiz_id, position),
        UNIQUE (quiz_id, question_id)
    )""",
    """CREATE TABLE IF NOT EXISTS bank_quiz_attempts (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        quiz_id BIGINT NOT NULL REFERENCES bank_quizzes(id) ON DELETE RESTRICT,
        telegram_id BIGINT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('practice','exam')),
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        submitted_at TIMESTAMPTZ,
        total_questions INTEGER NOT NULL,
        correct_count INTEGER NOT NULL DEFAULT 0,
        incorrect_count INTEGER NOT NULL DEFAULT 0,
        skipped_count INTEGER NOT NULL DEFAULT 0,
        score NUMERIC(10,2) NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS bank_quiz_attempt_answers (
        attempt_id BIGINT NOT NULL REFERENCES bank_quiz_attempts(id) ON DELETE CASCADE,
        question_id BIGINT NOT NULL REFERENCES question_bank(id) ON DELETE RESTRICT,
        selected_option TEXT,
        is_correct BOOLEAN,
        is_marked_review BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (attempt_id, question_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_bank_quizzes_published ON bank_quizzes(is_published, id DESC)",
    "ALTER TABLE bank_quiz_attempt_answers ADD COLUMN IF NOT EXISTS is_marked_review BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE bank_quizzes ADD COLUMN IF NOT EXISTS is_custom BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE bank_quizzes ADD COLUMN IF NOT EXISTS custom_owner_telegram_id BIGINT",
    "ALTER TABLE bank_quizzes ADD COLUMN IF NOT EXISTS question_time_seconds INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE bank_quizzes ADD COLUMN IF NOT EXISTS shuffle_options BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE bank_quiz_attempts ADD COLUMN IF NOT EXISTS question_time_seconds INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE bank_quiz_attempts ADD COLUMN IF NOT EXISTS quiz_settings JSONB NOT NULL DEFAULT '{}'::jsonb",
    "CREATE INDEX IF NOT EXISTS idx_bank_attempts_user ON bank_quiz_attempts(telegram_id, started_at DESC)",
    """CREATE TABLE IF NOT EXISTS bank_quiz_attempt_question_snapshots (
        attempt_id BIGINT NOT NULL REFERENCES bank_quiz_attempts(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        question_id BIGINT NOT NULL,
        subject TEXT NOT NULL DEFAULT 'Other / Unclassified',
        question_en TEXT NOT NULL,
        question_hi TEXT,
        options JSONB NOT NULL,
        correct_option TEXT NOT NULL,
        explanation_en TEXT,
        explanation_hi TEXT,
        PRIMARY KEY(attempt_id, question_id),
        UNIQUE(attempt_id, position)
    )""",
    "ALTER TABLE bank_quiz_attempt_question_snapshots ADD COLUMN IF NOT EXISTS subject TEXT NOT NULL DEFAULT 'Other / Unclassified'",
    """INSERT INTO bank_quiz_attempt_question_snapshots(attempt_id,position,question_id,subject,question_en,question_hi,options,correct_option,explanation_en,explanation_hi)
        SELECT a.id,qq.position,qb.id,qb.subject,qb.question_en,qb.question_hi,qb.options,qb.correct_option,qb.explanation_en,qb.explanation_hi
        FROM bank_quiz_attempts a JOIN bank_quiz_questions qq ON qq.quiz_id=a.quiz_id
        JOIN question_bank qb ON qb.id=qq.question_id
        LEFT JOIN bank_quiz_attempt_question_snapshots s ON s.attempt_id=a.id AND s.question_id=qb.id
        WHERE s.question_id IS NULL AND qb.correct_option IS NOT NULL ON CONFLICT DO NOTHING""",
]


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _get(d: dict, *names: str, default=None):
    lower = {str(k).strip().lower().replace(" ", "_"): v for k, v in d.items()}
    for name in names:
        key = name.lower().replace(" ", "_")
        if key in lower and lower[key] not in (None, ""):
            return lower[key]
    return default


def _option_map(value: Any, row: dict) -> dict[str, str]:
    options: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            key = str(k).strip().upper().replace("OPTION_", "")
            key = key[-1:] if key else ""
            if key in LETTERS and _norm(v): options[key] = _norm(v)
    elif isinstance(value, list):
        for i, v in enumerate(value[:5]):
            if isinstance(v, dict):
                val = _get(v, "text", "value", "option", default="")
                key = str(_get(v, "key", "label", "letter", default=LETTERS[i])).upper()[:1]
            else:
                val = v
                key = LETTERS[i]
            if key in LETTERS and _norm(val): options[key] = _norm(val)
    for letter in LETTERS:
        val = _get(row, letter, f"option_{letter}", f"option{letter}", f"choice_{letter}", f"choice{letter}")
        if val is not None and _norm(val): options[letter] = _norm(val)
    return options


def _answer_letter(answer: Any, options: dict[str, str]) -> str | None:
    if answer is None: return None
    s = _norm(answer).upper()
    s = re.sub(r"^(?:(?:CORRECT\s+)?ANSWER|ANS)\s*[:\-]\s*", "", s)
    m = re.match(r"^(?:OPTION\s*)?\(?([A-E])\)?[.):-]?\s*$", s)
    if m and m.group(1) in options: return m.group(1)
    if s.isdigit():
        n = int(s)
        # Common source formats use either 0-based or 1-based indices.
        if 0 <= n < len(options) and n == 0: return list(options)[n]
        if 1 <= n <= len(options): return list(options)[n-1]
    for k, v in options.items():
        if s == _norm(v).upper(): return k
    return None


def _normalize_record(record: Any, default_subject: str, source_name: str, owner_id: int) -> dict | None:
    if not isinstance(record, dict): return None
    q_en = _norm(_get(record, "question_en", "question", "question_text", "text", "q", "question_english", "english_question"))
    q_hi = _norm(_get(record, "question_hi", "question_hindi", "hindi_question", "question_hin", "प्रश्न"))
    options_raw = _get(record, "options", "choices", "answers", default={})
    options = _option_map(options_raw, record)
    if not q_en and q_hi:
        q_en = q_hi
    if not q_en: return None
    # At least two non-empty options are needed for a usable MCQ.
    options = {k: v for k, v in options.items() if _norm(v)}
    ans = _get(record, "correct_option", "correct_answer", "answer", "answer_key", "correct", "key", "right_answer", "correctanswer")
    correct = _answer_letter(ans, options)
    explanation_en = _norm(_get(record, "explanation_en", "explanation", "solution", "reason", "explanation_english"))
    explanation_hi = _norm(_get(record, "explanation_hi", "hindi_explanation", "explanation_hindi", "व्याख्या"))
    subject = _norm(_get(record, "subject", "category", "subject_name", default=default_subject)) or default_subject
    topic = _norm(_get(record, "topic", "chapter", "unit")) or None
    source = _norm(_get(record, "source", "book", "exam", "source_name", default=source_name)) or source_name
    source_url = _norm(_get(record, "source_url", "url", "link")) or None
    attribution = _norm(_get(record, "attribution", "author", "credit", "publisher")) or None
    canonical = _norm(q_en).casefold() + "|" + "|".join(_norm(options.get(k, "")).casefold() for k in LETTERS)
    qhash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    status = "ready" if len(options) >= 2 and correct in options else "pending"
    return {"question_en": q_en, "question_hi": q_hi or None, "options": options, "correct_option": correct,
            "explanation_en": explanation_en or None, "explanation_hi": explanation_hi or None,
            "subject": subject, "topic": topic, "source": source, "source_url": source_url,
            "attribution": attribution, "status": status, "question_hash": qhash, "created_by_telegram_id": owner_id}


def _extract_json_records(obj: Any) -> list:
    if isinstance(obj, list): return obj
    if isinstance(obj, dict):
        for key in ("questions", "items", "data", "results", "mcqs", "records"):
            val = obj.get(key)
            if isinstance(val, list): return val
        # One question object is also accepted.
        if any(k in {str(x).lower() for x in obj} for k in ("question", "question_en", "question_text", "q")):
            return [obj]
    raise ValueError("JSON must contain a question array or a single question object.")


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts = []
    def handle_data(self, data):
        if data.strip(): self.parts.append(data.strip())


def _html_to_records(raw: str) -> list[dict]:
    # First accept embedded JSON commonly used by quiz pages.
    for pattern in (r"(?:const|let|var)\s+(?:questions|quizData|questionData)\s*=\s*(\[.*\])\s*;", r"<script[^>]*type=['\"]application/json['\"][^>]*>(.*?)</script>"):
        match = re.search(pattern, raw, flags=re.I | re.S)
        if match:
            try:
                candidate = json.loads(html.unescape(match.group(1)))
                return _extract_json_records(candidate)
            except Exception:
                pass
    # Parse common question-card markup without depending on one CSS framework.
    from html.parser import HTMLParser as _HP
    class Parser(_HP):
        def __init__(self):
            super().__init__(convert_charrefs=True); self.stack=[]; self.nodes=[]; self.current=None
        def handle_starttag(self, tag, attrs):
            attrs=dict(attrs); cls=attrs.get("class", ""); ident=attrs.get("id", "")
            marker=(cls+" "+ident).lower()
            if any(x in marker for x in ("question-card", "question-item", "mcq", "quiz-question")):
                self.current={"question":"", "options":{}, "_fields":{}, "_active":None}; self.nodes.append(self.current)
            if self.current is not None:
                field=None
                if any(x in marker for x in ("question-text", "question-title", "question-statement")): field="question"
                elif re.search(r"(?:^|\s)(?:option|choice)[-_ ]?([a-e])(?:\s|$)", marker): field="option:"+re.search(r"(?:^|\s)(?:option|choice)[-_ ]?([a-e])(?:\s|$)", marker).group(1).upper()
                elif any(x in marker for x in ("explanation", "solution")): field="explanation"
                elif "correct" in marker or "answer-key" in marker: field="correct"
                if field: self.current["_active"]=field
                if tag in ("input",):
                    if attrs.get("checked") is not None and attrs.get("value"): self.current["correct"]=attrs.get("value")
                if tag == "br": self.current["_text"]=(self.current.get("_text", "")+" ")
        def handle_data(self, data):
            if self.current is None: return
            field=self.current.get("_active")
            if field:
                if field.startswith("option:"):
                    k=field.split(":",1)[1]; self.current["options"][k]=self.current["options"].get(k,"")+data
                elif field=="question": self.current["question"]+=data
                else: self.current[field]=self.current.get(field,"")+data
        def handle_endtag(self, tag):
            if self.current is not None and tag in ("div", "li", "article", "section"):
                # Keep card open unless a nested marked card is explicitly encountered.
                pass
    p=Parser(); p.feed(raw)
    records=[]
    for n in p.nodes:
        if _norm(n.get("question")) and len(n.get("options",{}))>=2:
            records.append({"question":_norm(n["question"]), "options":{k:_norm(v) for k,v in n["options"].items()}, "correct_answer":_norm(n.get("correct")), "explanation":_norm(n.get("explanation"))})
    if records: return records
    # Last-resort text extraction for a clear plain-text MCQ layout in HTML.
    extractor=_TextExtractor(); extractor.feed(raw); text="\n".join(extractor.parts)
    blocks=re.split(r"(?=\b(?:Q(?:uestion)?\s*)?\d+[.)])", text, flags=re.I)
    for block in blocks:
        lines=[_norm(x) for x in block.splitlines() if _norm(x)]
        if not lines: continue
        rec={"question":lines[0], "options":{}}
        for line in lines[1:]:
            m=re.match(r"^\(?([A-E])\)?[.)\-:]\s*(.+)$", line)
            if m: rec["options"][m.group(1)]=m.group(2)
            a=re.search(r"(?:correct answer|answer key|answer)\s*[:\-]\s*([A-E])\b", line, re.I)
            if a: rec["correct_answer"]=a.group(1)
        if len(rec["options"])>=2: records.append(rec)
    if not records: raise ValueError("No recognizable MCQs were found in this HTML. Export as JSON or use the CSV template for predictable imports.")
    return records


def parse_file(filename: str, raw: bytes, subject: str, owner_id: int) -> tuple[list[dict], str]:
    if len(raw) > MAX_FILE_BYTES: raise ValueError("File exceeds the 10 MB import limit.")
    ext=filename.rsplit(".",1)[-1].lower() if "." in filename else ""
    try: text=raw.decode("utf-8-sig")
    except UnicodeDecodeError: text=raw.decode("utf-8", errors="replace")
    if ext == "json": records=_extract_json_records(json.loads(text))
    elif ext == "csv":
        try: records=list(csv.DictReader(io.StringIO(text)))
        except Exception as e: raise ValueError(f"Could not read CSV: {e}")
        if not records: raise ValueError("CSV is empty or has no header row.")
    elif ext in ("html", "htm"): records=_html_to_records(text)
    else: raise ValueError("Supported file formats are .html, .htm, .json and .csv.")
    if len(records) > MAX_QUESTIONS_PER_IMPORT: raise ValueError(f"Import is limited to {MAX_QUESTIONS_PER_IMPORT:,} questions per file.")
    normalized=[]; invalid=0
    for record in records:
        item=_normalize_record(record, subject, filename, owner_id)
        if item is None: invalid+=1
        else: normalized.append(item)
    return normalized, f"detected={len(records)}; invalid={invalid}"


async def ensure_schema(pool):
    async with pool.connection() as conn:
        for statement in DDL: await conn.execute(statement)
    log.info("Question bank schema verified.")


async def import_records(pool, records: list[dict], owner_id: int, filename: str, file_type: str, subject: str, invalid_count: int) -> dict:
    if pool is None: raise RuntimeError("Database is unavailable.")
    async with pool.connection() as conn:
        async with conn.transaction():
            cur=await conn.execute("INSERT INTO question_import_batches(owner_telegram_id,file_name,file_type,selected_subject,total_detected,invalid_count) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id", (owner_id,filename,file_type,subject,len(records)+invalid_count,invalid_count))
            batch_id=(await cur.fetchone())["id"]
            imported=duplicates=0
            for item in records:
                # A source's subject overrides the batch default only when explicitly present.
                cur=await conn.execute("""INSERT INTO question_bank(question_en,question_hi,options,correct_option,explanation_en,explanation_hi,subject,topic,source,source_url,attribution,status,question_hash,import_batch_id,created_by_telegram_id)
                    VALUES(%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(question_hash) DO NOTHING RETURNING id""",
                    (item["question_en"],item["question_hi"],json.dumps(item["options"],ensure_ascii=False),item["correct_option"],item["explanation_en"],item["explanation_hi"],item["subject"],item["topic"],item["source"],item["source_url"],item["attribution"],item["status"],item["question_hash"],batch_id,owner_id))
                if await cur.fetchone(): imported+=1
                else: duplicates+=1
            await conn.execute("UPDATE question_import_batches SET imported_count=%s,duplicate_count=%s WHERE id=%s",(imported,duplicates,batch_id))
    return {"batch_id":batch_id,"detected":len(records)+invalid_count,"imported":imported,"duplicates":duplicates,"invalid":invalid_count,"pending":sum(1 for r in records if r["status"]=="pending")}


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Import HTML / JSON / CSV", callback_data="qb:import")],
        [InlineKeyboardButton("🗃 Question Bank", callback_data="qb:browse"), InlineKeyboardButton("🧩 Build Quiz", callback_data="qb:build")],
        [InlineKeyboardButton("🕵️ Verify Pending", callback_data="qb:pending"), InlineKeyboardButton("📦 Import History", callback_data="qb:history")],
        [InlineKeyboardButton("↩ Main Admin Panel", callback_data="adm:home")],
    ])


def subject_keyboard(prefix: str):
    rows=[]
    for i in range(0,len(SUBJECTS),2):
        rows.append([InlineKeyboardButton(SUBJECTS[j],callback_data=f"{prefix}:{j}") for j in range(i,min(i+2,len(SUBJECTS)))])
    rows.append([InlineKeyboardButton("✖ Cancel",callback_data="qb:home")])
    return InlineKeyboardMarkup(rows)


def _admin(update: Update) -> bool:
    u=update.effective_user; c=update.effective_chat
    # IDs are also checked by caller in bot.py before routing callbacks/messages.
    return bool(u and c and c.type == ChatType.PRIVATE)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, pool, is_authorized: bool):
    q=update.callback_query
    if not q: return
    data=q.data or ""
    if not data.startswith("qb:") and not data.startswith("play:"): return
    if data.startswith("play:") and (not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE):
        await q.answer("Please take quizzes in a private chat with the bot.",show_alert=True); return
    if data.startswith("qb:") and not is_authorized:
        await q.answer("Not authorized.",show_alert=True); return
    await q.answer()
    if data == "qb:home":
        context.user_data.pop("qb_flow",None)
        if pool is not None:
            async with pool.connection() as conn:
                cur=await conn.execute("SELECT COUNT(*) FILTER (WHERE status='ready') AS ready, COUNT(*) FILTER (WHERE status='pending') AS pending, COUNT(*) FILTER (WHERE status='archived') AS archived FROM question_bank")
                stats=await cur.fetchone()
                cur=await conn.execute("SELECT COUNT(*) AS n FROM bank_quizzes WHERE is_published=TRUE"); quizzes=(await cur.fetchone())["n"]
            text=f"🗃 Question Bank & Quiz Studio\n\nVerified questions: {stats['ready']}\nPending verification: {stats['pending']}\nArchived: {stats['archived']}\nPublished quizzes: {quizzes}\n\nChoose an action:"
        else: text="🗃 Question Bank & Quiz Studio\nChoose an action:"
        await q.edit_message_text(text,reply_markup=admin_keyboard()); return
    if data == "qb:import":
        context.user_data["qb_flow"]={"step":"import_subject"}
        await q.edit_message_text("Choose the default subject for the next file. Questions with an explicit subject field keep their own subject.",reply_markup=subject_keyboard("qb:importsubject")); return
    if data.startswith("qb:importsubject:"):
        idx=int(data.rsplit(":",1)[1]); context.user_data["qb_flow"]={"step":"await_file","subject":SUBJECTS[idx]}
        await q.edit_message_text(f"Default subject: {SUBJECTS[idx]}\nNow send one .html, .json or .csv file in this private chat. The bot will preview import counts before saving.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data="qb:home")]])); return
    if data == "qb:browse":
        await q.edit_message_text("Send a search phrase, or type *all* to show the latest 15 questions.\n\nSearch covers question text and subject.",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("All recent questions",callback_data="qb:searchall")],[InlineKeyboardButton("⬅ Question Bank menu",callback_data="qb:home")]])); context.user_data["qb_flow"]={"step":"search"}; return
    if data == "qb:searchall":
        context.user_data["qb_flow"]={"step":"search","search":"all"}; await show_search_results(q,pool,"all"); return
    if data == "qb:pending":
        context.user_data["qb_pending_skipped"]=[]
        await show_pending(q,pool); return
    if data.startswith("qb:pendingnext:"):
        skipped=context.user_data.setdefault("qb_pending_skipped",[])
        skipped.append(int(data.rsplit(":",1)[1]))
        await show_pending(q,pool,skipped); return
    if data.startswith("qb:detail:"):
        await show_question_detail(q,pool,int(data.rsplit(":",1)[1])); return
    if data.startswith("qb:edit:"):
        qid=int(data.rsplit(":",1)[1]); context.user_data["qb_flow"]={"step":"edit_question","question_id":qid}
        await q.edit_message_text(f"Edit question #{qid}. Send the complete replacement in this format:\n\nQuestion text\nA. Option A\nB. Option B\nC. Option C\nD. Option D\nAnswer: B\nExplanation: ...\n\nOnly the fields included in this replacement will be updated; the existing subject and source are retained.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data="qb:home")]])); return
    if data.startswith("qb:archive:"):
        qid=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn:
            cur=await conn.execute("UPDATE question_bank SET status='archived',updated_at=NOW() WHERE id=%s AND status<>'archived' RETURNING id",(qid,)); row=await cur.fetchone()
        await q.edit_message_text(f"Question #{qid} {'archived' if row else 'was not changed'}; it remains in the database for audit/history.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return
    if data.startswith("qb:delete:"):
        qid=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn:
            cur=await conn.execute("DELETE FROM question_bank qb WHERE id=%s AND NOT EXISTS(SELECT 1 FROM bank_quiz_questions bqq WHERE bqq.question_id=qb.id) RETURNING id",(qid,)); row=await cur.fetchone()
        await q.edit_message_text(f"Question #{qid} {'permanently deleted' if row else 'is used by a quiz or was not found; archive it instead'}.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Archive question",callback_data=f"qb:archive:{qid}")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return
    if data.startswith("qb:undo:"):
        batch_id=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT id,file_name FROM question_import_batches WHERE id=%s AND owner_telegram_id=%s",(batch_id,q.from_user.id)); batch=await cur.fetchone()
            if not batch:
                await q.edit_message_text("Import batch not found or not owned by you.",reply_markup=admin_keyboard()); return
            cur=await conn.execute("DELETE FROM question_bank qb WHERE import_batch_id=%s AND NOT EXISTS(SELECT 1 FROM bank_quiz_questions bqq WHERE bqq.question_id=qb.id) RETURNING id",(batch_id,)); deleted=len(await cur.fetchall())
            cur=await conn.execute("SELECT COUNT(*) AS n FROM question_bank WHERE import_batch_id=%s",(batch_id,)); remaining=(await cur.fetchone())["n"]
            await conn.execute("UPDATE question_import_batches SET status='rolled_back' WHERE id=%s",(batch_id,))
        await q.edit_message_text(f"Import batch #{batch_id} rollback completed.\nQuestions removed: {deleted}\nQuestions retained because a quiz references them: {remaining}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Import history",callback_data="qb:history")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return
    if data == "qb:history":
        if pool is None: await q.edit_message_text("Database unavailable."); return
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT id,file_name,total_detected,imported_count,duplicate_count,invalid_count,status,created_at FROM question_import_batches WHERE owner_telegram_id=%s ORDER BY id DESC LIMIT 10",(update.effective_user.id,)); rows=await cur.fetchall()
        buttons=[]
        if not rows: text="No imports yet."
        else:
            lines=["📦 Recent imports",""]
            for r in rows:
                lines.append(f"#{r['id']} {r['file_name']}\nDetected {r['total_detected']} · Added {r['imported_count']} · Duplicates {r['duplicate_count']} · Invalid {r['invalid_count']}")
                if r.get('status') != 'rolled_back': buttons.append([InlineKeyboardButton(f"Undo batch #{r['id']}",callback_data=f"qb:undo:{r['id']}")])
            text="\n\n".join(lines)
        buttons.append([InlineKeyboardButton("↩ Question Bank menu",callback_data="qb:home")])
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith("qb:answer:"):
        _,_,qid,letter=data.split(":",3); qid=int(qid)
        async with pool.connection() as conn:
            cur=await conn.execute("UPDATE question_bank SET correct_option=%s,status=CASE WHEN jsonb_object_length(options)>=2 THEN 'ready' ELSE 'pending' END,updated_at=NOW() WHERE id=%s AND status='pending' RETURNING id",(letter,qid)); row=await cur.fetchone()
        await q.edit_message_text(f"Question #{qid} answer {'saved and marked ready' if row else 'was not changed (it may already be reviewed)' }.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Verify next",callback_data="qb:pending")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return
    if data == "qb:build":
        context.user_data["qb_flow"]={"step":"quiz_title"}
        await q.edit_message_text("Send a title for the quiz. The next step lets you choose a subject and question count.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data="qb:home")]])); return
    if data.startswith("qb:quizsubject:"):
        idx=int(data.rsplit(":",1)[1]); flow=context.user_data.get("qb_flow",{}); flow.update({"step":"quiz_count","subject":SUBJECTS[idx]}); context.user_data["qb_flow"]=flow
        await q.edit_message_text(f"Subject: {SUBJECTS[idx]}\nHow many questions? Send a number from 1 to 100.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data="qb:home")]])); return
    if data.startswith("qb:quizpublish:"):
        quiz_id=int(data.rsplit(":",1)[1]); await publish_quiz(q,context,pool,quiz_id); return
    if data.startswith("qb:quizdiscard:"):
        quiz_id=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn: await conn.execute("DELETE FROM bank_quizzes WHERE id=%s AND created_by_telegram_id=%s AND is_published=FALSE",(quiz_id,update.effective_user.id))
        await q.edit_message_text("Draft quiz removed.",reply_markup=admin_keyboard()); return
    if data == "play:list":
        await show_student_quizzes(q,pool); return
    if data.startswith("play:start:"):
        quiz_id=int(data.rsplit(":",1)[1]); await choose_mode(q,context,pool,quiz_id); return
    if data.startswith("play:mode:"):
        _,_,mode,quiz_id=data.split(":",3); await start_attempt(q,context,pool,int(quiz_id),mode); return
    if data.startswith("play:ans:"):
        _,_,letter=data.split(":",2); await answer_question(q,context,pool,letter); return
    if data == "play:skip":
        await answer_question(q,context,pool,None); return
    if data == "play:submit":
        await submit_attempt(q,context,pool); return


async def show_search_results(q,pool,term):
    if pool is None: await q.edit_message_text("Database unavailable."); return
    async with pool.connection() as conn:
        if term=="all": cur=await conn.execute("SELECT id,question_en,subject,status FROM question_bank ORDER BY id DESC LIMIT 15")
        else: cur=await conn.execute("SELECT id,question_en,subject,status FROM question_bank WHERE question_en ILIKE %s OR COALESCE(question_hi,'') ILIKE %s OR subject ILIKE %s ORDER BY id DESC LIMIT 15",(f"%{term}%",f"%{term}%",f"%{term}%"))
        rows=await cur.fetchall()
    lines=["🗃 Question search",""]
    if not rows: lines.append("No matches found.")
    for r in rows: lines.append(f"#{r['id']} [{r['status']}] {r['subject']}\n{r['question_en'][:160]}")
    buttons=[[InlineKeyboardButton(f"Manage question #{r['id']}",callback_data=f"qb:detail:{r['id']}")] for r in rows]
    buttons += [[InlineKeyboardButton("Search again",callback_data="qb:browse")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]]
    await q.edit_message_text("\n\n".join(lines),reply_markup=InlineKeyboardMarkup(buttons))


async def show_question_detail(q,pool,qid):
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT id,question_en,question_hi,options,correct_option,explanation_en,explanation_hi,subject,topic,source,status,import_batch_id FROM question_bank WHERE id=%s",(qid,)); r=await cur.fetchone()
    if not r:
        await q.edit_message_text("Question not found.",reply_markup=admin_keyboard()); return
    opts=r["options"] if isinstance(r["options"],dict) else json.loads(r["options"])
    text=f"Question #{r['id']} · {r['status']}\nSubject: {r['subject']}\nTopic: {r['topic'] or '—'}\nSource: {r['source'] or '—'}\nImport batch: {r['import_batch_id'] or '—'}\n\n{r['question_en']}"
    if r["question_hi"]: text+="\n"+r["question_hi"]
    text+="\n\n"+"\n".join(f"{k}. {v}" for k,v in opts.items())+f"\n\nCorrect answer: {r['correct_option'] or 'Pending'}"
    if r["explanation_en"]: text+="\n\nExplanation: "+r["explanation_en"][:400]
    rows=[[InlineKeyboardButton("✏️ Edit",callback_data=f"qb:edit:{qid}"),InlineKeyboardButton("Archive",callback_data=f"qb:archive:{qid}")],[InlineKeyboardButton("Delete if unused",callback_data=f"qb:delete:{qid}")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]]
    await q.edit_message_text(text[:4000],reply_markup=InlineKeyboardMarkup(rows))


async def show_pending(q,pool,exclude_id=None):
    exclusions = exclude_id if isinstance(exclude_id, list) else ([exclude_id] if exclude_id else [])
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT id,question_en,options FROM question_bank WHERE status='pending' AND NOT (id = ANY(%s)) AND jsonb_object_length(options)>=2 ORDER BY id LIMIT 1",(exclusions,))
        r=await cur.fetchone()
        cur=await conn.execute("SELECT COUNT(*) AS n FROM question_bank WHERE status='pending'"); count=(await cur.fetchone())["n"]
    if not r:
        await q.edit_message_text("No more answer-verifiable questions are in the queue. Some pending entries may lack enough options; correct and re-import those entries or archive them. The Question Bank menu remains available.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return
    opts=r["options"] if isinstance(r["options"],dict) else json.loads(r["options"])
    text=f"🕵️ Pending question #{r['id']} · {count} pending\n\n{r['question_en']}\n\n"+"\n".join(f"{k}. {v}" for k,v in opts.items())+"\n\nChoose the verified correct answer:"
    rows=[]
    letters=list(opts)
    for i in range(0,len(letters),3): rows.append([InlineKeyboardButton(k,callback_data=f"qb:answer:{r['id']}:{k}") for k in letters[i:i+3]])
    rows.append([InlineKeyboardButton("Skip this question",callback_data=f"qb:pendingnext:{r['id']}"),InlineKeyboardButton("Menu",callback_data="qb:home")])
    await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup(rows))


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE, pool, is_authorized: bool):
    message=update.effective_message; user=update.effective_user
    if not message or not user or not is_authorized: return False
    doc=message.document
    if not doc: return False
    flow=context.user_data.get("qb_flow",{})
    if flow.get("step")!="await_file": return False
    filename=doc.file_name or "questions"
    ext=filename.rsplit(".",1)[-1].lower() if "." in filename else ""
    if ext not in ("html","htm","json","csv"):
        await message.reply_text("Unsupported file. Send .html, .htm, .json or .csv. Photo/PDF import is intentionally disabled in this phase."); return True
    if doc.file_size and doc.file_size>MAX_FILE_BYTES:
        await message.reply_text("File exceeds the 10 MB import limit."); return True
    try:
        tgfile=await context.bot.get_file(doc.file_id); raw=bytes(await tgfile.download_as_bytearray())
        default_subject=flow.get("subject",SUBJECTS[-1])
        records, summary=parse_file(filename,raw,default_subject,user.id)
        invalid=int(re.search(r"invalid=(\d+)",summary).group(1))
        if not records:
            await message.reply_text("No importable questions were found. Check the file's columns/schema and try again."); return True
        ready=sum(1 for r in records if r["status"]=="ready"); pending=len(records)-ready
        hashes=[r["question_hash"] for r in records]
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT question_hash FROM question_bank WHERE question_hash = ANY(%s)",(hashes,))
            existing={r["question_hash"] for r in await cur.fetchall()}
        seen=set(); within_duplicates=0
        for h in hashes:
            if h in seen: within_duplicates+=1
            seen.add(h)
        expected_duplicates=len(existing)+within_duplicates
        context.user_data["qb_import_preview"]={"filename":filename,"records":records,"subject":default_subject,"invalid":invalid,"expected_duplicates":expected_duplicates}
        await message.reply_text(f"📋 Import preview\n\nFile: {filename}\nDetected: {len(records)+invalid}\nRecognized: {len(records)}\nReady for scored quizzes: {ready}\nPending answer/options verification: {pending}\nPotential duplicates: {expected_duplicates}\nInvalid rows skipped: {invalid}\n\nNo questions have been saved yet. Confirm to import, or cancel.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm Import",callback_data="qb:confirmimport"),InlineKeyboardButton("✖ Cancel",callback_data="qb:cancelimport")]]))
    except Exception as e:
        log.exception("Question import preview failed")
        await message.reply_text(f"Could not parse this file: {str(e)[:500]}")
    return True


async def confirm_import(q,context,pool,owner_id):
    preview=context.user_data.get("qb_import_preview")
    if not preview: await q.edit_message_text("Import preview expired. Start again.",reply_markup=admin_keyboard()); return
    try:
        records=preview["records"]
        # Count invalid parser rows from preview. It is retained separately from recognized records.
        invalid=int(preview.get("invalid",0))
        result=await import_records(pool,records,owner_id,preview["filename"],preview["filename"].rsplit(".",1)[-1].lower(),preview["subject"],invalid)
        context.user_data.pop("qb_import_preview",None); context.user_data.pop("qb_flow",None)
        await q.edit_message_text("✅ Import complete\n\n"+f"Batch: #{result['batch_id']}\nDetected: {result['detected']}\nAdded: {result['imported']}\nDuplicates skipped: {result['duplicates']}\nInvalid rows: {result['invalid']}\nPending verification: {result['pending']}\n\nQuestions are stored permanently. Pending questions are excluded from scored quizzes.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Verify pending answers",callback_data="qb:pending")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]]))
    except Exception as e:
        log.exception("Question import commit failed")
        await q.edit_message_text(f"Import failed; the database transaction was rolled back.\n\n{str(e)[:400]}",reply_markup=admin_keyboard())


async def receive_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE, pool, is_authorized: bool) -> bool:
    msg=update.effective_message; user=update.effective_user
    if not msg or not msg.text or not user or not is_authorized: return False
    flow=context.user_data.get("qb_flow",{})
    step=flow.get("step")
    if step=="edit_question":
        qid=int(flow["question_id"])
        replacement=parse_forwarded_text(msg.text,user.id)
        if not replacement:
            await msg.reply_text("I couldn't recognize the MCQ. Send the question, options A–D, and (if known) an Answer: B line. No changes were made."); return True
        try:
            async with pool.connection() as conn:
                await conn.execute("UPDATE question_bank SET question_en=%s,options=%s::jsonb,correct_option=%s,explanation_en=%s,status=%s,question_hash=%s,updated_at=NOW() WHERE id=%s",(replacement["question_en"],json.dumps(replacement["options"],ensure_ascii=False),replacement["correct_option"],replacement["explanation_en"],replacement["status"],replacement["question_hash"],qid))
            context.user_data.pop("qb_flow",None)
            await msg.reply_text(f"Question #{qid} updated. Status: {replacement['status']}.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]]))
        except Exception as e:
            log.exception("Question edit failed")
            await msg.reply_text(f"Could not update the question (a duplicate may already exist). No successful change was confirmed. Details: {str(e)[:250]}")
        return True
    if step=="search":
        term=msg.text.strip(); context.user_data["qb_flow"]={"step":"search","search":term}
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT id,question_en,subject,status FROM question_bank WHERE question_en ILIKE %s OR COALESCE(question_hi,'') ILIKE %s OR subject ILIKE %s ORDER BY id DESC LIMIT 15",(f"%{term}%",f"%{term}%",f"%{term}%")); rows=await cur.fetchall()
        lines=["🗃 Question search",""]
        for r in rows: lines.append(f"#{r['id']} [{r['status']}] {r['subject']}\n{r['question_en'][:160]}")
        if not rows: lines.append("No matches found.")
        buttons=[[InlineKeyboardButton(f"Manage question #{r['id']}",callback_data=f"qb:detail:{r['id']}")] for r in rows]
        buttons += [[InlineKeyboardButton("Search again",callback_data="qb:browse")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]]
        await msg.reply_text("\n\n".join(lines),reply_markup=InlineKeyboardMarkup(buttons)); return True
    if step=="quiz_title":
        title=_norm(msg.text)[:160]
        if not title: await msg.reply_text("Send a non-empty quiz title."); return True
        context.user_data["qb_flow"]={"step":"quiz_subject","title":title}
        await msg.reply_text("Choose the subject for this quiz:",reply_markup=subject_keyboard("qb:quizsubject")); return True
    if step=="quiz_count":
        try: n=int(msg.text.strip())
        except ValueError: await msg.reply_text("Send a whole number from 1 to 100."); return True
        if not 1<=n<=100: await msg.reply_text("Question count must be from 1 to 100."); return True
        subject=flow["subject"]
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT id FROM question_bank WHERE status='ready' AND subject=%s ORDER BY RANDOM() LIMIT %s",(subject,n)); rows=await cur.fetchall()
            if len(rows)<n:
                await msg.reply_text(f"Only {len(rows)} verified questions are available in {subject}. Choose a smaller number or import more questions."); return True
            cur=await conn.execute("INSERT INTO bank_quizzes(title,subject,created_by_telegram_id) VALUES(%s,%s,%s) RETURNING id",(flow["title"],subject,user.id)); quiz_id=(await cur.fetchone())["id"]
            for pos,row in enumerate(rows,1): await conn.execute("INSERT INTO bank_quiz_questions(quiz_id,position,question_id) VALUES(%s,%s,%s)",(quiz_id,pos,row["id"]))
        context.user_data.pop("qb_flow",None)
        await msg.reply_text(f"🧩 Draft quiz created\n\nID: #{quiz_id}\nTitle: {flow['title']}\nSubject: {subject}\nQuestions: {n}\nScoring: +1 correct, −1/3 incorrect, 0 skipped\n\nPublish it for students or discard the draft.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Publish for students",callback_data=f"qb:quizpublish:{quiz_id}")],[InlineKeyboardButton("🗑 Discard draft",callback_data=f"qb:quizdiscard:{quiz_id}")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return True
    # Handle forwarded text as a question draft only if no other admin flow is active.
    forwarded=bool(getattr(msg,"forward_origin",None) or getattr(msg,"forward_from",None) or getattr(msg,"forward_from_chat",None))
    if not step:
        record=parse_forwarded_text(msg.text, user.id)
        if record:
            context.user_data["qb_forward_preview"]=record
            await msg.reply_text("Forwarded question recognized. It will be imported only after your confirmation.\n\n"+format_question(record),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Save question",callback_data="qb:saveforward")],[InlineKeyboardButton("Cancel",callback_data="qb:cancelforward")]])); return True
    return False


def parse_forwarded_text(text: str, owner_id: int) -> dict | None:
    lines=[_norm(x) for x in text.splitlines() if _norm(x)]
    if len(lines)<3: return None
    rec={"question":lines[0],"options":{}}
    for line in lines[1:]:
        m=re.match(r"^\(?([A-E])\)?[.)\-:]\s*(.+)$",line)
        if m: rec["options"][m.group(1)]=m.group(2); continue
        m=re.match(r"^(?:answer|correct answer|ans)\s*[:\-]\s*([A-E])\b",line,re.I)
        if m: rec["correct_answer"]=m.group(1)
        m=re.match(r"^(?:explanation|solution)\s*[:\-]\s*(.+)$",line,re.I)
        if m: rec["explanation"]=m.group(1)
    return _normalize_record(rec,SUBJECTS[-1],"Forwarded Telegram message",owner_id) if len(rec["options"])>=2 else None


def format_question(r):
    return r["question_en"]+"\n\n"+"\n".join(f"{k}. {v}" for k,v in r["options"].items())+f"\n\nAnswer: {r['correct_option'] or 'Pending verification'}\nSubject: {r['subject']}"


async def publish_quiz(q,context,pool,quiz_id):
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT id,title,subject,created_by_telegram_id,is_published FROM bank_quizzes WHERE id=%s",(quiz_id,)); quiz=await cur.fetchone()
        if not quiz or quiz["created_by_telegram_id"]!=q.from_user.id or quiz["is_published"]:
            await q.edit_message_text("Draft not found, not owned by you, or already published.",reply_markup=admin_keyboard()); return
        cur=await conn.execute("SELECT COUNT(*) AS n FROM bank_quiz_questions WHERE quiz_id=%s",(quiz_id,)); n=(await cur.fetchone())["n"]
        await conn.execute("UPDATE bank_quizzes SET is_published=TRUE,published_at=NOW() WHERE id=%s",(quiz_id,))
    await q.edit_message_text(f"✅ Quiz published for students.\n\n#{quiz_id} · {quiz['title']}\nSubject: {quiz['subject']}\nQuestions: {n}\n\nStudents can open /quiz in a private chat with the bot and choose this quiz.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]]))


async def show_student_quizzes(q,pool):
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT id,title,subject,(SELECT COUNT(*) FROM bank_quiz_questions bqq WHERE bqq.quiz_id=bq.id) AS n FROM bank_quizzes bq WHERE is_published=TRUE AND (is_custom=FALSE OR custom_owner_telegram_id=%s) ORDER BY id DESC LIMIT 20", (q.from_user.id,)); rows=await cur.fetchall()
    if not rows:
        await q.edit_message_text("No quizzes are published yet."); return
    lines=["📚 Available quizzes",""]; buttons=[]
    for r in rows:
        lines.append(f"#{r['id']} · {r['title']}\n{r['subject']} · {r['n']} questions")
        buttons.append([InlineKeyboardButton(f"Start #{r['id']} · {r['title'][:35]}",callback_data=f"play:start:{r['id']}")])
    await q.edit_message_text("\n\n".join(lines),reply_markup=InlineKeyboardMarkup(buttons))


async def choose_mode(q,context,pool,quiz_id):
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT id,title FROM bank_quizzes WHERE id=%s AND is_published=TRUE AND (is_custom=FALSE OR custom_owner_telegram_id=%s)",(quiz_id,q.from_user.id)); quiz=await cur.fetchone()
    if not quiz: await q.edit_message_text("Quiz not found or not published."); return
    await q.edit_message_text(f"{quiz['title']}\n\nChoose a mode:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧠 Practice (instant explanation)",callback_data=f"play:mode:practice:{quiz_id}")],[InlineKeyboardButton("⏱ Exam (results after submission)",callback_data=f"play:mode:exam:{quiz_id}")],[InlineKeyboardButton("Back to quizzes",callback_data="play:list")]]))


async def start_attempt(q,context,pool,quiz_id,mode):
    if mode not in ("practice","exam"): return
    async with pool.connection() as conn:
        cur=await conn.execute("SELECT id,title,subject,duration_minutes,positive_marks,negative_marks FROM bank_quizzes WHERE id=%s AND is_published=TRUE AND (is_custom=FALSE OR custom_owner_telegram_id=%s)",(quiz_id,q.from_user.id)); quiz=await cur.fetchone()
        if not quiz: await q.edit_message_text("Quiz unavailable."); return
        cur=await conn.execute("SELECT qb.id,qb.question_en,qb.question_hi,qb.options,qb.correct_option,qb.explanation_en,qb.explanation_hi FROM bank_quiz_questions bqq JOIN question_bank qb ON qb.id=bqq.question_id WHERE bqq.quiz_id=%s ORDER BY bqq.position",(quiz_id,)); questions=await cur.fetchall()
        cur=await conn.execute("INSERT INTO bank_quiz_attempts(quiz_id,telegram_id,mode,total_questions) VALUES(%s,%s,%s,%s) RETURNING id",(quiz_id,q.from_user.id,mode,len(questions))); attempt_id=(await cur.fetchone())["id"]
    if not questions: await q.edit_message_text("This quiz has no available questions."); return
    context.user_data["qb_attempt"]={"attempt_id":attempt_id,"quiz":quiz,"questions":[dict(x) for x in questions],"index":0,"answers":{},"mode":mode,"started":datetime.now(timezone.utc).isoformat()}
    if mode == "exam":
        context.application.create_task(_exam_timeout(context.application, q.from_user.id, q.message.chat_id, attempt_id, pool, int(quiz["duration_minutes"])))
    await send_current_question(q,context)


async def send_current_question(q,context):
    a=context.user_data.get("qb_attempt")
    if not a: await q.edit_message_text("No active quiz attempt."); return
    idx=a["index"]
    if idx>=len(a["questions"]):
        await q.edit_message_text("You have reached the end. Submit your answers to see the result.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Submit quiz",callback_data="play:submit")]])); return
    r=a["questions"][idx]; opts=r["options"] if isinstance(r["options"],dict) else json.loads(r["options"])
    selected=a["answers"].get(str(r["id"]))
    text=f"{a['quiz']['title']} · {idx+1}/{len(a['questions'])}\nMode: {a['mode'].title()}" + (f" · {a['quiz']['duration_minutes']} min limit" if a['mode']=='exam' else "") + f"\n\n{r['question_en']}"
    if r.get("question_hi"): text+="\n"+r["question_hi"]
    text+="\n\n"+"\n".join(f"{k}. {v}" for k,v in opts.items())
    if selected: text+=f"\n\nYour answer: {selected}"
    if a.get("feedback") and a["mode"]=="practice": text+="\n\n"+a["feedback"]
    rows=[]
    for i in range(0,len(opts),2): rows.append([InlineKeyboardButton(f"{k}{' ✓' if selected==k else ''}",callback_data=f"play:ans:{k}") for k in list(opts)[i:i+2]])
    rows.append([InlineKeyboardButton("⏭ Not Attempted",callback_data="play:skip")])
    if idx>0: rows.append([InlineKeyboardButton("⬅ Previous",callback_data="play:prev"),InlineKeyboardButton("Next ➡",callback_data="play:next")])
    else: rows.append([InlineKeyboardButton("Next ➡",callback_data="play:next")])
    rows.append([InlineKeyboardButton("Submit Quiz",callback_data="play:submit")])
    await q.edit_message_text(text[:3900],reply_markup=InlineKeyboardMarkup(rows))


async def answer_question(q,context,pool,letter):
    a=context.user_data.get("qb_attempt")
    if not a: await q.answer("No active quiz.",show_alert=True); return
    r=a["questions"][a["index"]]; opts=r["options"] if isinstance(r["options"],dict) else json.loads(r["options"])
    if letter is not None and letter not in opts: await q.answer("Invalid option.",show_alert=True); return
    a["answers"][str(r["id"])]=letter
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO bank_quiz_attempt_answers(attempt_id,question_id,selected_option,is_correct) VALUES(%s,%s,%s,%s) ON CONFLICT(attempt_id,question_id) DO UPDATE SET selected_option=EXCLUDED.selected_option,is_correct=EXCLUDED.is_correct",(a["attempt_id"],r["id"],letter,(letter==r["correct_option"]) if letter else None))
    if a["mode"]=="practice":
        if letter is None: feedback=f"⏭ Not attempted. Correct answer: {r['correct_option']}."
        elif letter==r["correct_option"]: feedback="✅ Correct."
        else: feedback=f"❌ Incorrect. Correct answer: {r['correct_option']}."
        if r.get("explanation_en"): feedback+="\n\n"+r["explanation_en"]
        if r.get("explanation_hi"): feedback+="\n"+r["explanation_hi"]
        a["feedback"]=feedback
        await q.answer("Answer saved.")
    else: await q.answer("Answer saved.")
    await send_current_question(q,context)


async def _exam_timeout(application, user_id, chat_id, attempt_id, pool, duration_minutes):
    await asyncio.sleep(max(1, duration_minutes) * 60)
    user_data=application.user_data.get(user_id, {})
    attempt=user_data.get("qb_attempt") if user_data else None
    if not attempt or attempt.get("attempt_id") != attempt_id:
        return
    # The same scoring path is applied when the exam timer expires.
    qs=attempt["questions"]; answers=attempt["answers"]; correct=incorrect=skipped=0; score=0.0
    quiz=attempt["quiz"]; pos=float(quiz["positive_marks"]); neg=float(quiz["negative_marks"])
    for r in qs:
        ans=answers.get(str(r["id"]))
        if not ans: skipped+=1
        elif ans==r["correct_option"]: correct+=1; score+=pos
        else: incorrect+=1; score-=neg
    async with pool.connection() as conn:
        await conn.execute("UPDATE bank_quiz_attempts SET submitted_at=NOW(),correct_count=%s,incorrect_count=%s,skipped_count=%s,score=%s WHERE id=%s AND submitted_at IS NULL",(correct,incorrect,skipped,round(score,2),attempt_id))
    user_data.pop("qb_attempt",None)
    accuracy=(correct/(correct+incorrect)*100) if correct+incorrect else 0
    text=f"⏱ Time is up — {quiz['title']}\n\nTotal: {len(qs)}\nCorrect: {correct}\nIncorrect: {incorrect}\nNot attempted: {skipped}\nAccuracy (attempted): {accuracy:.1f}%\nScore: {score:.2f} / {len(qs)*pos:.2f}\n\nAnswer review:"
    for i,r in enumerate(qs,1):
        text+=f"\n\n{i}. Your answer: {answers.get(str(r['id'])) or 'Not attempted'} · Correct: {r['correct_option']}"
        if r.get("explanation_en"): text+="\n"+r["explanation_en"][:120]
        if r.get("explanation_hi"): text+="\n"+r["explanation_hi"][:120]
    await application.bot.send_message(chat_id=chat_id,text=text[:4000],reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Take another quiz",callback_data="play:list")]]))


async def submit_attempt(q,context,pool):
    a=context.user_data.get("qb_attempt")
    if not a: await q.edit_message_text("No active quiz attempt."); return
    qs=a["questions"]; answers=a["answers"]; correct=incorrect=skipped=0; score=0.0
    quiz=a["quiz"]; pos=float(quiz["positive_marks"]); neg=float(quiz["negative_marks"])
    for r in qs:
        ans=answers.get(str(r["id"]))
        if not ans: skipped+=1
        elif ans==r["correct_option"]: correct+=1; score+=pos
        else: incorrect+=1; score-=neg
    async with pool.connection() as conn:
        await conn.execute("UPDATE bank_quiz_attempts SET submitted_at=NOW(),correct_count=%s,incorrect_count=%s,skipped_count=%s,score=%s WHERE id=%s",(correct,incorrect,skipped,round(score,2),a["attempt_id"]))
    accuracy=(correct/(correct+incorrect)*100) if correct+incorrect else 0
    text=(f"📊 Quiz Result — {quiz['title']}\n\nTotal: {len(qs)}\nCorrect: {correct}\nIncorrect: {incorrect}\nNot attempted: {skipped}\nAccuracy (attempted): {accuracy:.1f}%\nScore: {score:.2f} / {len(qs)*pos:.2f}\nNegative marking: −{neg:g} per wrong answer")
    if a["mode"]=="exam":
        text+="\n\nAnswer review:\n"
        for i,r in enumerate(qs,1):
            ans=answers.get(str(r["id"]))
            text+=f"\n{i}. Your answer: {ans or 'Not attempted'} · Correct: {r['correct_option']}"
            if r.get("explanation_en"): text+="\n   "+r["explanation_en"][:180]
            if r.get("explanation_hi"): text+="\n   "+r["explanation_hi"][:180]
    context.user_data.pop("qb_attempt",None)
    await q.edit_message_text(text[:4000],reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Take another quiz",callback_data="play:list")]]))


async def admin_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, pool, is_authorized: bool) -> bool:
    return await receive_admin_text(update,context,pool,is_authorized)


async def route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, pool, is_authorized: bool):
    q=update.callback_query; data=(q.data or "") if q else ""
    if not data.startswith(("qb:","play:")): return False
    if data == "qb:confirmimport":
        if not is_authorized: await q.answer("Not authorized.",show_alert=True); return True
        await q.answer(); await confirm_import(q,context,pool,update.effective_user.id); return True
    if data == "qb:cancelimport":
        if not is_authorized: await q.answer("Not authorized.",show_alert=True); return True
        context.user_data.pop("qb_import_preview",None); context.user_data.pop("qb_flow",None); await q.answer(); await q.edit_message_text("Import cancelled. No questions were saved.",reply_markup=admin_keyboard()); return True
    if data == "qb:saveforward":
        if not is_authorized: await q.answer("Not authorized.",show_alert=True); return True
        record=context.user_data.pop("qb_forward_preview",None)
        if not record: await q.answer("Preview expired",show_alert=True); return True
        result=await import_records(pool,[record],update.effective_user.id,"Forwarded Telegram message","forwarded",record["subject"],0)
        await q.answer(); await q.edit_message_text(f"Saved forwarded question. Added: {result['imported']}; duplicates skipped: {result['duplicates']}; pending verification: {result['pending']}.",reply_markup=admin_keyboard()); return True
    if data == "qb:cancelforward":
        context.user_data.pop("qb_forward_preview",None); await q.answer(); await q.edit_message_text("Forwarded question discarded.",reply_markup=admin_keyboard()); return True
    if data.startswith("play:prev") or data.startswith("play:next"):
        if not q: return True
        a=context.user_data.get("qb_attempt")
        if not a: await q.answer("No active quiz.",show_alert=True); return True
        if data=="play:prev": a["index"]=max(0,a["index"]-1)
        else: a["index"]=min(len(a["questions"])-1,a["index"]+1)
        a.pop("feedback",None)
        await q.answer(); await send_current_question(q,context); return True
    await handle_callback(update,context,pool,is_authorized)
    return True
