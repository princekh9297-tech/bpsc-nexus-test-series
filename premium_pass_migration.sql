-- Run in the dedicated BPSC Nexus Neon project only.
-- Safe to run repeatedly. Does not drop or recreate existing tables/data.

ALTER TABLE tests
    ADD COLUMN IF NOT EXISTS is_premium BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE tests
    ADD COLUMN IF NOT EXISTS vault_key TEXT NOT NULL DEFAULT 'general';

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
    CHECK (
        (revoked_at IS NULL AND revoked_by_telegram_id IS NULL)
        OR
        (revoked_at IS NOT NULL AND revoked_by_telegram_id IS NOT NULL)
    )
);

ALTER TABLE premium_passes
    ADD COLUMN IF NOT EXISTS pass_code TEXT NOT NULL DEFAULT 'legacy';

ALTER TABLE premium_passes
    ADD COLUMN IF NOT EXISTS vault_keys TEXT[] NOT NULL DEFAULT ARRAY['general']::TEXT[];

CREATE INDEX IF NOT EXISTS idx_premium_passes_user
    ON premium_passes(user_id);

CREATE INDEX IF NOT EXISTS idx_premium_passes_expiry
    ON premium_passes(expires_at)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tests_vault_key
    ON tests(vault_key);

-- IMPORTANT: after migration, classify each test into its vault by setting tests.vault_key to one of:
-- 'bpsc_pyq', 'ghatnachakra_pyq', 'upsc_pyq', or another documented vault key.
-- The current default 'general' is intentional to avoid guessing which vault existing tests belong to.
