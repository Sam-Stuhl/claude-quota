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
  name         TEXT,
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

-- Attribution, from transcripts shipped by `claude-quota ship` (one row per
-- API request). This is the record of where usage went. The OTel buckets above
-- stay the live, 10-second view.
CREATE TABLE IF NOT EXISTS req_usage (
  request_id    TEXT PRIMARY KEY,
  ts            BIGINT NOT NULL,
  device        TEXT,
  session_id    TEXT NOT NULL,
  agent_id      TEXT,                       -- set for subagent requests
  prompt_id     TEXT,                       -- the turn this request belongs to
  model         TEXT,
  query_source  TEXT,
  agent_type    TEXT,
  skill         TEXT,
  mcp_server    TEXT,
  mcp_tool      TEXT,
  effort        TEXT,
  speed         TEXT,
  tok_input     BIGINT NOT NULL DEFAULT 0,
  tok_output    BIGINT NOT NULL DEFAULT 0,
  tok_cache_r   BIGINT NOT NULL DEFAULT 0,
  tok_cache_w   BIGINT NOT NULL DEFAULT 0,
  cost_usd      DOUBLE PRECISION NOT NULL DEFAULT 0     -- list price, see pricing.py
);
CREATE INDEX IF NOT EXISTS idx_req_ts ON req_usage(ts);
CREATE INDEX IF NOT EXISTS idx_req_session ON req_usage(session_id);

-- One row per Claude session seen in transcripts.
CREATE TABLE IF NOT EXISTS conv (
  session_id    TEXT PRIMARY KEY,
  device        TEXT,
  source        TEXT,                       -- code | cowork
  cwd           TEXT,
  entrypoint    TEXT,
  git_branch    TEXT,
  cc_version    TEXT,
  title         TEXT,
  first_prompt  TEXT,
  first_ts      BIGINT,
  last_ts       BIGINT
);

-- One row per prompt (turn), with a short label.
CREATE TABLE IF NOT EXISTS turn (
  prompt_id     TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,
  agent_id      TEXT,
  ts            BIGINT,
  origin        TEXT,                       -- typed | sdk | queued | system
  door          TEXT,                       -- ATLAS turn stamp: schedule | web | voice | telegram | deck
  routine       TEXT,                       -- a scheduled turn's routine name
  label         TEXT
);
CREATE INDEX IF NOT EXISTS idx_turn_session ON turn(session_id);

CREATE TABLE IF NOT EXISTS subagent (
  agent_id      TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,
  agent_type    TEXT,
  description   TEXT,
  model         TEXT
);

-- What ATLAS knows about a Claude session: its own session or job record.
CREATE TABLE IF NOT EXISTS atlas_session (
  claude_session_id TEXT PRIMARY KEY,
  atlas_id      TEXT,
  number        INTEGER,
  kind          TEXT,
  title         TEXT,
  started_by    TEXT,
  folder        TEXT,
  repo          TEXT,
  profile       TEXT,
  pr_url        TEXT,
  series_key    TEXT,
  created_ts    BIGINT
);

