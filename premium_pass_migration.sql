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
