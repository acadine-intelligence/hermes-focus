"""Focus service: the one object the Hermes adapter, the dashboard route and the tests all drive."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import engine, render_html, sources
from .store import Store

DEFAULT_LEASE_MIN = 90
NUDGE_COOLDOWN_S = 20 * 60


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def parse_deadline(text: str | None, now: float) -> float | None:
    """Accept '+3h', '+2d', 'today', 'tomorrow', 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (local time)."""
    if not text:
        return None
    t = text.strip().lower()
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)\s*([mhd])", t)
    if m:
        mult = {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return now + float(m.group(1)) * mult
    base = datetime.fromtimestamp(now)
    if t in ("today", "eod", "tonight"):
        return base.replace(hour=23, minute=59, second=0, microsecond=0).timestamp()
    if t == "tomorrow":
        return (base + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0).timestamp()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text.strip(), fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=23, minute=59)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"cannot read deadline {text!r}; use +3h, +2d, today, tomorrow or YYYY-MM-DD [HH:MM]")


class FocusService:
    def __init__(self, home: Path, data_dir: Path, *, lease_minutes: int = DEFAULT_LEASE_MIN,
                 window_days: float = 3.0, kanban_db: Path | None = None, clock=time.time,
                 judge: Any = None, judge_status: str = "off"):
        self.home = Path(home)
        self.data_dir = Path(data_dir)
        self.judge = judge
        self.judge_status = judge_status
        self.judge_info: dict[str, Any] = {"backend": judge_status, "on": judge is not None}
        self.store = Store(data_dir)
        self.lease_minutes = int(lease_minutes)
        self.window_days = float(window_days)
        self.kanban_db = kanban_db
        self.clock = clock

    # ---- reading -----------------------------------------------------------------------------

    def _observe(self, exclude_session: str | None) -> tuple[list[dict], list[str], dict[str, dict]]:
        """Read the sources and, when it is on, ask the decision model. Always runs OUTSIDE the state
        lock: the drift hook takes that lock on every turn and must never wait on a network call."""
        signals, errors = sources.collect(self.home, now=self.clock(), exclude_session=exclude_session,
                                          window_days=self.window_days, kanban_db=self.kanban_db)
        judgments: dict[str, dict] = {}
        if self.judge is not None:
            judgments, info = self.judge.judge(signals)
            self.judge_info = {"on": True, **info, "status": self.judge_status}
        return signals, errors, judgments

    def _candidates(self, state: dict, obs: tuple[list[dict], list[str], dict[str, dict]]
                    ) -> tuple[list[engine.Candidate], list[str], list[dict]]:
        signals, errors, judgments = obs
        now = self.clock()
        cands = engine.build(signals, state["commitments"], now, judgments=judgments,
                             pinned=state.get("pinned") or {})
        ranked = engine.rank(cands, deferred=state["deferred"], now=now)
        return ranked, errors, signals

    def _reconcile(self, state: dict, signals: list[dict], errors: list[str], now: float) -> None:
        """Close a leased focus whose source item finished, and retire an expired lease."""
        f = state.get("focus")
        if not f or f.get("status") != "active":
            return
        if f["source"] == "kanban" and not any(e.startswith("kanban") for e in errors):
            if not any(s["source"] == "kanban" and s["ref"] == f["ref"] for s in signals):
                self._close(state, "completed", now, proof=f"Kanban task {f['ref']} left the open list")
                return
        if f.get("lease_until", 0) <= now:
            self._close(state, "expired", now)

    def _close(self, state: dict, status: str, now: float, proof: str = "", reason: str = "") -> dict:
        f = dict(state["focus"])
        f.update(status=status, ended_at=now, proof=proof, reason=reason,
                 minutes=round((now - f.get("accepted_at", now)) / 60))
        state["history"].append(f)
        state["focus"] = None
        if status == "completed":
            state.get("pinned", {}).pop(f["id"], None)
            # The source may still look open (a chat still ends with a question). Keep the finished
            # item from bouncing straight back as the next proposal.
            state["deferred"][f["id"]] = now + 12 * 3600
        if f["source"] == "commitment" and status == "completed":
            for c in state["commitments"]:
                if c["id"] == f["ref"]:
                    c["status"] = "done"
        return f

    def now(self, *, exclude_session: str | None = None) -> dict[str, Any]:
        now = self.clock()
        obs = self._observe(exclude_session)
        with self.store.edit() as state:
            ranked, errors, signals = self._candidates(state, obs)
            self._reconcile(state, signals, errors, now)
            decision = engine.decide(ranked, state["focus"], now)
            decision["errors"] = errors
            decision["counts"] = {"candidates": len(ranked), "signals": len(signals)}
            decision["today"] = state["stats"].get(_day(now), {})
            decision["generated_at"] = now
            decision["decision_model"] = dict(self.judge_info)
            decision["ladder"] = [engine._numbered(c, ranked) for c in ranked[:8]]
        return decision

    def rank(self, *, limit: int = 10, exclude_session: str | None = None) -> dict[str, Any]:
        """The whole ranking with the reason for every position. Read-only."""
        now = self.clock()
        state = self.store.read()
        ranked, errors, _ = self._candidates(state, self._observe(exclude_session))
        by_gate: dict[str, int] = {}
        for c in ranked:
            by_gate[engine.GATES[c.gate]] = by_gate.get(engine.GATES[c.gate], 0) + 1
        f = state.get("focus")
        leased = bool(f and f.get("status") == "active" and f.get("lease_until", 0) > now)
        return {"ladder": [engine._numbered(c, ranked) for c in ranked[:max(1, min(int(limit), 50))]],
                "total": len(ranked), "by_gate": by_gate, "errors": errors,
                "leased_id": f["id"] if leased and f else None,
                "decision_model": dict(self.judge_info),
                "rules": " > ".join(engine.GATES[g] for g in sorted(engine.GATES))}

    def pin(self, candidate: str | None, *, on: bool = True, session_id: str | None = None) -> dict[str, Any]:
        """Pin: your own word that an item matters. It lifts the item above inferred work."""
        now = self.clock()
        obs = self._observe(session_id)
        with self.store.edit() as state:
            ranked, _, _ = self._candidates(state, obs)
            pick = self._find(ranked, candidate) if candidate else None
            if pick is None:
                raise LookupError(f"no candidate matches {candidate!r}" if candidate else "say which item to pin")
            if on:
                state["pinned"][pick.id] = now
                state["deferred"].pop(pick.id, None)
            else:
                state["pinned"].pop(pick.id, None)
        return {"pinned": on, "id": pick.id, "title": pick.title}

    def write_card(self, decision: dict[str, Any] | None = None) -> Path | None:
        """Write the visual card (one self-contained HTML file) and return its path."""
        try:
            path = self.data_dir / "card.html"
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(render_html.card(decision or self.now()), encoding="utf-8")
            return path
        except OSError:
            return None

    # ---- writing -----------------------------------------------------------------------------

    def _find(self, ranked: list[engine.Candidate], key: str | None) -> engine.Candidate | None:
        if not ranked:
            return None
        if not key:
            return ranked[0]
        key = key.strip()
        if key.isdigit() and 1 <= int(key) <= len(ranked):
            return ranked[int(key) - 1]
        exact = [c for c in ranked if c.id == key or c.ref == key]
        if exact:
            return exact[0]
        pref = [c for c in ranked if c.id.startswith(key) or c.ref.startswith(key)]
        return pref[0] if len(pref) == 1 else None

    def accept(self, candidate: str | None = None, *, minutes: int | None = None,
               session_id: str | None = None, reason: str = "") -> dict[str, Any]:
        now = self.clock()
        minutes = int(minutes or self.lease_minutes)
        if not 5 <= minutes <= 480:
            raise ValueError("lease minutes must be between 5 and 480")
        obs = self._observe(session_id)
        with self.store.edit() as state:
            ranked, errors, signals = self._candidates(state, obs)
            self._reconcile(state, signals, errors, now)
            pick = self._find(ranked, candidate)
            if pick is None:
                raise LookupError(f"no candidate matches {candidate!r}" if candidate else "nothing to focus on")
            prev = state["focus"]
            replaced = None
            if prev and prev["id"] != pick.id:
                replaced = self._close(state, "overridden", now, reason=reason or f"switched to {pick.id}")
            focus = pick.to_dict()
            focus.update(status="active", accepted_at=now, lease_until=now + minutes * 60,
                         session_id=session_id,
                         keywords=sorted(engine.terms(f"{pick.title} {pick.next_action}"))[:24])
            state["focus"] = focus
            state["deferred"].pop(pick.id, None)
        return {"focus": focus, "replaced": replaced, "lease_until_local": _hhmm(focus["lease_until"])}

    def done(self, proof: str, *, session_id: str | None = None) -> dict[str, Any]:
        proof = (proof or "").strip()
        if len(proof) < 8:
            raise ValueError("say what proves it is done (a test result, a link, a sent message)")
        now = self.clock()
        with self.store.edit() as state:
            if not state["focus"]:
                raise LookupError("no active focus to complete")
            closed = self._close(state, "completed", now, proof=proof)
            self._bump(state, now, "done")
        nxt = self.now(exclude_session=session_id)
        return {"completed": closed, "next": nxt}

    def defer(self, candidate: str | None = None, *, hours: float = 24, reason: str = "",
              session_id: str | None = None) -> dict[str, Any]:
        now = self.clock()
        if not 0.25 <= float(hours) <= 24 * 30:
            raise ValueError("defer hours must be between 0.25 and 720")
        obs = self._observe(session_id)
        with self.store.edit() as state:
            target = None
            f = state["focus"]
            if f and (not candidate or candidate in (f["id"], f["ref"])):
                target = f["id"]
                self._close(state, "deferred", now, reason=reason)
            else:
                ranked, _, _ = self._candidates(state, obs)
                pick = self._find(ranked, candidate)
                if pick is None:
                    raise LookupError(f"no candidate matches {candidate!r}")
                target = pick.id
            state["deferred"][target] = now + float(hours) * 3600
        return {"deferred": target, "until_local": _hhmm(now + float(hours) * 3600)}

    def add(self, title: str, *, next_action: str = "", deadline: str | None = None,
            done_when: str = "") -> dict[str, Any]:
        title = " ".join((title or "").split())
        if len(title) < 3:
            raise ValueError("a commitment needs a title")
        now = self.clock()
        due = parse_deadline(deadline, now)
        cid = "commitment:" + hashlib.sha1(f"{title}|{now}".encode()).hexdigest()[:8]
        item = {"id": cid, "title": title[:120], "next_action": next_action.strip()[:160],
                "deadline": due, "status": "open", "created_at": now,
                "done_when": " ".join((done_when or "").split())[:160]}
        with self.store.edit() as state:
            state["commitments"].append(item)
        return {"added": item}

    # ---- turn hooks ----------------------------------------------------------------------------

    def _bump(self, state: dict, now: float, key: str) -> None:
        day = state["stats"].setdefault(_day(now), {})
        day[key] = int(day.get(key, 0)) + 1

    def on_turn(self, message: str, *, session_id: str | None, nudge: bool = True) -> str | None:
        """Record the turn. With ``nudge``, return a one-line context note when it drifts off a
        leased focus. Without it the turn is only counted, so the pane can show drift quietly."""
        now = self.clock()
        state = self.store.read()
        f = state.get("focus")
        if not f or f.get("status") != "active" or f.get("lease_until", 0) <= now:
            return None
        anchored = session_id and session_id in (f.get("session_id"), f.get("ref"))
        drift = not anchored and engine.is_drift(message, f)
        with self.store.edit() as state:
            self._bump(state, now, "drift" if drift else "on_focus")
            if not drift or not nudge:
                return None
            last = float(state["nudged"].get(session_id or "-", 0))
            if now - last < NUDGE_COOLDOWN_S:
                return None
            state["nudged"][session_id or "-"] = now
            self._bump(state, now, "nudges")
        left = int((f["lease_until"] - now) // 60)
        return (f"[Focus] The user's leased focus is \"{f['title']}\" (next: {f['next_action']}; "
                f"{left} min left on the lease). This request looks unrelated. Help with it, keep it short, "
                f"then offer one line to return to the focus. Do not refuse or lecture.")

    def kanban_completed(self, task_id: str, summary: str = "") -> bool:
        now = self.clock()
        with self.store.edit() as state:
            f = state.get("focus")
            if f and f.get("status") == "active" and f["source"] == "kanban" and f["ref"] == task_id:
                self._close(state, "completed", now, proof=summary or f"Kanban task {task_id} completed")
                self._bump(state, now, "done")
                return True
        return False

    def prompt_section(self) -> str:
        state = self.store.read()
        f = state.get("focus")
        now = self.clock()
        if not f or f.get("status") != "active" or f.get("lease_until", 0) <= now:
            return ""
        done = f" Done when: {f['done_when']}." if f.get("done_when") else ""
        return (f"The user has one leased focus until {_hhmm(f['lease_until'])}: \"{f['title']}\". "
                f"Next action: {f['next_action']}.{done} Favour work that moves it. When they finish, suggest "
                f"`/now done <proof>`; when something else is genuinely more urgent, say so plainly.")

    # ---- review ------------------------------------------------------------------------------

    def review(self, days: int = 7) -> dict[str, Any]:
        now = self.clock()
        cutoff = now - days * 86400
        state = self.store.read()
        hist = [h for h in state["history"] if h.get("ended_at", 0) >= cutoff]
        by = {k: [h for h in hist if h["status"] == k] for k in ("completed", "overridden", "deferred", "expired")}
        on = drift = 0
        for d, v in state["stats"].items():
            if d >= _day(cutoff):
                on += int(v.get("on_focus", 0))
                drift += int(v.get("drift", 0))
        total = on + drift
        return {
            "days": days,
            "completed": len(by["completed"]),
            "overridden": len(by["overridden"]),
            "deferred": len(by["deferred"]),
            "expired": len(by["expired"]),
            "focus_minutes": sum(int(h.get("minutes", 0)) for h in by["completed"]),
            "on_focus_turns": on,
            "drift_turns": drift,
            "focus_ratio": round(on / total, 2) if total else None,
            "recent": [{"title": h["title"], "status": h["status"], "proof": h.get("proof", ""),
                        "minutes": h.get("minutes", 0)} for h in hist[-5:]][::-1],
        }
