#!/usr/bin/env python3
"""Populate a claude-quota database with realistic-looking demo data.

For previewing and redesigning the dashboard without waiting to accumulate real
data. Writes to whatever database CLAUDE_QUOTA_DIR points at, so run it against
a throwaway demo directory, not your real ~/.claude-quota:

    CLAUDE_QUOTA_DIR=~/.claude-quota-demo python scripts/seed_demo.py

It generates several devices, many sessions across real project names, a fresh
5-hour window with a live projection, ~2 weeks of closed windows, a rising
7-day trend, and usage spread across models, agents, skills, MCP servers,
plugins, and effort levels. Re-run it to refresh the "now".
"""

from __future__ import annotations

import json
import random
import time

from claude_quota import config, db
from claude_quota.daemon import history

R = random.Random(42)
FIVE_H = 5 * 3600

DEVICES = ["mac-mini", "macbook-pro", "studio"]
# project -> device
PROJECTS = {
    "atlas": "mac-mini",
    "banking-dash": "mac-mini",
    "console": "studio",
    "notion-sync": "studio",
    "planner": "macbook-pro",
    "resume-git": "macbook-pro",
    "claude-quota": "mac-mini",
}
MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]
MODEL_W = [0.55, 0.35, 0.10]
QSOURCES = ["main", "subagent", "auxiliary"]
AGENTS = ["", "general-purpose", "code-reviewer", "Explore", "Plan"]
SKILLS = ["", "brainstorming", "systematic-debugging", "frontend-design", "writing-plans"]
MCPS = ["", "github", "notion", "money", "console"]
PLUGINS = ["", "superpowers", "cowork"]
EFFORTS = ["low", "medium", "high", "xhigh"]


def pick(seq):
    return R.choice(seq)


def weighted_model():
    return R.choices(MODELS, weights=MODEL_W)[0]


def add_bucket(conn, ts, sid, cost, *, model=None, qs=None, agent=None, skill=None,
               mcp=None, plugin=None, effort=None):
    bts = ts - (ts % config.BUCKET_SECONDS)
    model = model or weighted_model()
    qs = qs if qs is not None else pick(QSOURCES)
    agent = agent if agent is not None else pick(AGENTS)
    skill = skill if skill is not None else pick(SKILLS)
    mcp = mcp if mcp is not None else pick(MCPS)
    plugin = plugin if plugin is not None else pick(PLUGINS)
    effort = effort if effort is not None else pick(EFFORTS)
    ti = int(cost * R.uniform(8000, 12000))
    to = int(cost * R.uniform(1000, 1800))
    tcr = int(cost * R.uniform(40000, 60000))
    tcw = int(cost * R.uniform(2500, 4000))
    conn.execute(
        """INSERT INTO usage_bucket
           (ts, session_id, model, query_source, agent_name, skill_name, mcp_server,
            plugin_name, effort, cost_usd, tok_input, tok_output, tok_cache_r,
            tok_cache_w, active_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ts, session_id, model, query_source, agent_name, skill_name,
                       mcp_server, plugin_name, effort)
           DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd,
             tok_input = tok_input + excluded.tok_input,
             tok_output = tok_output + excluded.tok_output,
             tok_cache_r = tok_cache_r + excluded.tok_cache_r,
             tok_cache_w = tok_cache_w + excluded.tok_cache_w,
             active_ms = active_ms + excluded.active_ms""",
        (bts, sid, model, qs, agent, skill, mcp, plugin, effort, cost,
         ti, to, tcr, tcw, int(R.uniform(2000, 9000))),
    )


def add_quota(conn, ts, sid, device, five_pct, five_reset, seven_pct, seven_reset,
              *, cost_total=None):
    conn.execute(
        """INSERT INTO quota_sample
           (ts, session_id, device, five_h_pct, five_h_reset, seven_d_pct,
            seven_d_reset, cc_version, had_limits, model_id, cost_usd_total,
            context_pct, lines_added, lines_removed, exceeds_200k)
           VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)""",
        (ts, sid, device, round(five_pct, 1), five_reset, round(seven_pct, 1),
         seven_reset, "2.1.90", weighted_model(),
         round(cost_total if cost_total is not None else R.uniform(1, 40), 2),
         round(R.uniform(8, 82), 0), int(R.uniform(0, 900)), int(R.uniform(0, 300)),
         1 if R.random() < 0.08 else 0),
    )


