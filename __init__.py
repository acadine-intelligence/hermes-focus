"""Focus: one thing to work on now, picked from what Hermes already knows, and guarded while you do it.

Hermes adapter. All logic lives in ``hermes_focus`` (stdlib only, no Hermes imports) so it can be
tested without a running agent. This module wires it to tools, a slash command, a CLI command, a
system prompt section and three hooks.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Any

try:
    from .hermes_focus import host, render
    from .hermes_focus.service import FocusService
except ImportError:  # imported as a top-level module (pytest collecting the repo root)
    from hermes_focus import host, render  # type: ignore[no-redef]
    from hermes_focus.service import FocusService  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

TOOLSET = "focus"
_ctx: Any = None
_service: FocusService | None = None


def service() -> FocusService:
    global _service
    if _service is None:
        cfg = (lambda k, d: _ctx.get_config(k, d)) if _ctx is not None else (lambda k, d: d)
        _service = FocusService(host.hermes_home(), host.data_dir(),
                                lease_minutes=int(cfg("lease_minutes", 90)),
                                window_days=float(cfg("session_window_days", 3)),
                                kanban_db=host.kanban_db())
    return _service


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, default=str)


def _err(exc: Exception) -> str:
    return json.dumps({"success": False, "error": str(exc)})


# ---- tools -------------------------------------------------------------------------------------

SCHEMAS = {
    "focus_now": {
        "name": "focus_now",
        "description": (
            "Return the user's one current focus. If a focus is leased it returns that, plus an "
            "interruption only when something strictly more urgent exists (a deadline under 24 h or "
            "someone waiting on the user). Otherwise it proposes the top candidate from open Kanban "
            "tasks, chats waiting on the user, failing cron jobs and the user's own commitments. "
            "Deterministic and local. Call it when the user asks what to work on, says they are "
            "back, or seems scattered."),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "focus_accept": {
        "name": "focus_accept",
        "description": ("Lease a focus. With no candidate, leases the top proposal. Only call after the "
                        "user agrees; never accept on their behalf."),
        "parameters": {"type": "object", "properties": {
            "candidate": {"type": "string", "description": "Candidate id, ref, unique prefix, or list number from focus_now"},
            "minutes": {"type": "integer", "minimum": 5, "maximum": 480, "description": "Lease length; default 90"},
            "reason": {"type": "string", "description": "Why the user is switching, if a focus was already leased"},
        }, "additionalProperties": False},
    },
    "focus_done": {
        "name": "focus_done",
        "description": ("Complete the leased focus. Requires proof: what shows it is done (test output, "
                        "a link, a sent message). Returns the next proposal."),
        "parameters": {"type": "object", "properties": {
            "proof": {"type": "string", "description": "Concrete completion evidence"},
        }, "required": ["proof"], "additionalProperties": False},
    },
    "focus_defer": {
        "name": "focus_defer",
        "description": "Snooze a candidate, or release the leased focus, for some hours.",
        "parameters": {"type": "object", "properties": {
            "candidate": {"type": "string", "description": "Candidate to snooze; omit to release the leased focus"},
            "hours": {"type": "number", "minimum": 0.25, "maximum": 720, "description": "Default 24"},
            "reason": {"type": "string"},
        }, "additionalProperties": False},
    },
    "focus_add": {
        "name": "focus_add",
        "description": ("Record a commitment the user stated (\"I must send the invoice by Friday\"). "
                        "A commitment due within 24 hours outranks everything else."),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "next_action": {"type": "string", "description": "The first concrete step"},
            "deadline": {"type": "string", "description": "+3h, +2d, today, tomorrow, YYYY-MM-DD or YYYY-MM-DD HH:MM"},
        }, "required": ["title"], "additionalProperties": False},
    },
}


def focus_now(args: dict, **kw: Any) -> str:
    try:
        d = service().now(exclude_session=kw.get("session_id"))
        return _ok(decision=d, card=render.card(d))
    except Exception as exc:
        logger.exception("focus_now failed")
        return _err(exc)


def focus_accept(args: dict, **kw: Any) -> str:
    try:
        return _ok(**service().accept(args.get("candidate"), minutes=args.get("minutes"),
                                      session_id=kw.get("session_id"), reason=args.get("reason", "")))
    except Exception as exc:
        return _err(exc)


def focus_done(args: dict, **kw: Any) -> str:
    try:
        r = service().done(args.get("proof", ""), session_id=kw.get("session_id"))
        return _ok(completed=r["completed"], next_card=render.card(r["next"]))
    except Exception as exc:
        return _err(exc)


def focus_defer(args: dict, **kw: Any) -> str:
    try:
        return _ok(**service().defer(args.get("candidate"), hours=float(args.get("hours", 24)),
                                     reason=args.get("reason", ""), session_id=kw.get("session_id")))
    except Exception as exc:
        return _err(exc)


def focus_add(args: dict, **kw: Any) -> str:
    try:
        return _ok(**service().add(args.get("title", ""), next_action=args.get("next_action", ""),
                                   deadline=args.get("deadline")))
    except Exception as exc:
        return _err(exc)


HANDLERS = {"focus_now": focus_now, "focus_accept": focus_accept, "focus_done": focus_done,
            "focus_defer": focus_defer, "focus_add": focus_add}


# ---- /now ------------------------------------------------------------------------------------

USAGE = """/now                          show the focus, or the proposal
/now accept [n|id] [min]      lease it (default: the proposal, 90 min)
/now done <proof>             complete it with proof, get the next one
/now defer [n|id] [hours]     snooze it (default 24 h)
/now add <what> [by <when>]   record a commitment (+3h, tomorrow, 2026-11-11 17:00)
/now review [days]            what you finished and how often you drifted"""


def run_command(raw: str, session_id: str | None = None) -> str:
    svc = service()
    try:
        parts = shlex.split(raw or "")
    except ValueError:
        parts = (raw or "").split()
    verb = parts[0].lower() if parts else "now"
    rest = parts[1:]
    try:
        if verb in ("now", "show", "status"):
            return render.card(svc.now(exclude_session=session_id))
        if verb in ("accept", "take", "start"):
            key = rest[0] if rest else None
            mins = int(rest[1].rstrip("m")) if len(rest) > 1 and rest[1].rstrip("m").isdigit() else None
            r = svc.accept(key, minutes=mins, session_id=session_id)
            msg = f"Leased until {r['lease_until_local']}: {r['focus']['title']}\nnext: {r['focus']['next_action']}"
            if r["replaced"]:
                msg += f"\n(switched from: {r['replaced']['title']})"
            return msg
        if verb in ("done", "complete", "finish"):
            r = svc.done(" ".join(rest), session_id=session_id)
            return f"Done: {r['completed']['title']} ({r['completed']['minutes']} min)\n\n" + render.card(r["next"])
        if verb in ("defer", "skip", "snooze", "later"):
            key = rest[0] if rest and not _is_number(rest[0]) else None
            hrs = next((float(x) for x in rest if _is_number(x)), 24.0)
            r = svc.defer(key, hours=hrs, session_id=session_id)
            return f"Deferred until {r['until_local']}: {r['deferred']}\n\n" + render.card(svc.now(exclude_session=session_id))
        if verb == "add":
            text = " ".join(rest)
            title, _, when = text.partition(" by ")
            r = svc.add(title, deadline=when or None)
            return f"Added: {r['added']['title']}" + (" (with deadline)" if r["added"]["deadline"] else "")
        if verb == "review":
            days = int(rest[0]) if rest and rest[0].isdigit() else 7
            return render.review(svc.review(days))
        return USAGE
    except (ValueError, LookupError) as exc:
        return f"{exc}\n\n{USAGE}"


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _slash(raw_args: str, session_id: str | None = None) -> str:
    return run_command(raw_args, session_id=session_id)


# ---- hooks -------------------------------------------------------------------------------------

def _unattended(platform: str | None, parent_session_id: str | None) -> bool:
    if parent_session_id or (platform or "").lower() in ("cron", "kanban", "subagent", "batch"):
        return True
    try:
        return _ctx is not None and _ctx.current_cron_execution() is not None
    except Exception:
        return False


def on_pre_llm_call(session_id=None, user_message=None, platform=None, parent_session_id=None, **_: Any):
    if _ctx is not None and not _ctx.get_config("drift_guard", True):
        return None
    if _unattended(platform, parent_session_id) or not isinstance(user_message, str):
        return None
    try:
        note = service().on_turn(user_message, session_id=session_id)
    except Exception:
        logger.debug("focus drift check failed", exc_info=True)
        return None
    return {"context": note} if note else None


def on_kanban_task_completed(task_id=None, summary=None, **_: Any) -> None:
    if not task_id:
        return
    try:
        service().kanban_completed(str(task_id), summary or "")
    except Exception:
        logger.debug("focus kanban hook failed", exc_info=True)


def _prompt_section(_info: Any = None) -> str:
    try:
        return service().prompt_section()
    except Exception:
        return ""


# ---- CLI ---------------------------------------------------------------------------------------

def _cli_setup(parser) -> None:
    parser.add_argument("args", nargs="*", help="now | accept [n] [min] | done <proof> | defer [n] [h] | add <what> [by <when>] | review")
    parser.add_argument("--json", action="store_true", help="print the raw decision as JSON")


def _cli_handler(args) -> int:
    if getattr(args, "json", False):
        print(json.dumps(service().now(), indent=2, default=str))
    else:
        print(run_command(" ".join(shlex.quote(a) for a in args.args)))
    return 0


# ---- registration ------------------------------------------------------------------------------

def register(ctx) -> None:
    global _ctx, _service
    _ctx, _service = ctx, None
    for name, handler in HANDLERS.items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=SCHEMAS[name], handler=handler, emoji="🎯")
    ctx.register_command("now", _slash, description="One thing to work on now", args_hint="[accept|done|defer|add|review]")
    ctx.register_cli_command("focus", "One thing to work on now", _cli_setup, _cli_handler,
                             description="Show, lease, complete or defer your one focus.")
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("kanban_task_completed", on_kanban_task_completed)
    ctx.register_system_prompt_section("focus.current", _prompt_section, max_chars=600)
