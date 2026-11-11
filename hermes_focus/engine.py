"""Deterministic focus selection.

Signals become candidates. Each candidate gets a hard gate (lower is more urgent) and a weighted
tie-break score from 0-5 factors. The pick is the lowest gate, then the highest score, then the id.
Nothing here calls a model or the network, so the same state always gives the same answer.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# A wait older than this stops being "someone is waiting on you" and becomes ordinary work in
# flight: after three days the person has either moved on or the item needs triage, not a sprint.
WAIT_FRESH_HOURS = 72

# Hard gates, most urgent first. A score never moves a candidate across a gate.
GATES = {
    0: "due within 24 hours",
    1: "someone is waiting on you",
    2: "automation is failing",
    3: "work in flight",
    4: "backlog",
}

WEIGHTS = {
    "deadline": 4,
    "consequence": 4,
    "unlock": 3,
    "readiness": 2,
    "momentum": 2,
    "staleness": -2,
}

# Optional decision-model evidence (see judge.py). The model supplies probabilities; this file owns
# what they are allowed to do. Between the two thresholds the answer is undecided and the plain
# rules stand.
ACCEPT = 0.70
REJECT = 0.30

# A routine re-rank never replaces a leased focus. Only a candidate at one of these gates, and at a
# strictly more urgent gate than the focus, is offered as an interruption.
INTERRUPT_GATES = frozenset({0, 1})


@dataclass
class Candidate:
    id: str
    title: str
    next_action: str
    source: str
    ref: str
    gate: int
    factors: dict[str, float]
    evidence: list[str]
    resume: str = ""
    deadline: float | None = None
    score: float = 0.0
    done_when: str = ""
    pinned: bool = False
    judged: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # A pin lifts an item to gate 1. Say why it is there in the user's own terms.
        d["gate_reason"] = "you pinned this" if self.pinned and self.gate == 1 else GATES[self.gate]
        d["contributions"] = self.contributions()
        return d

    def contributions(self) -> list[list[Any]]:
        """Each factor's share of the score, largest first: [["consequence", 12.0], ...]."""
        parts = [[k, round(WEIGHTS[k] * self.factors.get(k, 0.0), 1)] for k in WEIGHTS]
        return sorted([p for p in parts if p[1]], key=lambda p: -abs(p[1]))


def _clamp(x: float) -> float:
    return max(0.0, min(5.0, float(x)))


def _score(f: dict[str, float]) -> float:
    return round(sum(WEIGHTS[k] * f.get(k, 0.0) for k in WEIGHTS), 2)


def _hours(h: float) -> str:
    if h < 1:
        return f"{int(h * 60)} min"
    if h < 48:
        return f"{h:.0f} h"
    return f"{h / 24:.0f} days"


def from_kanban(s: dict) -> Candidate:
    status, kind = s.get("status"), s.get("block_kind")
    waiting = status == "blocked" and kind in ("needs_input", "capability", None)
    waiting = waiting or status == "review"
    fresh = s.get("age_hours", 0) <= WAIT_FRESH_HOURS
    gate = 1 if waiting and fresh else 3 if waiting or status in ("running", "ready") else 4
    prio = int(s.get("priority") or 0)
    f = {
        "consequence": _clamp(prio / 50),
        "unlock": _clamp(s.get("unlocks", 0) * 1.5),
        "readiness": 5.0 if status in ("ready", "running", "review") else 3.0 if waiting else 2.0,
        "momentum": 4.0 if status == "running" else 1.0,
        "staleness": _clamp((s.get("age_hours", 0) - 72) / 48),
    }
    ev = [f"Kanban {s['ref']} is {status}" + (f" ({kind})" if kind else "")]
    if prio:
        ev.append(f"priority {prio}")
    if s.get("unlocks"):
        ev.append(f"unblocks {s['unlocks']} other task(s)")
    ev.append(f"opened {_hours(s.get('age_hours', 0))} ago")
    if waiting and status == "review":
        action = "Review the handoff and approve or request changes"
    elif waiting:
        action = "Answer the blocker so the task can move"
    elif status == "running":
        action = "Check the running worker and unblock it if it stalls"
    else:
        action = "Start the task or hand it to a worker"
    if status == "review":
        done = "the handoff is approved or sent back"
    elif waiting:
        done = "the card is no longer blocked"
    else:
        done = "the card is done or handed to a worker"
    return Candidate(id=f"kanban:{s['ref']}", title=s["title"], next_action=action, source="kanban",
                     ref=s["ref"], gate=gate, factors=f, evidence=ev,
                     resume=f"hermes kanban show {s['ref']}", done_when=done)


