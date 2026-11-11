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


def settings() -> dict:
    """``plugins.entries.focus.settings`` from the active profile's config. Empty on any failure."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as load
        except ImportError:
            from hermes_cli.config import load_config as load
        entry = ((load() or {}).get("plugins") or {}).get("entries", {}).get(PLUGIN_NAME) or {}
        found = entry.get("settings") or entry.get("config") or {}
        return found if isinstance(found, dict) else {}
    except Exception:
        return {}


def secret(name: str) -> str | None:
    """Credentials come through Hermes's secret scope only (catalog rule 11)."""
    try:
        from agent.secret_scope import get_secret
        return get_secret(name) or None
    except Exception:
        return None


def build_service(get_config=None, llm=None):
    """One factory for the agent half and the dashboard route, so both read the same settings."""
    from . import judge as judge_mod
    from .service import FocusService

    if get_config is None:
        cfg = settings()
        get_config = lambda key, default=None: cfg.get(key, default)  # noqa: E731
    data = data_dir()
    judge, status = judge_mod.build(get_config, data, secret=secret, llm=llm)
    return FocusService(hermes_home(), data,
                        lease_minutes=int(get_config("lease_minutes", 90)),
                        window_days=float(get_config("session_window_days", 3)),
                        kanban_db=kanban_db(), judge=judge, judge_status=status)
