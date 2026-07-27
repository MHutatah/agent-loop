"""Reading real quota state out of the agent CLIs.

Neither CLI exposes a `usage` subcommand, but Codex records the server's own
rate-limit view in its session rollout files — including the weekly window and
exactly when it resets. That is far better than inferring quota from our own call
counts, because it reflects everything the account spent, not just what this loop
did.

Claude has no equivalent on disk, so its side is the budget this loop enforces
itself (agentloop/budget.py). The two are reported side by side but they mean
different things, and the UI says so rather than implying a false symmetry.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"


@dataclass
class Window:
    used_percent: float
    window_minutes: int
    resets_at: int          # unix seconds

    @property
    def label(self) -> str:
        m = self.window_minutes
        if m >= 10080:
            return f"{m // 10080}-week" if m // 10080 > 1 else "weekly"
        if m >= 1440:
            return f"{m // 1440}-day"
        return f"{m // 60}h"

    @property
    def resets_in(self) -> str:
        secs = max(0, self.resets_at - int(time.time()))
        if secs >= 86400:
            return f"{secs // 86400}d {secs % 86400 // 3600}h"
        if secs >= 3600:
            return f"{secs // 3600}h {secs % 3600 // 60}m"
        return f"{secs // 60}m"


@dataclass
class CodexUsage:
    primary: Window | None = None
    secondary: Window | None = None
    total_tokens: int = 0
    checked_at: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.primary is not None


def _window(d: dict | None) -> Window | None:
    if not isinstance(d, dict) or d.get("used_percent") is None:
        return None
    return Window(float(d.get("used_percent") or 0),
                  int(d.get("window_minutes") or 0),
                  int(d.get("resets_at") or 0))


def codex_usage(max_files: int = 6) -> CodexUsage:
    """Most recent rate-limit snapshot Codex received from the server.

    Scans newest sessions backwards: a session that ended before any turn
    completed carries no snapshot, so the newest file is not always the useful one.
    """
    try:
        files = sorted(CODEX_SESSIONS.rglob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    except OSError as exc:
        return CodexUsage(error=str(exc))
    if not files:
        return CodexUsage(error="no Codex sessions yet")

    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                blob = json.loads(line)
            except json.JSONDecodeError:
                continue
            info = _find(blob, "rate_limits")
            if not isinstance(info, dict):
                continue
            tokens = _find(blob, "total_token_usage") or {}
            return CodexUsage(
                primary=_window(info.get("primary")),
                secondary=_window(info.get("secondary")),
                total_tokens=int((tokens or {}).get("total_tokens") or 0),
                checked_at=f.stat().st_mtime,
            )
    return CodexUsage(error="no rate-limit snapshot recorded yet")


def _find(obj, key: str):
    """Depth-first search for a key — the rollout schema nests it differently
    across Codex versions, so don't depend on an exact path."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find(v, key)
            if found is not None:
                return found
    return None
