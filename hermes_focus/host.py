"""Where Focus finds Hermes state and keeps its own. The only module that touches Hermes imports, and
each import is optional so the engine still runs (and tests) without Hermes installed."""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_NAME = "focus"


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # follows the active profile
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def data_dir() -> Path:
    """``<hermes home>/plugin-data/focus``: shared by the agent half and the dashboard route."""
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir(PLUGIN_NAME))
    except Exception:
        path = hermes_home() / "plugin-data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path


def kanban_db() -> Path | None:
    env = os.environ.get("HERMES_KANBAN_DB")
    return Path(env) if env else None
