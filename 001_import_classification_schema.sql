-- BPSC Nexus Mini App: import classification foundation
-- Idempotent PostgreSQL migration. question_bank.ensure_schema() also applies these additions.

CREATE TABLE IF NOT EXISTS exam_catalog (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  normalized_name TEXT NOT NULL UNIQUE,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_by_telegram_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS taxonomy_nodes (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  parent_id BIGINT REFERENCES taxonomy_nodes(id) ON DELETE RESTRICT,
  exam_id BIGINT REFERENCES exam_catalog(id) ON DELETE RESTRICT,
  node_type TEXT NOT NULL CHECK (node_type IN ('exam','subject','category','chapter','topic','subtopic')),
  name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_by_telegram_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_parent_type ON taxonomy_nodes(parent_id,node_type,is_active);
CREATE INDEX IF NOT EXISTS idx_taxonomy_normalized ON taxonomy_nodes(normalized_name);

ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS classification_original JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS classification_status TEXT NOT NULL DEFAULT 'unresolved';
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS classification_node_id BIGINT REFERENCES taxonomy_nodes(id) ON DELETE SET NULL;
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE question_bank ADD COLUMN IF NOT EXISTS source_page TEXT;
CREATE INDEX IF NOT EXISTS idx_question_bank_classification_status ON question_bank(classification_status,status);

CREATE TABLE IF NOT EXISTS question_classifications (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  question_id BIGINT NOT NULL REFERENCES question_bank(id) ON DELETE CASCADE,
  taxonomy_node_id BIGINT NOT NULL REFERENCES taxonomy_nodes(id) ON DELETE RESTRICT,
  is_primary BOOLEAN NOT NULL DEFAULT TRUE,
  approval_status TEXT NOT NULL DEFAULT 'approved' CHECK (approval_status IN ('suggested','approved','unresolved')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(question_id,taxonomy_node_id)
);

CREATE TABLE IF NOT EXISTS question_sources (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  question_id BIGINT NOT NULL REFERENCES question_bank(id) ON DELETE CASCADE,
  import_batch_id BIGINT REFERENCES question_import_batches(id) ON DELETE SET NULL,
  source_file TEXT,
  source_page TEXT,
  source_name TEXT,
  source_url TEXT,
  attribution TEXT,
  original_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_question_sources_question ON question_sources(question_id);

CREATE TABLE IF NOT EXISTS question_import_mappings (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_id BIGINT NOT NULL REFERENCES question_import_batches(id) ON DELETE CASCADE,
  original_path TEXT NOT NULL,
  original_label TEXT NOT NULL,
  suggested_node_id BIGINT REFERENCES taxonomy_nodes(id) ON DELETE SET NULL,
  match_method TEXT,
  confidence NUMERIC(5,4),
  approval_status TEXT NOT NULL DEFAULT 'unresolved' CHECK (approval_status IN ('suggested','approved','unresolved','rejected')),
  approved_by_telegram_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_import_mappings_batch ON question_import_mappings(batch_id,approval_status);

CREATE TABLE IF NOT EXISTS question_classification_reviews (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  question_id BIGINT REFERENCES question_bank(id) ON DELETE CASCADE,
  batch_id BIGINT REFERENCES question_import_batches(id) ON DELETE CASCADE,
  original_classification JSONB NOT NULL DEFAULT '{}'::jsonb,
  reason TEXT NOT NULL DEFAULT 'unresolved_classification',
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','dismissed')),
  resolved_by_telegram_id BIGINT,
  resolved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