def from_session(s: dict) -> Candidate:
    idle = float(s.get("idle_hours", 0))
    waiting = bool(s.get("waiting_on_user"))
    f = {
        "consequence": 3.0 if waiting else 1.5,
        "readiness": 4.0,
        "momentum": _clamp(5 - idle / 6),
        "staleness": _clamp((idle - 48) / 24),
        "unlock": 0.0,
    }
    if s.get("pinned"):
        f["consequence"] = _clamp(f["consequence"] + 1.5)
    ev = [f"{s.get('platform', 'chat')} session idle {_hours(idle)}"]
    if waiting:
        ev.append("the agent's last message asks you something")
    if s.get("pinned"):
        ev.append("pinned in Hermes")
    action = ("Reply to the open question" if waiting else "Resume where the chat stopped")
    return Candidate(id=f"session:{s['ref']}", title=s["title"], next_action=action, source="session",
                     ref=s["ref"], gate=1 if waiting and idle <= WAIT_FRESH_HOURS else 3,
                     factors=f, evidence=ev,
                     resume=f"hermes --resume {s['ref']}",
                     done_when=("you have answered in that chat" if waiting
                                else "the chat's request is finished or parked on purpose"))


def from_cron(s: dict) -> Candidate:
    streak = int(s.get("failure_streak", 1))
    f = {"consequence": _clamp(1 + streak), "readiness": 4.0, "momentum": 0.0, "unlock": 1.0,
         "staleness": 0.0}
    ev = [f"cron job failed {streak} run(s) in a row"]
    if s.get("detail"):
        ev.append(f"last error: {s['detail']}")
    return Candidate(id=f"cron:{s['ref']}", title=f"Fix failing cron: {s['title']}",
                     next_action="Read the last run output and fix or pause the job", source="cron",
                     ref=s["ref"], gate=2, factors=f, evidence=ev, resume=f"hermes cron show {s['ref']}",
                     done_when="the next run succeeds, or the job is paused on purpose")


def from_commitment(c: dict, now: float) -> Candidate:
    """A commitment the user stated directly. Highest trust: it outranks inferred work at its gate."""
    deadline = c.get("deadline")
    hours_left = (deadline - now) / 3600 if deadline else None
    gate = 0 if hours_left is not None and hours_left <= 24 else 3
    f = {
        "deadline": 0.0 if hours_left is None else _clamp(5 - max(hours_left, 0) / 24),
        "consequence": 4.0,
        "readiness": 5.0 if c.get("next_action") else 3.0,
        "momentum": 2.0,
        "unlock": 0.0,
        "staleness": 0.0,
    }
    ev = ["you added this commitment"]
    if hours_left is not None:
        ev.append("overdue" if hours_left < 0 else f"due in {_hours(hours_left)}")
    return Candidate(id=c["id"], title=c["title"], next_action=c.get("next_action") or "Define the first step",
                     source="commitment", ref=c["id"], gate=gate, factors=f, evidence=ev, deadline=deadline,
                     done_when=c.get("done_when") or "you can name the proof (a link, a test result, a sent message)")


