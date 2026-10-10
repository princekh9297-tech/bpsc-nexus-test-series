-- Run this migration in your dedicated BPSC Nexus Neon project.
-- Safe to run more than once.
-- Do not run against the database of another application.

ALTER TABLE tests
    ADD COLUMN IF NOT EXISTS is_premium BOOLEAN NOT NULL DEFAULT FALSE;

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

CREATE INDEX IF NOT EXISTS idx_premium_passes_user
    ON premium_passes(user_id);

CREATE INDEX IF NOT EXISTS idx_premium_passes_expiry
    ON premium_passes(expires_at)
    WHERE revoked_at IS NULL;