def session_id(project, n):
    return f"{project}-{n:04x}"


def main() -> None:
    now = int(time.time())
    conn = db.connect(config.db_path())
    for t in ("quota_sample", "usage_bucket", "session", "calibration",
              "window_history", "event_log", "raw_statusline"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()

    # Register sessions (a couple per project, some on different devices).
    sessions = []
    for project, device in PROJECTS.items():
        for _ in range(R.randint(1, 2)):
            sid = session_id(project, R.randint(0, 0xFFFF))
            db.upsert_session(conn, sid, now, device=device,
                              project_dir=f"/Users/sam/dev/{project}",
                              entrypoint="statusline",
                              start_type=pick(["fresh", "resume", "continue"]))
            sessions.append((sid, project, device))
    conn.commit()

    seven_reset = now + 4 * 86400

    # ~14 days of closed 5-hour windows during working hours.
    for day in range(14, 0, -1):
        base = now - day * 86400
        for hour in R.sample([9, 11, 13, 15, 19, 21], k=R.randint(2, 4)):
            reset = base - (base % 3600) + hour * 3600
            if reset >= now:
                continue
            start = reset - FIVE_H
            peak = R.choice([48, 61, 73, 82, 91, 100, 100, 67, 55])
            active = R.sample(sessions, k=R.randint(2, 4))
            for i in range(6):
                ts = start + int(FIVE_H * (i + 1) / 7)
                sid, _, device = active[i % len(active)]
                add_quota(conn, ts, sid, device, peak * (i + 1) / 6, reset,
                          R.uniform(20, 55), seven_reset)
            for sid, _, _ in active:
                for _ in range(R.randint(2, 5)):
                    ts = start + int(R.uniform(0, FIVE_H))
                    add_bucket(conn, ts, sid, round(R.uniform(0.4, 6.0), 2))
            if R.random() < 0.25:
                conn.execute(
                    "INSERT INTO event_log (ts, session_id, kind, detail) VALUES (?,?,?,?)",
                    (start + int(FIVE_H * 0.5), active[0][0], "claude_code.compaction", "{}"),
                )

    # The current window: rising 62 -> 72 over the last 40 min => a live SHORT.
    reset = now + 7200  # 2h out
    cur_seven = 58.0
    active_now = R.sample(sessions, k=min(5, len(sessions)))
    for i in range(21):
        ts = now - 2400 + i * 120
        pct = 62 + (72 - 62) * i / 20 + R.uniform(-0.3, 0.3)
        sid, _, device = active_now[i % len(active_now)]
        add_quota(conn, ts, sid, device, pct, reset,
                  cur_seven + i * 0.05, seven_reset, cost_total=R.uniform(3, 45))

    # Rich attribution for the current window across all dimensions.
    for sid, _, _ in active_now:
        n_chunks = R.randint(4, 9)
        for _ in range(n_chunks):
            ts = now - int(R.uniform(30, 3000))
            add_bucket(conn, ts, sid, round(R.uniform(0.5, 5.5), 2))
        detail = json.dumps(
            {"cost_usd": round(R.uniform(0.01, 0.4), 3), "duration_ms": R.randint(800, 9000)}
        )
        conn.execute(
            "INSERT INTO event_log (ts, session_id, kind, detail) VALUES (?,?,?,?)",
            (now - int(R.uniform(60, 1800)), sid, "claude_code.api_request", detail),
        )

    # Calibration: converged, per model.
    # Rates tuned so local cost explains most (not all) of the window, leaving a
    # realistic unattributed residual (claude.ai / Cowork) as a grey slice.
    for m, rate in (("claude-opus-5", 0.62), ("claude-sonnet-5", 0.34), ("claude-haiku-4-5", 0.09)):
        conn.execute(
            "INSERT INTO calibration (model, pct_per_usd, r2, n_samples, updated_at) "
            "VALUES (?, ?, 0.86, 140, ?)",
            (m, rate, now),
        )
    conn.commit()

    history.close_windows(conn, now)

    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("quota_sample", "usage_bucket", "session", "window_history", "event_log")}
    conn.close()
    print("seeded demo data:", counts)


if __name__ == "__main__":
    main()
