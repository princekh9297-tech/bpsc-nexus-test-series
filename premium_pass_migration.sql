-- BPSC Nexus Premium Pass compatibility migration.
-- Run only against the dedicated BPSC Nexus Neon database if startup migration cannot run.
-- This is idempotent and does not drop existing pass records.

CREATE TABLE IF NOT EXISTS pass_catalogue (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pass_code TEXT NOT NULL,
  pass_name TEXT NOT NULL,
  description TEXT,
  is_active BOOLEAN NOT NULL DEFAULT TRUE
);
ALTER TABLE pass_catalogue ADD COLUMN IF NOT EXISTS pass_code TEXT;
ALTER TABLE pass_catalogue ADD COLUMN IF NOT EXISTS pass_name TEXT;
ALTER TABLE pass_catalogue ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE pass_catalogue ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS uq_pass_catalogue_pass_code ON pass_catalogue(pass_code);

INSERT INTO pass_catalogue (pass_code, pass_name, description, is_active) VALUES
 ('bpsc_pyq', 'BPSC PYQ Pass', 'Access to the BPSC PYQ vault', TRUE),
 ('ghatnachakra_pyq', 'Ghatnachakra PYQ Pass', 'Access to the Ghatnachakra PYQ vault', TRUE),
 ('upsc_pyq', 'UPSC PYQ Pass', 'Access to the UPSC PYQ vault', TRUE),
 ('dual_vault', 'Dual Vault Pass', 'Access to two selected vaults', TRUE),
 ('all_access', 'All-Access Pass', 'Access to all supported vaults', TRUE),
 ('legacy', 'Legacy Premium Pass', 'Migrated legacy entitlement', TRUE)
ON CONFLICT (pass_code) DO NOTHING;

CREATE TABLE IF NOT EXISTS premium_passes (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  pass_id BIGINT NOT NULL REFERENCES pass_catalogue(id),
  granted_by_telegram_id BIGINT NOT NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  revoked_by_telegram_id BIGINT,
  note TEXT,
  pass_code TEXT NOT NULL DEFAULT 'legacy',
  vault_keys TEXT[] NOT NULL DEFAULT ARRAY['general']::TEXT[],
  CHECK (expires_at IS NULL OR expires_at > granted_at),
  CHECK ((revoked_at IS NULL AND revoked_by_telegram_id IS NULL) OR
         (revoked_at IS NOT NULL AND revoked_by_telegram_id IS NOT NULL))
);

ALTER TABLE app_users ADD COLUMN IF NOT EXISTS first_name TEXT;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE tests ADD COLUMN IF NOT EXISTS is_premium BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tests ADD COLUMN IF NOT EXISTS vault_key TEXT NOT NULL DEFAULT 'general';
ALTER TABLE tests ADD COLUMN IF NOT EXISTS created_by_telegram_id BIGINT;
ALTER TABLE tests ADD COLUMN IF NOT EXISTS creator_chat_id BIGINT;
ALTER TABLE tests ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'catalogue';

ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS pass_id BIGINT;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS granted_by_telegram_id BIGINT;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS revoked_by_telegram_id BIGINT;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS pass_code TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS vault_keys TEXT[] NOT NULL DEFAULT ARRAY['general']::TEXT[];

INSERT INTO pass_catalogue (pass_code, pass_name, description, is_active)
SELECT DISTINCT pp.pass_code, INITCAP(REPLACE(pp.pass_code, '_', ' ')),
       'Migrated existing pass type', TRUE
FROM premium_passes pp
WHERE pp.pass_code IS NOT NULL
ON CONFLICT (pass_code) DO NOTHING;

UPDATE premium_passes pp
SET pass_id = pc.id
FROM pass_catalogue pc
WHERE pp.pass_id IS NULL AND pc.pass_code = pp.pass_code;

UPDATE premium_passes pp
SET pass_id = pc.id
FROM pass_catalogue pc
WHERE pp.pass_id IS NULL AND pc.pass_code = 'legacy';

ALTER TABLE premium_passes ALTER COLUMN pass_id SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'premium_passes_pass_id_fkey'
      AND conrelid = 'premium_passes'::regclass
  ) THEN
    ALTER TABLE premium_passes
      ADD CONSTRAINT premium_passes_pass_id_fkey
      FOREIGN KEY (pass_id) REFERENCES pass_catalogue(id);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_premium_passes_user ON premium_passes(user_id);
CREATE INDEX IF NOT EXISTS idx_premium_passes_expiry ON premium_passes(expires_at) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tests_vault_key ON tests(vault_key);

CREATE TABLE IF NOT EXISTS quiz_creator_drafts (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_telegram_id BIGINT NOT NULL,
  source_chat_id BIGINT NOT NULL,
  title TEXT NOT NULL DEFAULT 'My Quiz',
  source_name TEXT,
  questions JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','discarded')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_quiz_creator_drafts_owner ON quiz_creator_drafts(owner_telegram_id, created_at DESC);
CREATE TABLE IF NOT EXISTS quiz_creator_usage (
  owner_telegram_id BIGINT NOT NULL,
  usage_day DATE NOT NULL DEFAULT CURRENT_DATE,
  quiz_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(owner_telegram_id, usage_day)
);


-- Question Bank + Quiz Delivery migration (also applied automatically by bot.py).
CREATE TABLE IF NOT EXISTS question_import_batches (
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
    );
CREATE TABLE IF NOT EXISTS question_bank (
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
    );
CREATE UNIQUE INDEX IF NOT EXISTS uq_question_bank_hash ON question_bank(question_hash);
CREATE INDEX IF NOT EXISTS idx_question_bank_subject_status ON question_bank(subject, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_question_bank_batch ON question_bank(import_batch_id);
CREATE TABLE IF NOT EXISTS bank_quizzes (
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
    );
CREATE TABLE IF NOT EXISTS bank_quiz_questions (
        quiz_id BIGINT NOT NULL REFERENCES bank_quizzes(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        question_id BIGINT NOT NULL REFERENCES question_bank(id) ON DELETE RESTRICT,
        PRIMARY KEY (quiz_id, position),
        UNIQUE (quiz_id, question_id)
    );
CREATE TABLE IF NOT EXISTS bank_quiz_attempts (
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
    );
CREATE TABLE IF NOT EXISTS bank_quiz_attempt_answers (
        attempt_id BIGINT NOT NULL REFERENCES bank_quiz_attempts(id) ON DELETE CASCADE,
        question_id BIGINT NOT NULL REFERENCES question_bank(id) ON DELETE RESTRICT,
        selected_option TEXT,
        is_correct BOOLEAN,
        PRIMARY KEY (attempt_id, question_id)
    );
CREATE INDEX IF NOT EXISTS idx_bank_quizzes_published ON bank_quizzes(is_published, id DESC);
CREATE INDEX IF NOT EXISTS idx_bank_attempts_user ON bank_quiz_attempts(telegram_id, started_at DESC);
