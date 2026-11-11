"""Read-only collectors over state Hermes already keeps on disk.

Each collector returns a list of ``Signal`` dicts. Every reader opens files read-only, tolerates
missing files and unknown schema versions, and never raises: a broken source yields no signals and
one entry in ``errors``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

# Session sources that represent a human at a keyboard. Cron, subagent, kanban worker and oneshot
# sessions are machine-driven and never become focus candidates on their own.
INTERACTIVE_SOURCES = frozenset({"cli", "tui", "desktop", "gui", "local", "telegram", "discord",
                                 "slack", "whatsapp", "signal", "matrix", "imessage", "email", "web"})

_QUESTION_TAIL = re.compile(r"\?\s*(\*\*|_|`)?\s*$")
_ASK_MARKERS = re.compile(r"(decisions? for you|your call|should i|do you want|want me to|"
                          r"which (one|option)|approve|confirm|let me know)", re.I)


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def clip_ends(text: Any, n: int) -> str:
    """Keep the head and the tail. The end of a reply is where the ask or the handoff sits."""
    s = str(text or "").strip()
    if len(s) <= n:
        return s
    half = n // 2
    return s[:half].rstrip() + "\n[...]\n" + s[-half:].lstrip()


def waiting_on_user(text: str) -> bool:
    """True when an assistant message ends by asking the human something."""
    tail = (text or "").strip()[-400:]
    if not tail:
        return False
    if _QUESTION_TAIL.search(tail):
        return True
    return bool(_ASK_MARKERS.search(tail))


def kanban_signals(db_path: Path, now: float, errors: list[str]) -> list[dict]:
    try:
        conn = _ro(db_path)
    except sqlite3.Error as exc:
        errors.append(f"kanban: {exc}")
        return []
    if conn is None:
        return []
    out: list[dict] = []
    try:
        cols = _columns(conn, "tasks")
        if not {"id", "title", "status"} <= cols:
            errors.append("kanban: unknown tasks schema")
            return []
        pick = [c for c in ("id", "title", "body", "status", "priority", "assignee", "created_at",
                            "started_at", "block_kind", "last_failure_error") if c in cols]
        rows = conn.execute(
            f"SELECT {', '.join(pick)} FROM tasks WHERE status IN "
            "('blocked','ready','running','todo','review')").fetchall()
        unlocks: dict[str, int] = {}
        if _columns(conn, "task_links") >= {"parent_id", "child_id"}:
            for r in conn.execute(
                    "SELECT l.parent_id, COUNT(*) FROM task_links l JOIN tasks c ON c.id = l.child_id "
                    "WHERE c.status IN ('todo','blocked') GROUP BY l.parent_id"):
                unlocks[r[0]] = int(r[1])
        for r in rows:
            d = dict(r)
            created = float(d.get("created_at") or now)
            out.append({
                "source": "kanban",
                "ref": d["id"],
                "title": _clip(d.get("title"), 120),
                "status": d.get("status"),
                "block_kind": d.get("block_kind"),
                "priority": int(d.get("priority") or 0),
                "assignee": d.get("assignee"),
                "age_hours": max(0.0, (now - created) / 3600),
                "unlocks": unlocks.get(d["id"], 0),
                "detail": _clip(d.get("last_failure_error") or d.get("body"), 160),
            })
    except sqlite3.Error as exc:
        errors.append(f"kanban: {exc}")
    finally:
        conn.close()
    return out


def session_signals(db_path: Path, now: float, errors: list[str], *, window_days: float = 3.0,
                    exclude_session: str | None = None, idle_minutes: float = 20.0,
                    limit: int = 40) -> list[dict]:
    try:
        conn = _ro(db_path)
    except sqlite3.Error as exc:
        errors.append(f"sessions: {exc}")
        return []
    if conn is None:
        return []
    out: list[dict] = []
    try:
        cols = _columns(conn, "sessions")
        if not {"id", "source", "started_at"} <= cols:
            errors.append("sessions: unknown schema")
            return []
        activity = "COALESCE(last_activity_at, started_at)" if "last_activity_at" in cols else "started_at"
        where = [f"{activity} > ?", "parent_session_id IS NULL" if "parent_session_id" in cols else "1"]
        for flag in ("archived", "hidden"):
            if flag in cols:
                where.append(f"COALESCE({flag},0) = 0")
        placeholders = ",".join("?" for _ in INTERACTIVE_SOURCES)
        where.append(f"source IN ({placeholders})")
        title = "title" if "title" in cols else "NULL"
        cwd = "cwd" if "cwd" in cols else "NULL"
        pinned = "pinned" if "pinned" in cols else "0"
        rows = conn.execute(
            f"SELECT id, source, {title} AS title, {cwd} AS cwd, {pinned} AS pinned, {activity} AS act "
            f"FROM sessions WHERE {' AND '.join(where)} ORDER BY act DESC LIMIT ?",
            (now - window_days * 86400, *sorted(INTERACTIVE_SOURCES), limit)).fetchall()
        mcols = _columns(conn, "messages")
        for r in rows:
            sid = r["id"]
            if sid == exclude_session:
                continue
            idle_min = (now - float(r["act"])) / 60
            if idle_min < idle_minutes:
                continue  # probably still being worked in another window
            last = None
            first_user = None
            last_user = None
            if {"session_id", "role", "content"} <= mcols:
                last_user = conn.execute(
                    "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
                    "AND content IS NOT NULL AND content != '' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
                last = conn.execute(
                    "SELECT role, content FROM messages WHERE session_id = ? AND role IN ('user','assistant') "
                    "AND content IS NOT NULL AND content != '' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
                if not r["title"]:
                    first_user = conn.execute(
                        "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
                        "ORDER BY id LIMIT 1", (sid,)).fetchone()
            last_role = last["role"] if last else None
            last_text = str(last["content"]) if last else ""
            out.append({
                "source": "session",
                "ref": sid,
                "title": _clip(r["title"] or (first_user["content"] if first_user else sid), 90),
                "platform": r["source"],
                "cwd": r["cwd"],
                "pinned": bool(r["pinned"]),
                "idle_hours": idle_min / 60,
                "waiting_on_user": last_role == "assistant" and waiting_on_user(last_text),
                "last_assistant": _clip(last_text, 200) if last_role == "assistant" else "",
                # Only read by the optional decision model (off by default); never leaves otherwise.
                "last_user_text": clip_ends(last_user["content"], 700) if last_user else "",
                "last_assistant_text": clip_ends(last_text, 1600) if last_role == "assistant" else "",
            })
    except sqlite3.Error as exc:
        errors.append(f"sessions: {exc}")
    finally:
        conn.close()
    return out


def cron_signals(jobs_path: Path, now: float, errors: list[str]) -> list[dict]:
    if not jobs_path.is_file():
        return []
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"cron: {exc}")
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    out: list[dict] = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or not job.get("enabled", True) or job.get("paused_at"):
            continue
        streak = int(job.get("failure_streak") or 0)
        status = str(job.get("last_status") or "").lower()
        if streak < 2 and status not in ("error", "failed", "failure"):
            continue
        out.append({
            "source": "cron",
            "ref": str(job.get("id") or job.get("name")),
            "title": _clip(job.get("name") or job.get("id"), 80),
            "failure_streak": max(streak, 1),
            "detail": _clip(job.get("last_error") or job.get("last_delivery_error") or status, 160),
        })
    return out


def collect(home: Path, *, now: float | None = None, exclude_session: str | None = None,
            window_days: float = 3.0, kanban_db: Path | None = None) -> tuple[list[dict], list[str]]:
    """Collect all signals under one Hermes home. Returns (signals, errors)."""
    now = time.time() if now is None else now
    errors: list[str] = []
    signals = []
    signals += kanban_signals(kanban_db or home / "kanban.db", now, errors)
    signals += session_signals(home / "state.db", now, errors, window_days=window_days,
                               exclude_session=exclude_session)
    signals += cron_signals(home / "cron" / "jobs.json", now, errors)
    return signals, errors
