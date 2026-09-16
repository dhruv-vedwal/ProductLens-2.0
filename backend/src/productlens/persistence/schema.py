"""Database bootstrap schema for the compatibility repository.

Keeping DDL separate from query code makes the persistence boundary readable and
allows migrations to own forward-only upgrades.
"""

SCHEMA_SQL = """            CREATE TABLE IF NOT EXISTS demo_requests (
              id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, url TEXT NOT NULL,
              objective TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
              project_id TEXT
            );
            CREATE TABLE IF NOT EXISTS demo_runs (
              id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES demo_requests(id),
              stage TEXT NOT NULL, status TEXT NOT NULL, artifact_root TEXT NOT NULL,
              error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS demo_attempts (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), ordinal INTEGER NOT NULL,
              stage TEXT NOT NULL, status TEXT NOT NULL, failure_code TEXT, created_at TEXT NOT NULL,
              UNIQUE(run_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS product_knowledge (
              id TEXT PRIMARY KEY, product_key TEXT UNIQUE NOT NULL, version INTEGER NOT NULL,
              evidence_json TEXT NOT NULL, confidence REAL NOT NULL, last_verified_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_versions (
              id TEXT PRIMARY KEY, product_key TEXT NOT NULL, version INTEGER NOT NULL,
              fingerprint TEXT NOT NULL, evidence_json TEXT NOT NULL, confidence REAL NOT NULL,
              captured_at TEXT NOT NULL, UNIQUE(product_key, version)
            );
            CREATE TABLE IF NOT EXISTS demo_plans (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_steps (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), ordinal INTEGER NOT NULL, payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS browser_sessions (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), provider TEXT NOT NULL, external_session_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS run_lineage (parent_run_id TEXT NOT NULL REFERENCES demo_runs(id), retry_run_id TEXT PRIMARY KEY REFERENCES demo_runs(id), created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS interaction_events (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), ordinal INTEGER NOT NULL, payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS presentation_plans (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS narration_scripts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quality_reports (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), kind TEXT NOT NULL, location TEXT NOT NULL, created_at TEXT NOT NULL);
            -- A compact, queryable mirror of the architectural JSON evidence.
            -- Files remain the immutable renderer inputs; this ledger makes a
            -- crashed/resumed run inspectable without scanning its directory.
            CREATE TABLE IF NOT EXISTS run_artifact_documents (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              kind TEXT NOT NULL, payload_json TEXT NOT NULL, source_path TEXT NOT NULL,
              sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, UNIQUE(run_id, kind)
            );
            CREATE TABLE IF NOT EXISTS provider_configs (
              id TEXT PRIMARY KEY, provider_type TEXT NOT NULL, name TEXT NOT NULL,
              credential_reference TEXT, active INTEGER NOT NULL, priority INTEGER NOT NULL,
              settings_json TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(provider_type, name)
            );
            CREATE TABLE IF NOT EXISTS generation_jobs (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES demo_runs(id),
              kind TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
              delivery_attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT,
              queued_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS generation_stage_jobs (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              stage TEXT NOT NULL, ordinal INTEGER NOT NULL, status TEXT NOT NULL,
              error_code TEXT, delivery_attempts INTEGER NOT NULL DEFAULT 0, claimed_at TEXT,
              started_at TEXT, completed_at TEXT, heartbeat_at TEXT, updated_at TEXT NOT NULL,
              UNIQUE(run_id, stage), UNIQUE(run_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, display_name TEXT, password_hash TEXT,
              theme_preference TEXT NOT NULL DEFAULT 'system', created_at TEXT NOT NULL, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL,
              revoked_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id), name TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS page_knowledge (
              id TEXT PRIMARY KEY, product_knowledge_id TEXT NOT NULL REFERENCES product_knowledge(id),
              url TEXT NOT NULL, evidence_json TEXT NOT NULL, confidence REAL NOT NULL, last_verified_at TEXT NOT NULL,
              UNIQUE(product_knowledge_id, url)
            );
            CREATE TABLE IF NOT EXISTS understanding_previews (
              id TEXT PRIMARY KEY, product_key TEXT NOT NULL, prompt_hash TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(product_key, prompt_hash)
            );
            CREATE TABLE IF NOT EXISTS form_schemas (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS synthetic_datasets (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audio_assets (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              location TEXT NOT NULL, duration_seconds REAL, provider TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS video_renders (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              location TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS provider_calls (
              id TEXT PRIMARY KEY, run_id TEXT REFERENCES demo_runs(id), provider TEXT NOT NULL,
              operation TEXT NOT NULL, status TEXT NOT NULL, duration_ms INTEGER, error_code TEXT,
              model TEXT, cost_class TEXT, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_demo_runs_created_at ON demo_runs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_demo_runs_request_id ON demo_runs(request_id);
            CREATE INDEX IF NOT EXISTS idx_demo_requests_project_id ON demo_requests(project_id);
            CREATE INDEX IF NOT EXISTS idx_demo_attempts_run_id ON demo_attempts(run_id, ordinal);
            CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_run_artifact_documents_run_id ON run_artifact_documents(run_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_generation_jobs_status ON generation_jobs(status, queued_at);
            CREATE INDEX IF NOT EXISTS idx_provider_calls_run_id ON provider_calls(run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_knowledge_versions_key ON knowledge_versions(product_key, version DESC);
            CREATE INDEX IF NOT EXISTS idx_projects_owner_id ON projects(owner_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions(user_id, expires_at);
"""
