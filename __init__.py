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
        cfg = (lambda k, d=None: _ctx.get_config(k, d)) if _ctx is not None else (lambda k, d=None: d)
        llm = None
        if str(cfg("decision_model", "off")).lower() == "llm" and _ctx is not None:
            try:
                llm = _ctx.llm
            except Exception:
                logger.debug("focus: ctx.llm unavailable", exc_info=True)
        _service = host.build_service(cfg, llm=llm)
    return _service


def _visual(decision: dict | None = None, session_id: str | None = None) -> dict:
    """Refresh the HTML card and say how to show it. Text surfaces ignore this and print `card`."""
    path = service().write_card(decision or service().now(exclude_session=session_id))
    if not path:
        return {}
    return {"visual": {"html": str(path), "show": (
        f"On Hermes Desktop, put ::preview{{file=\"{path}\"}} alone on its own line to render the card "
        "inline (buttons included). When the user has the Focus desktop half enabled, ::focus{} alone on "
        "its own line renders the live card instead. On every other surface, print `card` as plain text.")}}


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
            "done_when": {"type": "string", "description": "The observable proof that will show it is done"},
        }, "required": ["title"], "additionalProperties": False},
    },
    "focus_rank": {
        "name": "focus_rank",
        "description": ("Return the whole ranking of the user's open work with the reason for every position: "
                        "the level (due within 24 h, someone waiting, failing automation, in flight, backlog), "
                        "the score and its largest factors, the evidence, pins, and any decision-model evidence. "
                        "Read-only. Call it when the user asks why something ranks where it does, asks for "
                        "priorities, or wants to choose between items."),
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 10"},
        }, "additionalProperties": False},
    },
    "focus_pin": {
        "name": "focus_pin",
        "description": ("Pin or unpin an item. A pin is the user's own word that the item matters: it lifts the "
                        "item above inferred work (a deadline inside 24 h still comes first). Only call when the "
                        "user says so."),
        "parameters": {"type": "object", "properties": {
            "candidate": {"type": "string", "description": "Candidate id, ref, unique prefix, or list number"},
            "pinned": {"type": "boolean", "description": "false to unpin; default true"},
        }, "required": ["candidate"], "additionalProperties": False},
    },
}


def focus_now(args: dict, **kw: Any) -> str:
    try:
        d = service().now(exclude_session=kw.get("session_id"))
        return _ok(decision=d, card=render.card(d), **_visual(d))
    except Exception as exc:
        logger.exception("focus_now failed")
        return _err(exc)


def focus_accept(args: dict, **kw: Any) -> str:
    try:
        r = service().accept(args.get("candidate"), minutes=args.get("minutes"),
                             session_id=kw.get("session_id"), reason=args.get("reason", ""))
        return _ok(**r, **_visual(session_id=kw.get("session_id")))
    except Exception as exc:
        return _err(exc)


def focus_done(args: dict, **kw: Any) -> str:
    try:
        r = service().done(args.get("proof", ""), session_id=kw.get("session_id"))
        return _ok(completed=r["completed"], next_card=render.card(r["next"]), **_visual(r["next"]))
    except Exception as exc:
        return _err(exc)


def focus_defer(args: dict, **kw: Any) -> str:
    try:
        r = service().defer(args.get("candidate"), hours=float(args.get("hours", 24)),
                            reason=args.get("reason", ""), session_id=kw.get("session_id"))
        return _ok(**r, **_visual(session_id=kw.get("session_id")))
    except Exception as exc:
        return _err(exc)


def focus_add(args: dict, **kw: Any) -> str:
    try:
        return _ok(**service().add(args.get("title", ""), next_action=args.get("next_action", ""),
                                   deadline=args.get("deadline"), done_when=args.get("done_when", "")))
    except Exception as exc:
        return _err(exc)


def focus_rank(args: dict, **kw: Any) -> str:
    try:
        r = service().rank(limit=int(args.get("limit", 10)), exclude_session=kw.get("session_id"))
        return _ok(ranking=r, text=render.why(r))
    except Exception as exc:
        logger.exception("focus_rank failed")
        return _err(exc)


def focus_pin(args: dict, **kw: Any) -> str:
    try:
        r = service().pin(args.get("candidate"), on=bool(args.get("pinned", True)), session_id=kw.get("session_id"))
        return _ok(**r, **_visual(session_id=kw.get("session_id")))
    except Exception as exc:
        return _err(exc)


HANDLERS = {"focus_now": focus_now, "focus_accept": focus_accept, "focus_done": focus_done,
            "focus_defer": focus_defer, "focus_add": focus_add, "focus_rank": focus_rank,
            "focus_pin": focus_pin}


# ---- /now ------------------------------------------------------------------------------------

USAGE = """/now                          show the focus, or the proposal
/now accept [n|id] [min]      lease it (default: the proposal, 90 min)
/now done <proof>             complete it with proof, get the next one
/now defer [n|id] [hours]     snooze it (default 24 h)
/now add <what> [by <when>]   record a commitment (+3h, tomorrow, 2026-11-11 17:00)
/now why [n]                  the whole ranking and the reason for every position
/now pin <n|id>               say an item matters; /now unpin <n|id> takes it back
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
        if verb in ("why", "rank", "list", "all"):
            limit = int(rest[0]) if rest and rest[0].isdigit() else 10
            return render.why(svc.rank(limit=limit, exclude_session=session_id))
        if verb in ("pin", "unpin"):
            r = svc.pin(rest[0] if rest else None, on=verb == "pin", session_id=session_id)
            return (f"{'Pinned' if r['pinned'] else 'Unpinned'}: {r['title']}\n\n"
                    + render.card(svc.now(exclude_session=session_id)))
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


def _speaks() -> bool:
    """Whether Focus may put anything in front of the model. Off by default: Focus is a pane, a chip
    and a command the user opens, and it stays out of every other chat unless the user asks for it."""
    try:
        return bool(_ctx is not None and _ctx.get_config("drift_guard", False))
    except Exception:
        return False


def on_pre_llm_call(session_id=None, user_message=None, platform=None, parent_session_id=None, **_: Any):
    if _unattended(platform, parent_session_id) or not isinstance(user_message, str):
        return None
    try:
        # Always count the turn (the pane shows on-focus and drifted turns). Only speak when asked to.
        note = service().on_turn(user_message, session_id=session_id, nudge=_speaks())
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
    if not _speaks():
        return ""
    try:
        return service().prompt_section()
    except Exception:
        return ""


# ---- CLI ---------------------------------------------------------------------------------------

def _cli_setup(parser) -> None:
    parser.add_argument("args", nargs="*", help="now | accept [n] [min] | done <proof> | defer [n] [h] | add <what> [by <when>] | why | pin <n> | unpin <n> | review")
    parser.add_argument("--html", action="store_true", help="write the visual card and print its path")
    parser.add_argument("--json", action="store_true", help="print the raw decision as JSON")


def _cli_handler(args) -> int:
    if getattr(args, "html", False):
        print(service().write_card() or "could not write the card")
    elif getattr(args, "json", False):
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
    ctx.register_command("now", _slash, description="One thing to work on now", args_hint="[accept|done|defer|add|why|pin|review]")
    ctx.register_cli_command("focus", "One thing to work on now", _cli_setup, _cli_handler,
                             description="Show, lease, complete or defer your one focus.")
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("kanban_task_completed", on_kanban_task_completed)
    ctx.register_system_prompt_section("focus.current", _prompt_section, max_chars=600)
