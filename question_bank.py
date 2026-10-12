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

import leaderboard as lb

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

log = logging.getLogger("bpsc_nexus_question_bank")
MAX_QUESTIONS_PER_IMPORT = 20000
MAX_FILE_BYTES = 20 * 1024 * 1024
LETTERS = "ABCDE"
SUBJECTS = ["General Science", "Bihar Special", "Modern Indian History", "Indian Polity", "Geography", "Economy", "Ancient & Medieval History", "Art & Culture", "Other / Unclassified"]

DDL = [
    """CREATE TABLE IF NOT EXISTS question_bank_subjects (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        created_by_telegram_id BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
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
        # Common nested exports: {chapters:[{name, questions:[...]}]} or
        # a list of chapter records. Flatten without losing chapter/topic labels.
        for key in ("questions", "items", "data", "results", "mcqs", "records"):
            val = obj.get(key)
            if isinstance(val, list): return val
        for key in ("chapters", "sections", "subjects", "topics"):
            val = obj.get(key)
            if isinstance(val, list):
                flattened=[]
                for group in val:
                    if not isinstance(group, dict): continue
                    group_name = group.get("name") or group.get("title") or group.get("chapterNameEn") or group.get("section") or ""
                    group_hi = group.get("hiName") or group.get("chapterNameHi") or ""
                    for q in group.get("questions", group.get("items", [])) or []:
                        if isinstance(q, dict):
                            row=dict(q)
                            row.setdefault("chapter", group_name)
                            if group_hi: row.setdefault("chapter_hi", group_hi)
                            flattened.append(row)
                if flattened: return flattened
        # One question object is also accepted.
        if any(k in {str(x).lower() for x in obj} for k in ("question", "question_en", "question_text", "q")):
            return [obj]
    raise ValueError("JSON must contain a question array or a single question object.")


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts = []
    def handle_data(self, data):
        if data.strip(): self.parts.append(data.strip())


def _js_value_after_assignment(raw: str, names: tuple[str, ...]):
    """Decode a JSON-compatible JS assignment without executing uploaded code."""
    for name in names:
        match = re.search(r"\b(?:const|let|var)\s+" + re.escape(name) + r"\s*=\s*", raw, flags=re.I)
        if not match: continue
        start = match.end()
        while start < len(raw) and raw[start].isspace(): start += 1
        if start >= len(raw) or raw[start] not in "[{": continue
        try:
            value, end = json.JSONDecoder().raw_decode(raw[start:])
            return value
        except Exception:
            continue
    return None


def _answer_index_to_letter(value, option_count: int) -> str | None:
    if isinstance(value, bool): return None
    if isinstance(value, int):
        # These two source formats document/use zero-based indexes.
        return LETTERS[value] if 0 <= value < min(option_count, len(LETTERS)) else None
    text = _norm(value).upper()
    m = re.fullmatch(r"(?:OPTION\s*)?\(?([A-E])\)?[.)] ?", text)
    if m: return m.group(1)
    if text.isdigit():
        n=int(text)
        if 0 <= n < min(option_count, len(LETTERS)): return LETTERS[n]
        if 1 <= n <= min(option_count, len(LETTERS)): return LETTERS[n-1]
    return None


def _flatten_chapters(obj: Any) -> list[dict]:
    """Flatten chapter-wrapped question arrays, preserving chapter and bilingual fields."""
    if isinstance(obj, list):
        out=[]
        for item in obj:
            if isinstance(item, dict) and isinstance(item.get("questions"), list):
                chapter = item.get("chapterNameEn") or item.get("name") or item.get("title") or item.get("section") or ""
                chapter_hi = item.get("chapterNameHi") or item.get("hiName") or ""
                for q in item["questions"]:
                    if not isinstance(q, dict): continue
                    row=dict(q); row.setdefault("chapter", chapter); row.setdefault("topic", q.get("topic") or chapter)
                    if chapter_hi: row.setdefault("chapter_hi", chapter_hi)
                    out.append(row)
            else: out.append(item)
        return out
    if isinstance(obj, dict):
        for key in ("questions", "items", "data", "results", "mcqs", "records"):
            val=obj.get(key)
            if isinstance(val, list): return _flatten_chapters(val)
        for key in ("chapters", "sections", "subjects", "topics"):
            val=obj.get(key)
            if isinstance(val, list): return _flatten_chapters(val)
    return _extract_json_records(obj)


def _balanced_js_value(raw: str, start: int):
    """Read one JSON-compatible array/object without executing JavaScript."""
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(raw[start:])
        return value
    except Exception:
        return None


class _VisibleTextExtractor(HTMLParser):
    """Extract readable HTML text while ignoring scripts, styles and hidden payloads."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]; self.skip=0
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag.lower() in ("script", "style", "noscript", "svg"): self.skip += 1
        if not self.skip and tag.lower() in ("br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "section", "article"):
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style", "noscript", "svg") and self.skip: self.skip -= 1
        if not self.skip and tag.lower() in ("p", "div", "li", "tr", "td", "th", "section", "article"):
            self.parts.append("\n")
    def handle_data(self, data):
        if not self.skip and data.strip(): self.parts.append(data.strip()+" ")


def _generic_question_rows(obj: Any) -> list[dict]:
    """Recursively discover question records inside common export wrappers."""
    out=[]
    question_keys={"question","question_en","question_text","questiontext","questionenglish","q","prompt","stem","ques","प्रश्न"}
    container_keys=("questions","questionlist","question_list","mcqs","items","data","results","records","quiz","quizdata","questionbank","question_bank","allquestions","all_questions","examquestions","exam_questions","list")
    if isinstance(obj, list):
        # First flatten chapter/subject wrappers while retaining their labels.
        for item in obj:
            if isinstance(item, dict):
                child_key=next((k for k in item if str(k).lower() in container_keys and isinstance(item[k],list)),None)
                if child_key:
                    label=item.get("chapter") or item.get("chapterNameEn") or item.get("name") or item.get("title") or item.get("subject") or item.get("section") or item.get("topic") or ""
                    hi_label=item.get("chapterNameHi") or item.get("hiName") or item.get("subject_hi") or ""
                    children=_generic_question_rows(item[child_key])
                    for row in children:
                        row.setdefault("topic",label)
                        if hi_label: row.setdefault("topic_hi",hi_label)
                    out.extend(children)
                elif any(str(k).lower().replace(" ","_") in question_keys for k in item):
                    out.append(item)
                else:
                    nested=_generic_question_rows(item)
                    out.extend(nested)
            else:
                continue
        return out
    if isinstance(obj, dict):
        keys={str(k).lower().replace(" ","_") for k in obj}
        if keys & question_keys:
            return [obj]
        for key,val in obj.items():
            if isinstance(val,(dict,list)) and (str(key).lower().replace("-","_") in container_keys or isinstance(val,list)):
                got=_generic_question_rows(val)
                if got:
                    # A wrapper label is useful as a topic/subject fallback.
                    label=obj.get("name") or obj.get("title") or obj.get("subject") or obj.get("section") or obj.get("chapter") or ""
                    for row in got: row.setdefault("topic",label)
                    return got
    return out


def _adapt_generic_row(row: dict, default_subject: str, source: str) -> dict:
    """Map common English/Hindi field aliases to the question-bank schema."""
    def first(*keys):
        return _get(row,*keys,default=None)
    q=first("question_en","questionEnglish","question_text","questionText","question","prompt","stem","ques","q","title","प्रश्न")
    qhi=first("question_hi","questionHindi","hindi_question","question_hin","hiQuestion","questionHindiText","प्रश्न_हिंदी")
    if isinstance(q,dict):
        qhi=qhi or q.get("hi") or q.get("hindi")
        q=q.get("en") or q.get("english") or q.get("text") or qhi
    opts=first("options","opts","choices","answers","alternatives","optionList","option_list","answerOptions","answer_options")
    if isinstance(opts,dict):
        # Handle {en:[...], hi:[...]} and {A:{en:...,hi:...}} shapes.
        if any(str(k).lower() in ("en","english","hi","hindi") for k in opts):
            en=opts.get("en") or opts.get("english") or opts.get("hi") or opts.get("hindi") or []
            hi=opts.get("hi") or opts.get("hindi") or []
            opts=en
            if isinstance(opts,list) and isinstance(hi,list):
                opts=[(str(v)+(" / "+str(hi[i]) if i<len(hi) and hi[i] else "")) for i,v in enumerate(opts)]
        else:
            normalized={}
            for k,v in opts.items():
                if isinstance(v,dict):
                    en=v.get("en") or v.get("english") or v.get("text") or v.get("value") or ""
                    hi=v.get("hi") or v.get("hindi") or ""
                    normalized[k]=str(en)+(" / "+str(hi) if hi else "")
                else: normalized[k]=v
            opts=normalized
    if isinstance(opts,list):
        opts={LETTERS[i]:v for i,v in enumerate(opts[:5])}
    if not isinstance(opts,dict): opts={}
    # Separate Hindi options found under parallel fields.
    hiopts=first("hiopts","options_hi","hindi_options","optionHindi","option_hindi")
    if isinstance(hiopts,list):
        for i,val in enumerate(hiopts[:5]):
            letter=LETTERS[i]
            if val and letter in opts and str(val) not in str(opts[letter]): opts[letter]=str(opts[letter])+" / "+str(val)
    ans=first("correct_option","correctAnswer","correct_answer","answer_key","answerKey","correct","answer","ans","key","rightAnswer","right_answer","correctIndex","correct_index","answerIndex","answer_index","solutionIndex")
    if isinstance(ans,dict): ans=ans.get("option") or ans.get("key") or ans.get("index") or ans.get("answer") or ans.get("en")
    # Explicit index fields are conventionally zero-based; generic answer fields usually are 1-based.
    anskey=str(next((k for k in row if str(k).lower().replace(" ","_") in ("ans","correctindex","correct_index","answerindex","answer_index","solutionindex")),"")).lower()
    if anskey and isinstance(ans,(int,float)) and not isinstance(ans,bool):
        # Explicit index-style fields (including the common `ans`) are zero-based.
        ans=LETTERS[int(ans)] if 0 <= int(ans) < min(len(opts),5) else None
    elif isinstance(ans,(int,float)) and not isinstance(ans,bool):
        n=int(ans); ans=LETTERS[n-1] if 1 <= n <= min(len(opts),5) else (LETTERS[0] if n==0 else None)
    exp=first("explanation_en","explanationEnglish","explanation","solution","reason","exp","rationale","解析","解説")
    exp_hi=first("explanation_hi","explanationHindi","hindi_explanation","hiexp","व्याख्या")
    if isinstance(exp,dict):
        exp_hi=exp_hi or exp.get("hi") or exp.get("hindi")
        exp=exp.get("en") or exp.get("english") or exp.get("text") or ""
    subject=first("subject","subjectName","category","categoryName","section","paper","exam","group") or default_subject or "Other / Unclassified"
    topic=first("topic","subtopic","chapter","chapterNameEn","unit","lesson","sectionName","page","tag")
    if isinstance(q,list): q="\n".join(map(str,q))
    if isinstance(qhi,list): qhi="\n".join(map(str,qhi))
    return {"question":str(q or ""),"question_hi":str(qhi or ""),"options":opts,"correct_answer":ans,
            "explanation":str(exp or ""),"explanation_hi":str(exp_hi or ""),"subject":str(subject),"topic":str(topic or ""),"source":source}


def _html_to_records(raw: str) -> list[dict]:
    """Format-adaptive HTML importer. Never executes uploaded JavaScript."""
    candidates=[]
    # Known compact format used by earlier BPSC Nexus exports.
    raw_payload=_js_value_after_assignment(raw,("RAW",))
    if isinstance(raw_payload,list) and raw_payload:
        converted=[]
        for item in raw_payload:
            if not isinstance(item,dict): continue
            qparts=item.get("i") or []; qparts=[qparts] if isinstance(qparts,str) else qparts
            qtext="\n".join(str(x) for x in qparts if x is not None)
            tail=item.get("tl") or []; tail=[tail] if isinstance(tail,str) else tail
            if tail: qtext += ("\n" if qtext else "")+"\n".join(str(x) for x in tail if x is not None)
            opts_raw=item.get("o") or []
            opts={LETTERS[i]:str(v) for i,v in enumerate(opts_raw[:5]) if v is not None and _norm(v)} if isinstance(opts_raw,list) else opts_raw
            correct=_answer_index_to_letter(item.get("a"),len(opts))
            exp=item.get("x") or {}; exp_parts=exp.get("w",[]) if isinstance(exp,dict) else exp
            if isinstance(exp_parts,str): exp_parts=[exp_parts]
            explanation="\n".join(str(x) for x in exp_parts if x is not None)
            converted.append({"question":qtext or str(item.get("id") or "Question"),"options":opts,"correct_answer":correct,"explanation":explanation,
                "subject":item.get("s") or "Other / Unclassified","topic":item.get("c") or item.get("s") or None,"source":"BPSCVEERS HTML import"})
        if converted: candidates.append(converted)

    # Try all embedded data scripts, not only scripts with a specific ID/type.
    for attrs,body in re.findall(r"<script\b([^>]*)>(.*?)</script\s*>",raw,flags=re.I|re.S):
        if not body.strip(): continue
        parsed=[]
        try: parsed.append(json.loads(html.unescape(body.strip())))
        except Exception: pass
        # JS scripts frequently declare an array/object in a named variable.
        for match in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([\[{])",body):
            val=_balanced_js_value(body,match.start(2))
            if val is not None: parsed.append(val)
        for obj in parsed:
            rows=_generic_question_rows(obj)
            if rows:
                candidates.append([_adapt_generic_row(r,"Other / Unclassified","Adaptive embedded HTML import") for r in rows if isinstance(r,dict)])

    # Search assignments anywhere in the document, including minified scripts and unconventional variable names.
    for match in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([\[{])",raw):
        name=match.group(1); val=_balanced_js_value(raw,match.start(2))
        if val is None: continue
        rows=_generic_question_rows(val)
        if rows:
            candidates.append([_adapt_generic_row(r,"Other / Unclassified",f"JavaScript {name} import") for r in rows if isinstance(r,dict)])

    # Last structured-data fallback: locate JSON-looking arrays/objects anywhere, useful for
    # JSON blobs in data attributes or scripts with missing/malformed type attributes.
    decoder=json.JSONDecoder()
    for m in re.finditer(r"[\[{]",raw):
        # Avoid rescanning every nested bracket in huge documents.
        if m.start() and raw[m.start()-1] not in "=:[,\n\r\t ": continue
        val=_balanced_js_value(raw,m.start())
        if val is None: continue
        rows=_generic_question_rows(val)
        if rows:
            candidates.append([_adapt_generic_row(r,"Other / Unclassified","Embedded structured-data import") for r in rows if isinstance(r,dict)])

    # DOM/text fallback for HTML quizzes that render questions as ordinary elements or tables.
    # This is intentionally conservative: only records with a question and at least two options survive normalization.
    text_parser=_VisibleTextExtractor()
    try:
        text_parser.feed(raw)
        visible="\n".join(text_parser.parts)
        pattern=re.compile(r"(?ims)(?:^|\n)\s*(?:Q(?:uestion)?\s*)?(\d{1,5})\s*[.)\-:]\s*(.+?)(?=(?:\n\s*[A-E][.)]\s)|\Z)")
        rows=[]
        for qm in pattern.finditer(visible):
            block=qm.group(2).strip(); optmatches=list(re.finditer(r"(?im)(?:^|\n)\s*([A-E])[.)]\s*(.+?)(?=(?:\n\s*[A-E][.)]\s)|\Z)",block))
            if len(optmatches)<2: continue
            firstopt=optmatches[0].start(); qtext=block[:firstopt].strip()
            opts={om.group(1):om.group(2).strip() for om in optmatches}
            answer_match=re.search(r"(?im)(?:correct\s+answer|answer|ans|सही\s+उत्तर)\s*[:：\-]\s*\(?([A-E])\)?",block)
            explanation_match=re.search(r"(?im)(?:explanation|solution|व्याख्या)\s*[:：]\s*(.+)$",block)
            rows.append({"question":qtext,"options":opts,"correct_answer":answer_match.group(1) if answer_match else None,
                         "explanation":explanation_match.group(1).strip() if explanation_match else "","subject":"Other / Unclassified","source":"Visible HTML text import"})
        if rows: candidates.append(rows)
    except Exception: pass

    # Prefer the candidate with the largest plausible question set. Duplicate candidates can arise
    # because a single payload is discoverable both in a script and as a global assignment.
    plausible=[]
    for candidate in candidates:
        if not candidate: continue
        cleaned=[r for r in candidate if isinstance(r,dict) and _norm(_get(r,"question","question_en","q","prompt","stem"))]
        if cleaned: plausible.append(cleaned)
    if plausible:
        return max(plausible,key=len)
    raise ValueError("Could not identify a usable question set in this HTML. The importer tried embedded JSON, JavaScript arrays/objects, nested chapter data and visible HTML question blocks. This file may use an unsupported layout, encrypted data, or dynamically fetched questions. No uploaded JavaScript was executed.")


def parse_file(filename: str, raw: bytes, subject: str, owner_id: int) -> tuple[list[dict], str]:
    if len(raw) > MAX_FILE_BYTES: raise ValueError("File exceeds the 20 MB import limit.")
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
        # Keep user-created subjects persistent and available in all admin selectors.
        cur = await conn.execute("SELECT name FROM question_bank_subjects ORDER BY name")
        for row in await cur.fetchall():
            if row["name"] not in SUBJECTS: SUBJECTS.append(row["name"])
    await lb.ensure_schema(pool)
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
        [InlineKeyboardButton("➕ Add One Question", callback_data="qb:addone"), InlineKeyboardButton("📥 Bulk Import File", callback_data="qb:import")],
        [InlineKeyboardButton("➕ Create Subject", callback_data="qb:newsubject"), InlineKeyboardButton("🧪 Create Test Series", callback_data="qb:build")],
        [InlineKeyboardButton("🗃 Browse / Edit", callback_data="qb:browse"), InlineKeyboardButton("🗑 Delete Questions", callback_data="qb:deletehub")],
        [InlineKeyboardButton("🧹 Bulk Delete by IDs", callback_data="qb:bulkdelete"), InlineKeyboardButton("📦 Import History / Undo", callback_data="qb:history")],
        [InlineKeyboardButton("🗂 Manage Test Series", callback_data="qb:manage")],
        [InlineKeyboardButton("🕵️ Verify Pending", callback_data="qb:pending"), InlineKeyboardButton("🧪 Quality Audit", callback_data="qb:audit")],
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
    if data == "qb:newsubject":
        context.user_data["qb_flow"]={"step":"create_subject"}
        await q.edit_message_text("➕ Create a new subject\n\nSend the exact subject name. It will be saved to the database and appear in future import and test-series selectors.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data="qb:home")]])); return
    if data == "qb:addone":
        context.user_data["qb_flow"]={"step":"add_question_subject"}
        await q.edit_message_text("➕ Add a question\n\nChoose its subject first:",reply_markup=subject_keyboard("qb:addsubject")); return
    if data.startswith("qb:addsubject:"):
        idx=int(data.rsplit(":",1)[1]); context.user_data["qb_flow"]={"step":"add_question_text","subject":SUBJECTS[idx]}
        await q.edit_message_text("Send the MCQ in one message using this format:\n\nQuestion text\nA. Option A\nB. Option B\nC. Option C\nD. Option D\nAnswer: B\nExplanation: Explanation text\n\nHindi text may be included too. The question is previewed before saving.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖ Cancel",callback_data="qb:home")]])); return
    if data == "qb:deletehub":
        await q.edit_message_text("🗑 Question deletion\n\nChoose a group to browse. Open a question to edit, archive, or permanently delete it. Permanent deletion can also remove its links from quizzes.",reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("All questions",callback_data="qb:liststatus:all:0")],
            [InlineKeyboardButton("Verified / Ready",callback_data="qb:liststatus:ready:0"),InlineKeyboardButton("Pending",callback_data="qb:liststatus:pending:0")],
            [InlineKeyboardButton("Archived",callback_data="qb:liststatus:archived:0")],
            [InlineKeyboardButton("🧹 Bulk delete ALL questions",callback_data="qb:bulkstatus:all")],
            [InlineKeyboardButton("Delete all Ready",callback_data="qb:bulkstatus:ready"),InlineKeyboardButton("Delete all Pending",callback_data="qb:bulkstatus:pending")],
            [InlineKeyboardButton("Delete all Archived",callback_data="qb:bulkstatus:archived")],
            [InlineKeyboardButton("⬅ Question Bank menu",callback_data="qb:home")]])); return
    if data.startswith("qb:bulkstatus:"):
        status=data.rsplit(":",1)[1]
        if status not in {"all","ready","pending","archived"}:
            await q.edit_message_text("Invalid bulk-delete filter.",reply_markup=admin_keyboard()); return
        async with pool.connection() as conn:
            if status == "all":
                cur=await conn.execute("SELECT COUNT(*) AS n FROM question_bank")
            else:
                cur=await conn.execute("SELECT COUNT(*) AS n FROM question_bank WHERE status=%s",(status,))
            count=int((await cur.fetchone())["n"])
        if count == 0:
            await q.edit_message_text(f"No {status} questions to delete.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to deletion",callback_data="qb:deletehub")]])); return
        context.user_data["qb_bulk_status"] = status
        label = "ALL questions" if status == "all" else f"all {status} questions"
        await q.edit_message_text(
            f"⚠️ BULK DELETE CONFIRMATION\n\nYou are about to permanently delete {count} {label}.\n\nThis removes those questions from quiz sets and removes answer-level attempt records for those questions. Aggregate attempt totals remain. This cannot be undone.\n\nContinue only if this is the intended group.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 Permanently delete {count}",callback_data="qb:bulkstatusconfirm")],[InlineKeyboardButton("Cancel",callback_data="qb:deletehub")]])
        ); return
    if data == "qb:bulkstatusconfirm":
        status=context.user_data.get("qb_bulk_status")
        if status not in {"all","ready","pending","archived"}:
            await q.edit_message_text("Bulk-delete selection expired. Start again.",reply_markup=admin_keyboard()); return
        async with pool.connection() as conn:
            async with conn.transaction():
                if status == "all":
                    await conn.execute("DELETE FROM bank_quiz_questions WHERE question_id IN (SELECT id FROM question_bank)")
                    await conn.execute("DELETE FROM bank_quiz_attempt_answers WHERE question_id IN (SELECT id FROM question_bank)")
                    cur=await conn.execute("DELETE FROM question_bank RETURNING id")
                else:
                    await conn.execute("DELETE FROM bank_quiz_questions WHERE question_id IN (SELECT id FROM question_bank WHERE status=%s)",(status,))
                    await conn.execute("DELETE FROM bank_quiz_attempt_answers WHERE question_id IN (SELECT id FROM question_bank WHERE status=%s)",(status,))
                    cur=await conn.execute("DELETE FROM question_bank WHERE status=%s RETURNING id",(status,))
                deleted=len(await cur.fetchall())
        context.user_data.pop("qb_bulk_status",None)
        await q.edit_message_text(f"✅ Bulk deletion complete.\n\nQuestions permanently deleted: {deleted}\nFilter: {status.title()}\n\nNo database tables were dropped.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Delete another group",callback_data="qb:deletehub")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return
    if data.startswith("qb:liststatus:"):
        _,_,status,page=data.split(":",3); page=max(0,int(page)); await show_question_list(q,pool,status,page); return
    if data == "qb:bulkdelete":
        context.user_data["qb_flow"]={"step":"bulk_delete_ids"}
        await q.edit_message_text("🧹 Bulk delete questions\n\nSend the question IDs separated by commas or spaces (example: 12, 18, 25). I will show a confirmation before changing anything.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖ Cancel",callback_data="qb:home")]])); return
    if data == "qb:bulkdeleteconfirm":
        ids=context.user_data.get("qb_bulk_delete_ids",[])
        if not ids: await q.edit_message_text("No question IDs selected.",reply_markup=admin_keyboard()); return
        deleted=0; unhandled=[]
        async with pool.connection() as conn:
            async with conn.transaction():
                for qid in ids:
                    await conn.execute("DELETE FROM bank_quiz_questions WHERE question_id=%s",(qid,))
                    # Attempt totals remain intact; remove only answer-level rows that FK-restrict deletion.
                    await conn.execute("DELETE FROM bank_quiz_attempt_answers WHERE question_id=%s",(qid,))
                    cur=await conn.execute("DELETE FROM question_bank WHERE id=%s RETURNING id",(qid,))
                    if await cur.fetchone(): deleted+=1
                    else: unhandled.append(qid)
        context.user_data.pop("qb_bulk_delete_ids",None); context.user_data.pop("qb_flow",None)
        await q.edit_message_text(f"✅ Bulk delete complete\n\nDeleted: {deleted}\nNot found: {', '.join(map(str,unhandled)) if unhandled else 'None'}\n\nQuiz links for deleted questions were removed. Existing database tables and unrelated questions were left intact.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Delete more",callback_data="qb:bulkdelete")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return
    if data == "qb:purgearchivedconfirm":
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT COUNT(*) AS n FROM question_bank WHERE status='archived'"); n=(await cur.fetchone())["n"]
        await q.edit_message_text(f"Permanently delete all {n} archived question(s)?\n\nQuiz links and answer-level attempt rows will also be removed; aggregate attempt totals remain. This cannot be undone.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Confirm delete archived",callback_data="qb:purgearchived")],[InlineKeyboardButton("Cancel",callback_data="qb:deletehub")]])); return
    if data == "qb:purgearchived":
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM bank_quiz_questions WHERE question_id IN (SELECT id FROM question_bank WHERE status='archived')")
                await conn.execute("DELETE FROM bank_quiz_attempt_answers WHERE question_id IN (SELECT id FROM question_bank WHERE status='archived')")
                cur=await conn.execute("DELETE FROM question_bank WHERE status='archived' RETURNING id"); n=len(await cur.fetchall())
        await q.edit_message_text(f"✅ Permanently deleted {n} archived question(s).",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Question deletion",callback_data="qb:deletehub")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return
    if data.startswith("qb:purgeconfirm:"):
        qid=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT question_en,status,(SELECT COUNT(*) FROM bank_quiz_questions WHERE question_id=%s) AS refs FROM question_bank WHERE id=%s",(qid,qid)); r=await cur.fetchone()
        if not r: await q.edit_message_text("Question not found.",reply_markup=admin_keyboard()); return
        await q.edit_message_text(f"Permanently delete question #{qid}?\n\nQuiz references: {r['refs']}\nStatus: {r['status']}\n\nThis removes it from quiz sets and removes answer-level attempt rows for this question; aggregate attempt totals remain. This cannot be undone.\n\n{r['question_en'][:500]}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Confirm permanent delete",callback_data=f"qb:purge:{qid}")],[InlineKeyboardButton("Cancel",callback_data=f"qb:detail:{qid}")]])); return
    if data.startswith("qb:purge:"):
        qid=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM bank_quiz_questions WHERE question_id=%s",(qid,))
                await conn.execute("DELETE FROM bank_quiz_attempt_answers WHERE question_id=%s",(qid,))
                cur=await conn.execute("DELETE FROM question_bank WHERE id=%s RETURNING id",(qid,)); row=await cur.fetchone()
        await q.edit_message_text(f"{'✅ Question #' + str(qid) + ' permanently deleted and removed from quiz sets.' if row else 'Question not found; no question was deleted.'}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Delete more",callback_data="qb:deletehub")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return
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
    if data == "qb:audit":
        async with pool.connection() as conn:
            cur=await conn.execute("""SELECT COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status='ready') AS ready,
                COUNT(*) FILTER (WHERE status='pending') AS pending,
                COUNT(*) FILTER (WHERE status='archived') AS archived,
                COUNT(*) FILTER (WHERE status='ready' AND (explanation_en IS NULL OR BTRIM(explanation_en)='') AND (explanation_hi IS NULL OR BTRIM(explanation_hi)='')) AS no_explanation,
                COUNT(*) FILTER (WHERE status='ready' AND (subject='Other / Unclassified' OR BTRIM(subject)='')) AS unclassified,
                COUNT(*) FILTER (WHERE status='ready' AND jsonb_object_length(options)<2) AS invalid_options
                FROM question_bank""")
            r=await cur.fetchone()
            cur=await conn.execute("SELECT subject,COUNT(*) AS n FROM question_bank WHERE status='ready' GROUP BY subject ORDER BY n DESC,subject LIMIT 12")
            subjects=await cur.fetchall()
        lines=["🧪 Question Bank Quality Audit","",f"Total records: {r['total']}",f"Verified and usable: {r['ready']}",f"Pending review: {r['pending']}",f"Archived: {r['archived']}",f"Ready without any explanation: {r['no_explanation']}",f"Ready but unclassified: {r['unclassified']}",f"Ready with invalid option count: {r['invalid_options']}","","Verified questions by subject:"]
        lines.extend(f"• {x['subject']}: {x['n']}" for x in subjects)
        lines += ["","Note: duplicate imports are prevented by the question hash. Review pending answers before publishing tests."]
        await q.edit_message_text("\n".join(lines)[:4000],reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Review pending",callback_data="qb:pending")],[InlineKeyboardButton("Manage questions",callback_data="qb:browse")],[InlineKeyboardButton("Back",callback_data="qb:home")]])); return
    if data == "qb:manage":
        async with pool.connection() as conn:
            cur=await conn.execute("""SELECT q.id,q.title,q.subject,q.is_published,q.created_by_telegram_id,COUNT(qq.question_id) AS n
                FROM bank_quizzes q LEFT JOIN bank_quiz_questions qq ON qq.quiz_id=q.id
                GROUP BY q.id ORDER BY q.id DESC LIMIT 15""")
            rows=await cur.fetchall()
        if not rows:
            await q.edit_message_text("No quizzes exist yet.",reply_markup=admin_keyboard()); return
        buttons=[[InlineKeyboardButton(f"{'🟢' if r['is_published'] else '📝'} #{r['id']} · {r['title'][:30]} ({r['n']} Q)",callback_data=f"qb:manageview:{r['id']}")] for r in rows]
        buttons.append([InlineKeyboardButton("Back",callback_data="qb:home")])
        await q.edit_message_text("🛠 Quiz Manager\n\nChoose a quiz to edit its settings, publication status or sharing link.",reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith("qb:manageview:"):
        quiz_id=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn:
            cur=await conn.execute("""SELECT q.*,COUNT(qq.question_id) AS n FROM bank_quizzes q LEFT JOIN bank_quiz_questions qq ON qq.quiz_id=q.id WHERE q.id=%s GROUP BY q.id""",(quiz_id,)); r=await cur.fetchone()
        if not r:
            await q.edit_message_text("Quiz not found.",reply_markup=admin_keyboard()); return
        state="Published" if r['is_published'] else "Draft"
        text=f"🛠 Quiz #{r['id']}\n\nTitle: {r['title']}\nSubject: {r['subject']}\nQuestions: {r['n']}\nStatus: {state}\nDuration: {r['duration_minutes']} minutes\nScoring: +{r['positive_marks']} / −{r['negative_marks']}\nQuestion timer: {r['question_time_seconds']} seconds\nShuffle options: {'Yes' if r['shuffle_options'] else 'No'}"
        buttons=[[InlineKeyboardButton("✏️ Edit title",callback_data=f"qb:editquiztitle:{quiz_id}"),InlineKeyboardButton("⏱ Duration",callback_data=f"qb:editquizduration:{quiz_id}")],[InlineKeyboardButton("⚖️ Scoring",callback_data=f"qb:editquizmarks:{quiz_id}")],[InlineKeyboardButton("🧩 Edit question set",callback_data=f"qb:managequestions:{quiz_id}")],[InlineKeyboardButton("🔒 Unpublish" if r['is_published'] else "🚀 Publish",callback_data=f"qb:togglepublish:{quiz_id}")],[InlineKeyboardButton("Back to quizzes",callback_data="qb:manage")]]
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith("qb:editquiztitle:"):
        quiz_id=int(data.rsplit(":",1)[1]); context.user_data["qb_flow"]={"step":"edit_quiz_title","quiz_id":quiz_id}
        await q.edit_message_text("Send the new quiz title (1–160 characters).",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data=f"qb:manageview:{quiz_id}")]])); return
    if data.startswith("qb:editquizduration:"):
        quiz_id=int(data.rsplit(":",1)[1]); context.user_data["qb_flow"]={"step":"edit_quiz_duration","quiz_id":quiz_id}
        await q.edit_message_text("Send the total exam duration in minutes (1–360).",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data=f"qb:manageview:{quiz_id}")]])); return
    if data.startswith("qb:editquizmarks:"):
        quiz_id=int(data.rsplit(":",1)[1]); context.user_data["qb_flow"]={"step":"edit_quiz_marks","quiz_id":quiz_id}
        await q.edit_message_text("Send scoring as two numbers separated by a comma: positive,negative\nExample: 1,0.333333",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data=f"qb:manageview:{quiz_id}")]])); return
    if data.startswith("qb:managequestions:"):
        quiz_id=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT title FROM bank_quizzes WHERE id=%s",(quiz_id,)); quiz=await cur.fetchone()
            cur=await conn.execute("SELECT qq.question_id,qq.position,q.question_en,q.subject FROM bank_quiz_questions qq JOIN question_bank q ON q.id=qq.question_id WHERE qq.quiz_id=%s ORDER BY qq.position LIMIT 20",(quiz_id,)); rows=await cur.fetchall()
            cur=await conn.execute("SELECT COUNT(*) AS n FROM bank_quiz_questions WHERE quiz_id=%s",(quiz_id,)); total=(await cur.fetchone())["n"]
        if not quiz:
            await q.edit_message_text("Quiz not found.",reply_markup=admin_keyboard()); return
        lines=[f"🧩 Question set · {quiz['title']}",f"Total questions: {total}","Showing first 20. Tap a question to remove it.",""]
        buttons=[[InlineKeyboardButton(f"Remove #{r['question_id']} · {r['question_en'][:28]}",callback_data=f"qb:removequizq:{quiz_id}:{r['question_id']}")] for r in rows]
        buttons += [[InlineKeyboardButton("➕ Add questions",callback_data=f"qb:addquizq:{quiz_id}")],[InlineKeyboardButton("Back to quiz settings",callback_data=f"qb:manageview:{quiz_id}")]]
        await q.edit_message_text("\n".join(lines),reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith("qb:removequizq:"):
        _,_,quiz_id,qid=data.split(":",3); quiz_id=int(quiz_id); qid=int(qid)
        async with pool.connection() as conn:
            cur=await conn.execute("DELETE FROM bank_quiz_questions WHERE quiz_id=%s AND question_id=%s RETURNING question_id",(quiz_id,qid)); removed=await cur.fetchone()
            # Compact positions after a deletion while preserving the current relative order.
            if removed:
                cur=await conn.execute("SELECT question_id FROM bank_quiz_questions WHERE quiz_id=%s ORDER BY position",(quiz_id,)); ids=[r["question_id"] for r in await cur.fetchall()]
                await conn.execute("DELETE FROM bank_quiz_questions WHERE quiz_id=%s",(quiz_id,))
                for pos,question_id in enumerate(ids,1): await conn.execute("INSERT INTO bank_quiz_questions(quiz_id,position,question_id) VALUES(%s,%s,%s)",(quiz_id,pos,question_id))
        await q.answer("Question removed." if removed else "Question was not in this quiz.")
        await q.edit_message_text("Question set updated.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("View question set",callback_data=f"qb:managequestions:{quiz_id}")],[InlineKeyboardButton("Quiz settings",callback_data=f"qb:manageview:{quiz_id}")]])); return
    if data.startswith("qb:addquizq:"):
        quiz_id=int(data.rsplit(":",1)[1]); context.user_data["qb_flow"]={"step":"add_quiz_subject","quiz_id":quiz_id}
        rows=[[InlineKeyboardButton("🌐 Mixed subjects",callback_data=f"qb:addquizsubject:{quiz_id}:all")]]
        for i in range(0,len(SUBJECTS),2): rows.append([InlineKeyboardButton(SUBJECTS[j],callback_data=f"qb:addquizsubject:{quiz_id}:{j}") for j in range(i,min(i+2,len(SUBJECTS)))])
        rows.append([InlineKeyboardButton("Cancel",callback_data=f"qb:managequestions:{quiz_id}")])
        await q.edit_message_text("Choose the subject pool for additional questions:",reply_markup=InlineKeyboardMarkup(rows)); return
    if data.startswith("qb:addquizsubject:"):
        _,_,quiz_id,value=data.split(":",3); quiz_id=int(quiz_id)
        subject=None if value=="all" else SUBJECTS[int(value)]
        context.user_data["qb_flow"]={"step":"add_quiz_count","quiz_id":quiz_id,"subject":subject}
        await q.edit_message_text(f"Subject pool: {subject or 'Mixed subjects'}\nSend how many additional questions to add (1–100). Existing questions will be excluded.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data=f"qb:managequestions:{quiz_id}")]])); return
    if data.startswith("qb:togglepublish:"):
        quiz_id=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn:
            cur=await conn.execute("UPDATE bank_quizzes SET is_published=NOT is_published,published_at=CASE WHEN is_published THEN NULL ELSE NOW() END WHERE id=%s RETURNING is_published,title",(quiz_id,)); r=await cur.fetchone()
        await q.edit_message_text(f"Quiz {'published' if r and r['is_published'] else 'unpublished' if r else 'not found'}.\n\n{r['title'] if r else ''}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Manage this quiz",callback_data=f"qb:manageview:{quiz_id}")],[InlineKeyboardButton("Quiz list",callback_data="qb:manage")]])); return
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
            cur=await conn.execute("""SELECT b.id,b.file_name,b.total_detected,b.imported_count,b.duplicate_count,b.invalid_count,b.status,b.created_at,
                (SELECT COUNT(*) FROM question_bank q WHERE q.import_batch_id=b.id) AS remaining_count
                FROM question_import_batches b WHERE b.owner_telegram_id=%s ORDER BY b.id DESC LIMIT 25""",(update.effective_user.id,)); rows=await cur.fetchall()
        buttons=[]
        if not rows: text="No imports yet."
        else:
            lines=["📦 Import batches · newest first", "Batch name · date · original count · current count", ""]
            for r in rows:
                created=r['created_at'].strftime('%Y-%m-%d %H:%M UTC') if r['created_at'] else 'date unavailable'
                lines.append(f"#{r['id']} · {r['file_name']}\n{created} · Imported {r['imported_count']} / detected {r['total_detected']} · Currently linked {r['remaining_count']} · {r['status']}")
                if r.get('status') not in ('deleted','rolled_back') and int(r['remaining_count'] or 0)>0:
                    buttons.append([InlineKeyboardButton(f"🗑 Delete only batch #{r['id']} ({r['remaining_count']})",callback_data=f"qb:batchdeleteask:{r['id']}")])
            text="\n\n".join(lines)
        buttons.append([InlineKeyboardButton("↩ Question Bank menu",callback_data="qb:home")])
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith("qb:batchdeleteask:"):
        batch_id=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn:
            cur=await conn.execute("""SELECT b.id,b.file_name,b.created_at,b.imported_count,
                (SELECT COUNT(*) FROM question_bank q WHERE q.import_batch_id=b.id) AS remaining_count,
                (SELECT COUNT(*) FROM question_bank q WHERE q.import_batch_id=b.id AND EXISTS(SELECT 1 FROM bank_quiz_questions qq WHERE qq.question_id=q.id)) AS used_count
                FROM question_import_batches b WHERE b.id=%s AND b.owner_telegram_id=%s""",(batch_id,q.from_user.id)); batch=await cur.fetchone()
        if not batch:
            await q.edit_message_text("Batch not found or not owned by you.",reply_markup=admin_keyboard()); return
        await q.edit_message_text(f"⚠️ DELETE IMPORT BATCH #{batch_id}?\n\nName: {batch['file_name']}\nImported originally: {batch['imported_count']}\nStill linked to this batch: {batch['remaining_count']}\nQuestions used in quizzes: {batch['used_count']}\n\nOnly this batch will be affected. Questions already referenced by quizzes will be archived to preserve historical attempts; other questions in other batches will not be touched. This action cannot be undone.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Confirm delete this batch",callback_data=f"qb:batchdeleteconfirm:{batch_id}")],[InlineKeyboardButton("Cancel",callback_data="qb:history")]])); return
    if data.startswith("qb:batchdeleteconfirm:"):
        batch_id=int(data.rsplit(":",1)[1])
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT id FROM question_import_batches WHERE id=%s AND owner_telegram_id=%s AND status NOT IN ('deleted','rolled_back') FOR UPDATE",(batch_id,q.from_user.id)); batch=await cur.fetchone()
            if not batch:
                await q.edit_message_text("Batch not found, already deleted, or not owned by you.",reply_markup=admin_keyboard()); return
            cur=await conn.execute("""UPDATE question_bank qb SET status='archived',updated_at=NOW()
                WHERE qb.import_batch_id=%s AND EXISTS(SELECT 1 FROM bank_quiz_questions qq WHERE qq.question_id=qb.id) RETURNING qb.id""",(batch_id,)); archived=len(await cur.fetchall())
            cur=await conn.execute("""DELETE FROM question_bank qb WHERE qb.import_batch_id=%s
                AND NOT EXISTS(SELECT 1 FROM bank_quiz_questions qq WHERE qq.question_id=qb.id) RETURNING qb.id""",(batch_id,)); deleted=len(await cur.fetchall())
            await conn.execute("UPDATE question_import_batches SET status='deleted' WHERE id=%s",(batch_id,))
        await q.edit_message_text(f"✅ Batch #{batch_id} processed.\nPermanently removed: {deleted}\nArchived to preserve existing quiz/history references: {archived}\nNo questions from other batches were changed.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 View batches",callback_data="qb:history")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]])); return
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


