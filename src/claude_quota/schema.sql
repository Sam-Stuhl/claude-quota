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

-- Attribution, from transcripts shipped by `claude-quota ship` (one row per
-- API request). This is the record of where usage went. The OTel buckets above
-- stay the live, 10-second view.
CREATE TABLE IF NOT EXISTS req_usage (
  request_id    TEXT PRIMARY KEY,
  ts            INTEGER NOT NULL,
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
  tok_input     INTEGER NOT NULL DEFAULT 0,
  tok_output    INTEGER NOT NULL DEFAULT 0,
  tok_cache_r   INTEGER NOT NULL DEFAULT 0,
  tok_cache_w   INTEGER NOT NULL DEFAULT 0,
  cost_usd      REAL NOT NULL DEFAULT 0     -- list price, see pricing.py
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
  first_ts      INTEGER,
  last_ts       INTEGER
);

-- One row per prompt (turn), with a short label.
CREATE TABLE IF NOT EXISTS turn (
  prompt_id     TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,
  agent_id      TEXT,
  ts            INTEGER,
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
  created_ts    INTEGER
);


-- Small named settings Sam changes from the dashboard or the CLI, as JSON
-- (the weekly budget plan, key 'budget_plan').
CREATE TABLE IF NOT EXISTS setting (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at BIGINT NOT NULL
);
