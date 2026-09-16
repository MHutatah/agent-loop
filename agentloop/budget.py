"""A spend guard for the judge.

Sized for a Pro plan originally, where an enthusiastic night of reviews could
leave him unable to use Claude in the morning. On Max that is no longer the
binding constraint, so the ceiling is high and **0 means unlimited**: what
remains is a guard against a crash loop, which has burned the budget once
already by re-judging the same unchanged commit every tick.

Deliberately a file of timestamps rather than anything cleverer: it survives
restarts, needs no daemon, and is trivial to inspect or reset by hand.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("agentloop.budget")


class Budget:
    def __init__(self, path: str | Path, max_calls: int, window_hours: float = 5.0):
        self.path = Path(path)
        self.max_calls = max_calls
        self.window_s = window_hours * 3600

    def _load(self) -> list[float]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [float(t) for t in data] if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return []

    def _recent(self, now: float | None = None) -> list[float]:
        now = now or time.time()
        return [t for t in self._load() if now - t < self.window_s]

    def used(self) -> int:
        return len(self._recent())

    @property
    def unlimited(self) -> bool:
        return self.max_calls <= 0

    def remaining(self) -> int:
        if self.unlimited:
            return 1 << 30
        return max(0, self.max_calls - self.used())

    def allow(self) -> bool:
        """Is there budget for one more judge call?

        0 MEANS UNLIMITED. Read as a plain ceiling it meant "never judge", which
        would silently hold every pull request forever with a reason that reads
        like a spend decision.
        """
        return self.unlimited or self.remaining() > 0

    def record(self) -> None:
        now = time.time()
        recent = self._recent(now)
        recent.append(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(recent), encoding="utf-8")

    def reset(self) -> None:
        """Clear the counter. For when calls were spent on a bug rather than on
        real reviews — as happened when a crash loop burned five before failing."""
        self.path.unlink(missing_ok=True)

    def resets_in_min(self) -> int:
        """Minutes until the oldest call falls out of the window."""
        recent = sorted(self._recent())
        if not recent:
            return 0
        return max(0, int((self.window_s - (time.time() - recent[0])) / 60))

    def status(self) -> str:
        if self.unlimited:
            return f"{self.used()} judge calls in the last {self.window_s/3600:.0f}h (no cap)"
        return (f"{self.used()}/{self.max_calls} judge calls in the last "
                f"{self.window_s/3600:.0f}h"
                + (f", resets in {self.resets_in_min()}m" if self.used() else ""))
