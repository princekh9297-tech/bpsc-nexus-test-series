-- BPSC Nexus Student Intelligence migration
-- Safe to run repeatedly on the existing Neon PostgreSQL database.
CREATE TABLE IF NOT EXISTS app_users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    first_name TEXT,
    username TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS last_name TEXT;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS photo_url TEXT;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS language_code TEXT;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS bpsc_student_login_history (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    session_key TEXT NOT NULL,
    first_name TEXT,
    username TEXT,
    login_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_agent TEXT,
    UNIQUE (telegram_id, session_key)
);
CREATE INDEX IF NOT EXISTS bpsc_student_login_tid_idx
    ON bpsc_student_login_history (telegram_id, login_at DESC);

CREATE TABLE IF NOT EXISTS bpsc_student_activity (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    event_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS bpsc_student_activity_tid_idx
    ON bpsc_student_activity (telegram_id, created_at DESC);
