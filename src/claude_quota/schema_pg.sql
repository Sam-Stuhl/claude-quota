-- Postgres schema (used when DATABASE_URL is set). Mirrors schema.sql, with
-- Postgres types and no SQLite-only constructs (WITHOUT ROWID, PRAGMAs). The
-- usage_bucket key columns are NOT NULL DEFAULT '' so the composite primary key
-- and ON CONFLICT upsert work the same way the SQLite side does.
CREATE TABLE IF NOT EXISTS quota_sample (
  id             BIGSERIAL PRIMARY KEY,
  ts             BIGINT NOT NULL,
  session_id     TEXT NOT NULL,
  device         TEXT,
  five_h_pct     DOUBLE PRECISION,
  five_h_reset   BIGINT,
  seven_d_pct    DOUBLE PRECISION,
  seven_d_reset  BIGINT,
  cc_version     TEXT,
  had_limits     INTEGER NOT NULL,
  model_id       TEXT,
  cost_usd_total DOUBLE PRECISION,
  context_pct    DOUBLE PRECISION,
  lines_added    INTEGER,
  lines_removed  INTEGER,
  exceeds_200k   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_quota_ts ON quota_sample(ts);

CREATE TABLE IF NOT EXISTS raw_statusline (
  id         BIGSERIAL PRIMARY KEY,
  ts         BIGINT NOT NULL,
  session_id TEXT,
  device     TEXT,
  payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_statusline(ts);

CREATE TABLE IF NOT EXISTS session (
  session_id   TEXT PRIMARY KEY,
  first_seen   BIGINT NOT NULL,
  last_seen    BIGINT NOT NULL,
  device       TEXT,
  cwd          TEXT,
  project_dir  TEXT,
  git_worktree TEXT,
  entrypoint   TEXT,
  start_type   TEXT,
  color_idx    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_bucket (
  ts           BIGINT NOT NULL,
  session_id   TEXT NOT NULL,
  model        TEXT NOT NULL,
  query_source TEXT NOT NULL DEFAULT '',
  agent_name   TEXT NOT NULL DEFAULT '',
  skill_name   TEXT NOT NULL DEFAULT '',
  mcp_server   TEXT NOT NULL DEFAULT '',
  plugin_name  TEXT NOT NULL DEFAULT '',
  effort       TEXT NOT NULL DEFAULT '',
  cost_usd     DOUBLE PRECISION NOT NULL DEFAULT 0,
  tok_input    BIGINT NOT NULL DEFAULT 0,
  tok_output   BIGINT NOT NULL DEFAULT 0,
  tok_cache_r  BIGINT NOT NULL DEFAULT 0,
  tok_cache_w  BIGINT NOT NULL DEFAULT 0,
  active_ms    BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (ts, session_id, model, query_source, agent_name, skill_name,
               mcp_server, plugin_name, effort)
);
CREATE INDEX IF NOT EXISTS idx_bucket_ts ON usage_bucket(ts);

CREATE TABLE IF NOT EXISTS calibration (
  model       TEXT PRIMARY KEY,
  pct_per_usd DOUBLE PRECISION NOT NULL,
  r2          DOUBLE PRECISION,
  n_samples   INTEGER NOT NULL,
  updated_at  BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS window_history (
  reset_at      BIGINT PRIMARY KEY,
  peak_pct      DOUBLE PRECISION NOT NULL,
  hit_cap       INTEGER NOT NULL,
  total_cost    DOUBLE PRECISION NOT NULL,
  session_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
  ts BIGINT NOT NULL, session_id TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event_log(ts);
