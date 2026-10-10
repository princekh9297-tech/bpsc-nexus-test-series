-- Reference only: bot.py applies equivalent idempotent statements at startup.
-- Run only against the dedicated BPSC Nexus Neon database if manual recovery is needed.
CREATE TABLE IF NOT EXISTS premium_passes (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  granted_by_telegram_id BIGINT NOT NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  revoked_by_telegram_id BIGINT,
  note TEXT,
  CHECK (expires_at IS NULL OR expires_at > granted_at),
  CHECK ((revoked_at IS NULL AND revoked_by_telegram_id IS NULL) OR (revoked_at IS NOT NULL AND revoked_by_telegram_id IS NOT NULL))
);
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS first_name TEXT;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE tests ADD COLUMN IF NOT EXISTS is_premium BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tests ADD COLUMN IF NOT EXISTS vault_key TEXT NOT NULL DEFAULT 'general';
ALTER TABLE tests ADD COLUMN IF NOT EXISTS created_by_telegram_id BIGINT;
ALTER TABLE tests ADD COLUMN IF NOT EXISTS creator_chat_id BIGINT;
ALTER TABLE tests ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'catalogue';
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS granted_by_telegram_id BIGINT;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS revoked_by_telegram_id BIGINT;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS pass_code TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE premium_passes ADD COLUMN IF NOT EXISTS vault_keys TEXT[] NOT NULL DEFAULT ARRAY['general']::TEXT[];
CREATE INDEX IF NOT EXISTS idx_premium_passes_user ON premium_passes(user_id);
CREATE INDEX IF NOT EXISTS idx_premium_passes_expiry ON premium_passes(expires_at) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tests_vault_key ON tests(vault_key);
CREATE TABLE IF NOT EXISTS quiz_creator_drafts (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, owner_telegram_id BIGINT NOT NULL,
 source_chat_id BIGINT NOT NULL, title TEXT NOT NULL DEFAULT 'My Quiz', source_name TEXT,
 questions JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','discarded')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_quiz_creator_drafts_owner ON quiz_creator_drafts(owner_telegram_id, created_at DESC);
CREATE TABLE IF NOT EXISTS quiz_creator_usage (
 owner_telegram_id BIGINT NOT NULL, usage_day DATE NOT NULL DEFAULT CURRENT_DATE,
 quiz_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner_telegram_id, usage_day)
);