async def show_question_list(q,pool,status="all",page=0):
    if pool is None:
        await q.edit_message_text("Database unavailable.",reply_markup=admin_keyboard()); return
    page=max(0,int(page)); limit=10; offset=page*limit
    async with pool.connection() as conn:
        if status=="all":
            cur=await conn.execute("SELECT id,question_en,subject,status FROM question_bank ORDER BY id DESC LIMIT %s OFFSET %s",(limit,offset)); rows=await cur.fetchall()
            cur=await conn.execute("SELECT COUNT(*) AS n FROM question_bank"); total=(await cur.fetchone())["n"]
        else:
            cur=await conn.execute("SELECT id,question_en,subject,status FROM question_bank WHERE status=%s ORDER BY id DESC LIMIT %s OFFSET %s",(status,limit,offset)); rows=await cur.fetchall()
            cur=await conn.execute("SELECT COUNT(*) AS n FROM question_bank WHERE status=%s",(status,)); total=(await cur.fetchone())["n"]
    lines=[f"🗃 Questions · {status.title()} · {total} total",f"Page {page+1} · showing {len(rows)}","",]
    if not rows: lines.append("No questions on this page.")
    buttons=[[InlineKeyboardButton(f"#{r['id']} · {r['question_en'][:38]}",callback_data=f"qb:detail:{r['id']}")] for r in rows]
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("⬅ Previous",callback_data=f"qb:liststatus:{status}:{page-1}"))
    if offset+len(rows)<total: nav.append(InlineKeyboardButton("Next ➡",callback_data=f"qb:liststatus:{status}:{page+1}"))
    if nav: buttons.append(nav)
    buttons += [[InlineKeyboardButton("🔎 Search",callback_data="qb:browse"),InlineKeyboardButton("🗑 Delete menu",callback_data="qb:deletehub")],[InlineKeyboardButton("⬅ Question Bank menu",callback_data="qb:home")]]
    await q.edit_message_text("\n".join(lines),reply_markup=InlineKeyboardMarkup(buttons))


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
    rows=[[InlineKeyboardButton("✏️ Edit",callback_data=f"qb:edit:{qid}"),InlineKeyboardButton("Archive",callback_data=f"qb:archive:{qid}")],[InlineKeyboardButton("🗑 Permanent delete…",callback_data=f"qb:purgeconfirm:{qid}")],[InlineKeyboardButton("Question deletion",callback_data="qb:deletehub")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]]
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


