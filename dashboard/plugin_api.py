"""Focus backend routes for the Desktop pane: /api/plugins/focus/...

GET  /state            the current decision (leased focus or proposal) plus today's counts
POST /accept           {candidate?, minutes?}
POST /done             {proof}
POST /defer            {candidate?, hours?}
POST /add              {title, next_action?, deadline?}
GET  /review?days=7

Every route runs the same deterministic engine as the /now command. Nothing leaves the machine.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body

_LIB = Path(__file__).resolve().parent.parent / "hermes_focus"


def _load_lib():
    """Load the plugin's own engine package by path. The dashboard imports this file on its own,
    outside the agent's plugin package, so a relative import has no parent to resolve against."""
    name = "hermes_focus_dashboard_lib"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _LIB / "__init__.py",
                                                  submodule_search_locations=[str(_LIB)])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load focus engine from {_LIB}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_lib()
from hermes_focus_dashboard_lib import host, render  # noqa: E402
from hermes_focus_dashboard_lib.service import FocusService  # noqa: E402

router = APIRouter()


def _svc() -> FocusService:
    # Built per request: host paths follow the active profile.
    return FocusService(host.hermes_home(), host.data_dir(), kanban_db=host.kanban_db())


def _wrap(fn) -> dict[str, Any]:
    try:
        return {"ok": True, **fn()}
    except (ValueError, LookupError) as exc:
        return {"ok": False, "error": str(exc)}


def _state() -> dict[str, Any]:
    d = _svc().now()
    return {"decision": d, "card": render.card(d)}


@router.get("/state")
async def state() -> dict:
    return _wrap(_state)


@router.post("/accept")
async def accept(body: dict = Body(default={})) -> dict:
    return _wrap(lambda: {**_svc().accept(body.get("candidate"), minutes=body.get("minutes")), **_state()})


@router.post("/done")
async def done(body: dict = Body(default={})) -> dict:
    return _wrap(lambda: {"completed": _svc().done(str(body.get("proof", "")))["completed"], **_state()})


@router.post("/defer")
async def defer(body: dict = Body(default={})) -> dict:
    return _wrap(lambda: {**_svc().defer(body.get("candidate"), hours=float(body.get("hours", 24))), **_state()})


@router.post("/add")
async def add(body: dict = Body(default={})) -> dict:
    return _wrap(lambda: {**_svc().add(str(body.get("title", "")), next_action=str(body.get("next_action", "")),
                                       deadline=body.get("deadline")), **_state()})


@router.get("/review")
async def review(days: int = 7) -> dict:
    return _wrap(lambda: {"review": _svc().review(max(1, min(int(days), 90)))})
