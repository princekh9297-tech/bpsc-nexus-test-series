-- Run only in the dedicated BPSC Nexus Neon project.
-- If these tables already exist, do not rerun unnecessarily.

CREATE TABLE IF NOT EXISTS app_users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    first_name TEXT,
    username TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    test_type TEXT NOT NULL DEFAULT 'mock'
        CHECK (test_type IN ('pyq', 'subject', 'mock')),
    duration_minutes INTEGER NOT NULL DEFAULT 120 CHECK (duration_minutes > 0),
    total_marks NUMERIC(8,2) NOT NULL DEFAULT 150,
    marks_per_correct NUMERIC(6,2) NOT NULL DEFAULT 1,
    negative_marks NUMERIC(6,2) NOT NULL DEFAULT 0,
    is_published BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS questions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject TEXT NOT NULL,
    question_en TEXT NOT NULL,
    question_hi TEXT NOT NULL,
    options JSONB NOT NULL CHECK (jsonb_typeof(options) = 'object'),
    correct_option CHAR(1) NOT NULL CHECK (correct_option IN ('A','B','C','D','E')),
    explanation_en TEXT,
    explanation_hi TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS test_questions (
    test_id BIGINT NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
    question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK (position > 0),
    PRIMARY KEY (test_id, question_id),
    UNIQUE (test_id, position)
);

CREATE TABLE IF NOT EXISTS attempts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    test_id BIGINT NOT NULL REFERENCES tests(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'in_progress'
        CHECK (status IN ('in_progress', 'submitted', 'expired')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at TIMESTAMPTZ,
    score NUMERIC(10,2),
    correct_count INTEGER NOT NULL DEFAULT 0,
    incorrect_count INTEGER NOT NULL DEFAULT 0,
    unattempted_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS attempt_answers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id BIGINT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE RESTRICT,
    selected_option CHAR(1) CHECK (selected_option IN ('A','B','C','D','E')),
    is_correct BOOLEAN,
    answered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (attempt_id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_test ON attempts(test_id);
CREATE INDEX IF NOT EXISTS idx_test_questions_order ON test_questions(test_id, position);
CREATE INDEX IF NOT EXISTS idx_questions_subject ON questions(subject);
CREATE INDEX IF NOT EXISTS idx_answers_attempt ON attempt_answers(attempt_id);
