-- BPSC Nexus next-update additions.
-- miniapp.py also creates these objects idempotently during startup.
CREATE TABLE IF NOT EXISTS bpsc_nexus_quix_posts (
  id BIGSERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'quick_revision',
  duration_label TEXT NOT NULL DEFAULT 'QUICK READ',
  quiz_id BIGINT REFERENCES bank_quizzes(id) ON DELETE SET NULL,
  is_published BOOLEAN NOT NULL DEFAULT FALSE,
  published_at TIMESTAMPTZ,
  created_by_telegram_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS bpsc_nexus_quix_feed_idx
  ON bpsc_nexus_quix_posts(is_published,published_at DESC,id DESC);

ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS classification_confidence NUMERIC(5,4) NOT NULL DEFAULT 0.5;
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS classification_source TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS classification_review_required BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS original_question_type TEXT;
UPDATE question_bank SET original_question_type=question_type WHERE original_question_type IS NULL;
UPDATE question_bank
SET classification_confidence=CASE WHEN question_type IN ('standard','statement_based','assertion_reason','match_following','pair_based','chronology','passage_based','image_based','table_based','numerical','multiple_correct','incorrect_pair','odd_one_out','sequence','data_interpretation') THEN 0.88 ELSE 0.42 END,
    classification_source=CASE WHEN question_type IS NULL OR question_type IN ('unknown','') THEN 'needs_review' ELSE 'rule_based' END,
    classification_review_required=CASE WHEN question_type IS NULL OR question_type IN ('unknown','') THEN TRUE ELSE FALSE END
WHERE classification_source='legacy';
