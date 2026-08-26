PRAGMA journal_mode=WAL;

-- One row per statusline ingest. The quota ground-truth stream, plus the rich
-- per-render session state Claude Code hands us (cost, context, lines, model).
CREATE TABLE IF NOT EXISTS quota_sample (
  id            INTEGER PRIMARY KEY,
  ts            INTEGER NOT NULL,           -- unix seconds
  session_id    TEXT NOT NULL,
  device        TEXT,                       -- which machine reported this
  five_h_pct    REAL,                       -- NULL when rate_limits absent
  five_h_reset  INTEGER,
  seven_d_pct   REAL,
  seven_d_reset INTEGER,
  cc_version    TEXT,
  had_limits    INTEGER NOT NULL,           -- 0/1, for degraded-mode detection
  model_id      TEXT,                       -- model.id at render time
  cost_usd_total REAL,                      -- cost.total_cost_usd (CC's own tally)
  context_pct   REAL,                       -- context_window.used_percentage
  lines_added   INTEGER,
  lines_removed INTEGER,
  exceeds_200k  INTEGER                      -- 0/1
);
CREATE INDEX IF NOT EXISTS idx_quota_ts ON quota_sample(ts);

-- Full raw statusline payloads, kept for reprocessing as the schema evolves.
CREATE TABLE IF NOT EXISTS raw_statusline (
  id         INTEGER PRIMARY KEY,
  ts         INTEGER NOT NULL,
  session_id TEXT,
  device     TEXT,
  payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_statusline(ts);

-- Sessions seen, from either source.
CREATE TABLE IF NOT EXISTS session (
  session_id    TEXT PRIMARY KEY,
  first_seen    INTEGER NOT NULL,
  last_seen     INTEGER NOT NULL,
  name          TEXT,                       -- Claude Code's session_name
  device        TEXT,
  cwd           TEXT,
  project_dir   TEXT,
  git_worktree  TEXT,
  entrypoint    TEXT,
  start_type    TEXT,
  color_idx     INTEGER NOT NULL            -- stable palette slot, assigned on insert
);

-- Accumulated OTel deltas, bucketed to 10s to keep the table small.
CREATE TABLE IF NOT EXISTS usage_bucket (
  ts            INTEGER NOT NULL,           -- floor to 10s
  session_id    TEXT NOT NULL,
  model         TEXT NOT NULL,
  query_source  TEXT,                       -- main | subagent | auxiliary
  agent_name    TEXT,
  skill_name    TEXT,
  mcp_server    TEXT,
  plugin_name   TEXT,
  effort        TEXT,
  cost_usd      REAL NOT NULL DEFAULT 0,
  tok_input     INTEGER NOT NULL DEFAULT 0,
  tok_output    INTEGER NOT NULL DEFAULT 0,
  tok_cache_r   INTEGER NOT NULL DEFAULT 0,
  tok_cache_w   INTEGER NOT NULL DEFAULT 0,
  active_ms     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ts, session_id, model, query_source, agent_name, skill_name,
               mcp_server, plugin_name, effort)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_bucket_ts ON usage_bucket(ts);

-- Fitted cost->quota conversion, one row per model, rewritten by the calibrator.
CREATE TABLE IF NOT EXISTS calibration (
  model         TEXT PRIMARY KEY,
  pct_per_usd   REAL NOT NULL,
  r2            REAL,
  n_samples     INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL
);

-- Closed 5h windows, for the history view.
CREATE TABLE IF NOT EXISTS window_history (
  reset_at      INTEGER PRIMARY KEY,
  peak_pct      REAL NOT NULL,
  hit_cap       INTEGER NOT NULL,
  total_cost    REAL NOT NULL,
  session_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
  ts INTEGER NOT NULL, session_id TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event_log(ts);