def _prob(x: Any) -> float | None:
    """A usable probability, or None. JSON admits NaN and 1e309, and bool is an int."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if x == x and 0.0 <= x <= 1.0 else None


def apply_judgment(c: Candidate, answers: dict | None, *, fresh: bool) -> None:
    """Let decision-model evidence move a chat candidate between gates. Policy lives here, in code.

    The model answers three yes/no questions about one chat. It never ranks, never sees other
    candidates, and a missing or malformed answer changes nothing.
    """
    if c.source != "session" or not isinstance(answers, dict):
        return
    need, opt, opn = (_prob(answers.get(k)) for k in ("human_needed", "optional_only", "open_commitment"))
    if need is None or opt is None or opn is None:
        return
    c.judged = {"human_needed": need, "optional_only": opt, "open_commitment": opn}
    if need >= ACCEPT and opn >= ACCEPT:
        c.gate = 1 if fresh else 3
        c.factors["consequence"] = max(c.factors.get("consequence", 0.0), 4.0)
        c.next_action = "Reply to the open question"
        c.done_when = "you have answered in that chat"
        c.evidence.append(f"decision model: unfinished work needs your answer ({need:.2f})")
    elif opt >= ACCEPT and need <= REJECT:
        c.gate = 4
        c.next_action = "Nothing owed: the agent only offered optional extras"
        c.evidence.append(f"decision model: only an optional offer is open ({opt:.2f})")
    elif opn <= REJECT and need <= REJECT:
        c.gate = 4
        c.next_action = "Nothing owed: the request in this chat was finished"
        c.evidence.append(f"decision model: no unfinished request ({1 - opn:.2f})")
    else:
        c.evidence.append(f"decision model: undecided (needs you {need:.2f}, unfinished {opn:.2f}); rules stand")


def build(signals: list[dict], commitments: list[dict], now: float | None = None, *,
          judgments: dict[str, dict] | None = None, pinned: dict[str, float] | None = None) -> list[Candidate]:
    now = time.time() if now is None else now
    makers = {"kanban": from_kanban, "session": from_session, "cron": from_cron}
    out = []
    for s in signals:
        if s.get("source") not in makers:
            continue
        c = makers[s["source"]](s)
        if judgments and c.source == "session" and c.ref in judgments:
            apply_judgment(c, judgments[c.ref], fresh=float(s.get("idle_hours", 0)) <= WAIT_FRESH_HOURS)
        out.append(c)
    out += [from_commitment(c, now) for c in commitments if c.get("status", "open") == "open"]
    for c in out:
        if pinned and c.id in pinned:
            # Your own word outranks inference: a pin lifts the item to "waiting on you" and wins
            # ties there. A deadline inside 24 hours still comes first.
            c.pinned = True
            c.gate = min(c.gate, 1)
            c.evidence.insert(0, "you pinned this")
        c.score = _score(c.factors)
    return out


def rank(cands: list[Candidate], *, deferred: dict[str, float] | None = None,
         now: float | None = None) -> list[Candidate]:
    now = time.time() if now is None else now
    deferred = deferred or {}
    live = [c for c in cands if deferred.get(c.id, 0) <= now]
    # Commitments win ties inside a gate: a stated commitment outranks inferred work.
    return sorted(live, key=lambda c: (c.gate, not c.pinned, c.source != "commitment", -c.score, c.id))


def decide(ranked: list[Candidate], focus: dict | None, now: float | None = None) -> dict[str, Any]:
    """Return the focus decision: the active lease if one holds, else the top candidate.

    Shape: {"mode": "leased"|"proposed"|"empty", "focus": {...}, "interrupt": {...}|None,
            "next": [...], "lease_left_min": int|None}
    """
    now = time.time() if now is None else now
    leased = bool(focus and focus.get("status") == "active" and focus.get("lease_until", 0) > now)
    if leased and focus is not None:
        others = [c for c in ranked if c.id != focus["id"]]
        interrupt = next((c for c in others
                          if c.gate in INTERRUPT_GATES and c.gate < int(focus.get("gate", 4))), None)
        return {
            "mode": "leased",
            "focus": focus,
            "interrupt": interrupt.to_dict() if interrupt else None,
            "next": [_numbered(c, ranked) for c in others[:3]],
            "lease_left_min": int((focus["lease_until"] - now) // 60),
        }
    if not ranked:
        return {"mode": "empty", "focus": None, "interrupt": None, "next": [], "lease_left_min": None}
    return {"mode": "proposed", "focus": ranked[0].to_dict(), "interrupt": None,
            "next": [_numbered(c, ranked) for c in ranked[1:4]], "lease_left_min": None}


def _numbered(c: Candidate, ranked: list[Candidate]) -> dict[str, Any]:
    d = c.to_dict()
    d["n"] = ranked.index(c) + 1  # the number `/now accept <n>` takes
    return d


# ---- drift -----------------------------------------------------------------------------------

_STOP = frozenset("""the and for with that this from into your you are was were have has had not but
what when where which while will would could should can just then than them they their there here
about after before over under again more most some such only also very make made does done doing
check task work hermes session chat please thanks okay yeah sure let lets need want""".split())
_WORD = re.compile(r"[a-z0-9][a-z0-9_\-]{3,}")


def terms(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def is_drift(message: str, focus: dict, *, min_words: int = 6) -> bool:
    """True when a substantial message shares no terms with the leased focus.

    Short replies ("yes", "go", "looks good") never count: they continue whatever is happening.
    """
    if len((message or "").split()) < min_words:
        return False
    anchor = terms(" ".join(str(focus.get(k, "")) for k in ("title", "next_action", "ref")))
    anchor |= set(focus.get("keywords") or [])
    if not anchor:
        return False
    return not (terms(message) & anchor)
