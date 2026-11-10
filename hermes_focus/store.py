"""Plugin-owned durable state: one JSON file, atomic writes, an advisory lock across processes."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows: single-process best effort
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
HISTORY_CAP = 200
STATS_DAYS_CAP = 60


def empty() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "focus": None, "history": [], "deferred": {},
            "commitments": [], "stats": {}, "nudged": {}}


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.path = self.dir / "focus.json"
        self.lock_path = self.dir / "focus.lock"

    def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty()
        except (OSError, ValueError):
            # A corrupt file must not take the agent down. Keep it for inspection, start clean.
            with contextlib.suppress(OSError):
                self.path.replace(self.path.with_suffix(".corrupt.json"))
            return empty()
        base = empty()
        if isinstance(data, dict):
            base.update({k: v for k, v in data.items() if k in base})
        return base

    @contextlib.contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Read-modify-write under an exclusive lock. Changes persist when the block exits cleanly."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = self.read()
                yield state
                self._trim(state)
                self._write(state)
            finally:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _write(self, state: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".focus-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @staticmethod
    def _trim(state: dict[str, Any]) -> None:
        state["history"] = state["history"][-HISTORY_CAP:]
        days = sorted(state["stats"])
        for d in days[:-STATS_DAYS_CAP]:
            state["stats"].pop(d, None)
        if len(state["nudged"]) > 200:
            keep = sorted(state["nudged"].items(), key=lambda kv: kv[1])[-100:]
            state["nudged"] = dict(keep)
        state["commitments"] = [c for c in state["commitments"] if c.get("status") == "open"][-100:] + \
            [c for c in state["commitments"] if c.get("status") != "open"][-50:]