async def receive_poll(update: Update, context: ContextTypes.DEFAULT_TYPE, is_authorized: bool):
    """Import a Telegram quiz poll sent/forwarded by an authorized admin; always preview first."""
    message=update.effective_message; user=update.effective_user
    if not message or not user or not is_authorized or message.chat.type!=ChatType.PRIVATE or not message.poll:
        return False
    poll=message.poll
    opts={LETTERS[i]: option.text for i,option in enumerate(poll.options[:5]) if _norm(option.text)}
    correct=LETTERS[poll.correct_option_id] if poll.type=="quiz" and poll.correct_option_id is not None and poll.correct_option_id<len(LETTERS) else None
    record=_normalize_record({"question":poll.question,"options":opts,"correct_option":correct,
        "explanation":getattr(poll,"explanation",None),"subject":SUBJECTS[-1],"source":"Telegram quiz poll"},SUBJECTS[-1],"Telegram quiz poll",user.id)
    if not record:
        await message.reply_text("I couldn't read this poll as a multiple-choice question. Send a poll with at least two options.")
        return True
    context.user_data["qb_forward_preview"]=record
    warning="" if record["correct_option"] else "\n\n⚠️ This is a regular poll without a visible answer key, so it will be saved as pending and must be verified."
    await message.reply_text("📥 Telegram poll import preview\n\nThe poll has not been saved yet. Review the question and confirm below.\n\n"+format_question(record)+warning,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Save question",callback_data="qb:saveforward")],[InlineKeyboardButton("Discard",callback_data="qb:cancelforward")]]))
    return True


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
        await message.reply_text("File exceeds the 20 MB import limit."); return True
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
        # Register all source-defined subjects so admins can immediately build new test series from them.
        imported_subjects = sorted({str(item.get("subject") or preview["subject"]).strip() for item in records if str(item.get("subject") or preview["subject"]).strip()})
        async with pool.connection() as conn:
            for subject_name in imported_subjects:
                await conn.execute("INSERT INTO question_bank_subjects(name,created_by_telegram_id) VALUES(%s,%s) ON CONFLICT(name) DO NOTHING", (subject_name, owner_id))
                if subject_name not in SUBJECTS: SUBJECTS.append(subject_name)
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
    if step == "create_subject":
        name = _norm(msg.text)[:100]
        if len(name) < 2:
            await msg.reply_text("Subject name must contain at least 2 characters. Send the name again."); return True
        try:
            async with pool.connection() as conn:
                await conn.execute("INSERT INTO question_bank_subjects(name,created_by_telegram_id) VALUES(%s,%s) ON CONFLICT(name) DO NOTHING", (name,user.id))
            if name not in SUBJECTS: SUBJECTS.append(name)
            context.user_data.pop("qb_flow",None)
            await msg.reply_text(f"✅ Subject saved: {name}\n\nIt is now available for question imports and new test series.",reply_markup=admin_keyboard())
        except Exception as exc:
            log.exception("Subject creation failed")
            await msg.reply_text(f"Could not save this subject. No question data was changed. {str(exc)[:250]}",reply_markup=admin_keyboard())
        return True
    if step == "add_question_text":
        record=parse_forwarded_text(msg.text,user.id)
        if not record:
            await msg.reply_text("I couldn't parse that MCQ. Please include a question, options A–D, and an Answer: B line. No changes were made."); return True
        record["subject"]=flow.get("subject",SUBJECTS[-1])
        context.user_data["qb_forward_preview"]=record
        context.user_data.pop("qb_flow",None)
        await msg.reply_text("📝 Question preview — not saved yet\n\n"+format_question(record)+f"\\nSubject: {record['subject']}\\n\\nSave this question?",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Save question",callback_data="qb:saveforward")],[InlineKeyboardButton("✖ Discard",callback_data="qb:cancelforward")]])); return True
    if step == "bulk_delete_ids":
        import re as _re
        ids=sorted(set(int(x) for x in _re.findall(r"\\d+",msg.text)))
        if not ids:
            await msg.reply_text("No numeric question IDs found. Send IDs separated by commas or spaces, or cancel."); return True
        if len(ids)>100:
            await msg.reply_text("Please process at most 100 IDs per batch."); return True
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT id,question_en,status FROM question_bank WHERE id = ANY(%s) ORDER BY id",(ids,)); found=await cur.fetchall()
        context.user_data["qb_bulk_delete_ids"]=[int(r["id"]) for r in found]
        if not found:
            await msg.reply_text("None of those IDs exist in the question bank.",reply_markup=admin_keyboard()); context.user_data.pop("qb_flow",None); return True
        preview="\\n".join(f"#{r['id']} [{r['status']}] {r['question_en'][:85]}" for r in found[:20])
        extra=f"\\n…and {len(found)-20} more" if len(found)>20 else ""
        await msg.reply_text(f"⚠️ Confirm bulk deletion of {len(found)} question(s)?\\n\\n{preview}{extra}\\n\\nQuiz links and answer-level attempt rows for these questions will be removed; aggregate attempt totals remain. This cannot be undone.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Confirm delete",callback_data="qb:bulkdeleteconfirm")],[InlineKeyboardButton("Cancel",callback_data="qb:home")]])); return True
    if step in {"edit_quiz_title","edit_quiz_duration","edit_quiz_marks"}:
        quiz_id=int(flow["quiz_id"])
        try:
            if step=="edit_quiz_title":
                value=_norm(msg.text)[:160]
                if not value: raise ValueError("Title cannot be empty.")
                async with pool.connection() as conn:
                    await conn.execute("UPDATE bank_quizzes SET title=%s WHERE id=%s",(value,quiz_id))
                result=f"Title updated to: {value}"
            elif step=="edit_quiz_duration":
                value=int(msg.text.strip())
                if not 1<=value<=360: raise ValueError("Duration must be between 1 and 360 minutes.")
                async with pool.connection() as conn:
                    await conn.execute("UPDATE bank_quizzes SET duration_minutes=%s WHERE id=%s",(value,quiz_id))
                result=f"Duration updated to {value} minutes."
            else:
                parts=[float(x.strip()) for x in msg.text.split(",")]
                if len(parts)!=2 or not (0.1<=parts[0]<=10) or not (0<=parts[1]<=5): raise ValueError("Use positive,negative. Positive must be 0.1–10 and negative 0–5.")
                async with pool.connection() as conn:
                    await conn.execute("UPDATE bank_quizzes SET positive_marks=%s,negative_marks=%s WHERE id=%s",(parts[0],parts[1],quiz_id))
                result=f"Scoring updated: +{parts[0]} / −{parts[1]}"
            context.user_data.pop("qb_flow",None)
            await msg.reply_text(result,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open quiz settings",callback_data=f"qb:manageview:{quiz_id}")],[InlineKeyboardButton("Quiz Manager",callback_data="qb:manage")]]))
        except ValueError as e:
            await msg.reply_text(str(e) or "Invalid value. Please try again.")
        return True
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
    if step=="add_quiz_count":
        quiz_id=int(flow["quiz_id"])
        try: count=int(msg.text.strip())
        except ValueError: await msg.reply_text("Send a whole number from 1 to 100."); return True
        if not 1<=count<=100: await msg.reply_text("Question count must be from 1 to 100."); return True
        subject=flow.get("subject")
        async with pool.connection() as conn:
            cond="q.status='ready' AND NOT EXISTS (SELECT 1 FROM bank_quiz_questions old WHERE old.quiz_id=%s AND old.question_id=q.id)"
            params=[quiz_id]
            if subject:
                cond += " AND q.subject=%s"; params.append(subject)
            cur=await conn.execute("SELECT COUNT(*) AS n FROM question_bank q WHERE "+cond,tuple(params)); eligible=int((await cur.fetchone())["n"])
            if eligible<count:
                await msg.reply_text(f"Only {eligible} eligible questions match this pool. Send a smaller count."); return True
            cur=await conn.execute("SELECT COALESCE(MAX(position),0) AS m FROM bank_quiz_questions WHERE quiz_id=%s",(quiz_id,)); position=int((await cur.fetchone())["m"])
            cur=await conn.execute("SELECT q.id FROM question_bank q WHERE "+cond+" ORDER BY RANDOM() LIMIT %s",tuple(params+[count])); ids=[int(r["id"]) for r in await cur.fetchall()]
            for qid in ids:
                position+=1
                await conn.execute("INSERT INTO bank_quiz_questions(quiz_id,position,question_id) VALUES(%s,%s,%s)",(quiz_id,position,qid))
        context.user_data.pop("qb_flow",None)
        await msg.reply_text(f"Added {len(ids)} questions to quiz #{quiz_id}.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("View question set",callback_data=f"qb:managequestions:{quiz_id}")],[InlineKeyboardButton("Quiz settings",callback_data=f"qb:manageview:{quiz_id}")]])); return True
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
    username=getattr(context.bot,"username",None)
    share=f"https://t.me/{username}?start=quiz_{quiz_id}" if username else "Open /quiz in a private chat with the bot."
    await q.edit_message_text(f"✅ Quiz published for students.\n\n#{quiz_id} · {quiz['title']}\nSubject: {quiz['subject']}\nQuestions: {n}\n\nShare this quiz link:\n{share}\n\nStudents can tap the link to open this exact quiz.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Manage quiz",callback_data=f"qb:manageview:{quiz_id}")],[InlineKeyboardButton("Question Bank menu",callback_data="qb:home")]]),disable_web_page_preview=True)


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
        cur=await conn.execute("UPDATE bank_quiz_attempts SET submitted_at=NOW(),correct_count=%s,incorrect_count=%s,skipped_count=%s,score=%s WHERE id=%s AND submitted_at IS NULL RETURNING id",(correct,incorrect,skipped,round(score,2),attempt_id))
        if await cur.fetchone(): await lb.record_bank_attempt(conn,attempt_id,user_id,"Student")
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
        cur=await conn.execute("UPDATE bank_quiz_attempts SET submitted_at=NOW(),correct_count=%s,incorrect_count=%s,skipped_count=%s,score=%s WHERE id=%s AND submitted_at IS NULL RETURNING id",(correct,incorrect,skipped,round(score,2),a["attempt_id"]))
        if await cur.fetchone(): await lb.record_bank_attempt(conn,a["attempt_id"],q.from_user.id,q.from_user.first_name or "Student")
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
        await q.answer(); await q.edit_message_text(f"Question saved. Added: {result['imported']}; duplicates skipped: {result['duplicates']}; pending verification: {result['pending']}.",reply_markup=admin_keyboard()); return True
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
