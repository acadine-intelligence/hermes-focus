"""Build a fictional Hermes home for tests and the demo video. No real data.

    python3 demo/seed.py /tmp/focus-demo-home
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

H = 3600


def seed(home: Path, now: float | None = None, *, extras: bool = False) -> Path:
    """``extras`` adds two chats that the plain rules read the same way and a decision model does not:
    one where the agent finished and only offered more, one where it is blocked on an approval."""
    now = time.time() if now is None else now
    home.mkdir(parents=True, exist_ok=True)

    k = sqlite3.connect(home / "kanban.db")
    k.executescript("""
        DROP TABLE IF EXISTS tasks; DROP TABLE IF EXISTS task_links;
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
            priority INTEGER, created_at REAL, started_at REAL, block_kind TEXT, last_failure_error TEXT);
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
    """)
    k.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("t_a1", "Approve the pricing page copy", "", "writer", "blocked", 150, now - 30 * H, None, "needs_input", None),
        ("t_b2", "Ship onboarding email sequence", "", "builder", "todo", 120, now - 50 * H, None, None, None),
        ("t_c3", "Migrate billing webhooks to v2", "", "builder", "running", 100, now - 6 * H, now - 2 * H, None, None),
        ("t_d4", "Write launch blog post", "", "writer", "todo", 40, now - 200 * H, None, None, None),
        ("t_e5", "Old idea: dark mode", "", "builder", "done", 10, now - 400 * H, None, None, None),
    ])
    k.executemany("INSERT INTO task_links VALUES (?,?)", [("t_a1", "t_b2"), ("t_a1", "t_d4")])
    k.commit()
    k.close()

    s = sqlite3.connect(home / "state.db")
    s.executescript("""
        DROP TABLE IF EXISTS sessions; DROP TABLE IF EXISTS messages;
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, cwd TEXT,
            parent_session_id TEXT, started_at REAL NOT NULL, last_activity_at REAL,
            archived INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, timestamp REAL);
    """)
    sessions = [
        ("s_q", "desktop", "Fix flaky checkout test", "/work/shop", None, now - 5 * H, now - 3 * H),
        ("s_r", "cli", "Refactor search ranking", "/work/search", None, now - 30 * H, now - 26 * H),
        ("s_cron", "cron", "nightly report", None, None, now - 2 * H, now - 2 * H),
        ("s_sub", "subagent", "child", None, "s_q", now - 4 * H, now - 4 * H),
        ("s_live", "desktop", "This chat", None, None, now - 600, now - 60),
    ]
    s.executemany("INSERT INTO sessions (id, source, title, cwd, parent_session_id, started_at, last_activity_at)"
                  " VALUES (?,?,?,?,?,?,?)", sessions)
    s.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", [
        ("s_q", "user", "the checkout test fails one run in five", now - 5 * H),
        ("s_q", "assistant", "Found a race in the cart fixture. Two fixes: retry or await the cart. Which one do you want?", now - 3 * H),
        ("s_r", "user", "refactor ranking", now - 30 * H),
        ("s_r", "assistant", "Refactor is half done; scoring module extracted.", now - 26 * H),
    ])
    if extras:
        s.executemany("INSERT INTO sessions (id, source, title, cwd, parent_session_id, started_at, last_activity_at)"
                      " VALUES (?,?,?,?,?,?,?)", [
            ("s_opt", "desktop", "Summarise the Q3 churn report", "/work/data", None, now - 4 * H, now - 2 * H),
            ("s_blk", "cli", "Rotate the staging database password", "/work/infra", None, now - 9 * H, now - 8 * H),
        ])
        s.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", [
            ("s_opt", "user", "summarise the Q3 churn report in five bullets", now - 4 * H),
            ("s_opt", "assistant", "Here are the five bullets, saved to churn-summary.md. Want me to also turn "
                                   "them into a slide?", now - 2 * H),
            ("s_blk", "user", "rotate the staging database password and update the app secrets", now - 9 * H),
            ("s_blk", "assistant", "New password generated and stored. I have not applied it: applying restarts "
                                   "the staging API for about a minute. Reply go and I will apply it now.", now - 8 * H),
        ])
    s.commit()
    s.close()

    (home / "cron").mkdir(exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "j_ok", "name": "morning digest", "enabled": True, "last_status": "ok", "failure_streak": 0},
        {"id": "j_bad", "name": "backup to S3", "enabled": True, "last_status": "error", "failure_streak": 3,
         "last_error": "AccessDenied: bucket policy"},
        {"id": "j_off", "name": "paused job", "enabled": False, "last_status": "error", "failure_streak": 9},
    ]}))
    return home


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--extras"]
    out = seed(Path(args[0] if args else "demo/home"), extras="--extras" in sys.argv)
    print(out)
