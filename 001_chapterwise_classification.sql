-- BPSC Nexus chapter/topic classification columns.
-- Idempotent: safe to execute more than once.
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS exam TEXT;
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS chapter TEXT;
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS subtopic TEXT;
CREATE INDEX IF NOT EXISTS idx_question_bank_subject_chapter_topic
    ON question_bank(subject, chapter, topic, subtopic, status);
-- Duplicate question records are intentionally allowed; retain hash for reporting/search.
DROP INDEX IF EXISTS uq_question_bank_hash;
CREATE INDEX IF NOT EXISTS idx_question_bank_hash ON question_bank(question_hash);

-- Saved import mapping profiles (admin-owned).
CREATE TABLE IF NOT EXISTS question_import_mapping_profiles (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_telegram_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    mapping JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(owner_telegram_id, name)
);
