"""Plain-text rendering for /now, the CLI and tool results."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _t(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "-"


def card(d: dict[str, Any]) -> str:
    mode, f = d.get("mode"), d.get("focus")
    lines: list[str] = []
    if mode == "empty" or not f:
        lines.append("Nothing open. No blocked tasks, waiting chats, failing crons or commitments.")
        lines.append("Add one: /now add <what> [by <when>]")
        return "\n".join(lines)
    if mode == "leased":
        lines.append(f"FOCUS  {f['title']}")
        lines.append(f"  next   {f['next_action']}")
        lines.append(f"  lease  until {_t(f.get('lease_until'))} ({d.get('lease_left_min')} min left)")
    else:
        lines.append(f"PROPOSED  {f['title']}")
        lines.append(f"  next   {f['next_action']}")
    lines.append(f"  why    {f.get('gate_reason', '')}: " + "; ".join(f.get("evidence", [])[:3]))
    if f.get("done_when"):
        lines.append(f"  done   when {f['done_when']}")
    if f.get("resume"):
        lines.append(f"  open   {f['resume']}")
    if d.get("interrupt"):
        i = d["interrupt"]
        lines.append("")
        lines.append(f"INTERRUPT?  {i['title']}  ({i['gate_reason']})")
        lines.append(f"  switch with: /now accept {i['id']}")
    nxt = d.get("next") or []
    if nxt:
        lines.append("")
        lines.append("then:")
        for c in nxt:
            lines.append(f"  {c.get('n', '-')}. {c['title']}  [{c['gate_reason']}]")
    if mode == "proposed":
        lines.append("")
        lines.append("Take it: /now accept   Pick another: /now accept 2   Skip: /now defer   Ranking: /now why")
    today = d.get("today") or {}
    if today:
        lines.append("")
        lines.append(f"today: {today.get('done', 0)} done, {today.get('on_focus', 0)} on-focus turns, "
                     f"{today.get('drift', 0)} drifted")
    for e in d.get("errors") or []:
        lines.append(f"(source skipped: {e})")
    dm = _model_line(d.get("decision_model"))
    if dm:
        lines.append(dm)
    return "\n".join(lines)


def _model_line(dm: dict[str, Any] | None) -> str:
    if not dm or not dm.get("on"):
        return ""
    line = (f"decision model {dm.get('status') or dm.get('backend')}: {dm.get('judged', 0)} chats judged now, "
            f"{dm.get('cached', 0)} from cache")
    if dm.get("skipped"):
        line += f", {dm['skipped']} left to the plain rules"
    if dm.get("errors"):
        line += f" ({'; '.join(dm['errors'])})"
    return line


def why(r: dict[str, Any]) -> str:
    """The whole ranking, with the reason for every position."""
    ladder = r.get("ladder") or []
    if not ladder:
        return "Nothing open to rank."
    lines = [f"RANKING  {r.get('total', len(ladder))} open items"]
    for c in ladder:
        mark = "  <- your focus" if c["id"] == r.get("leased_id") else ""
        pin = "  [pinned]" if c.get("pinned") else ""  # also shown when a deadline outranks the pin
        lines.append(f"{c['n']:>2}. {c['title']}{pin}{mark}")
        parts = ", ".join(f"{k} {v:+g}" for k, v in (c.get("contributions") or [])[:3])
        lines.append(f"    {c['gate_reason']} · score {c['score']:g}" + (f" ({parts})" if parts else ""))
        lines.append("    " + "; ".join(c.get("evidence", [])[:4]))
    lines.append("")
    lines.append(f"Order: {r.get('rules', '')}.")
    lines.append("A pin or your own commitment wins ties. The score only orders items inside a level.")
    lines.append("Change it: /now pin <n>   /now unpin <n>   /now defer <n>   /now accept <n>")
    for e in r.get("errors") or []:
        lines.append(f"(source skipped: {e})")
    dm = _model_line(r.get("decision_model"))
    lines.append(dm if dm else "decision model: off (plain rules only)")
    return "\n".join(lines)


def review(r: dict[str, Any]) -> str:
    ratio = "n/a" if r["focus_ratio"] is None else f"{int(r['focus_ratio'] * 100)}%"
    lines = [f"Last {r['days']} days: {r['completed']} completed ({r['focus_minutes']} focused min), "
             f"{r['overridden']} switched, {r['deferred']} deferred, {r['expired']} leases expired.",
             f"Turns on focus: {ratio} ({r['on_focus_turns']} on, {r['drift_turns']} drifted)."]
    for h in r["recent"]:
        tail = f" - {h['proof']}" if h.get("proof") else ""
        lines.append(f"  {h['status']:<10} {h['title']}{tail}")
    return "\n".join(lines)
