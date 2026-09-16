"""Runtime-tunable knobs, adjustable from the phone.

`config.py` is the security boundary and a guarded path — nothing should edit it
at runtime, least of all a web button. So the handful of values that legitimately
change day to day live here instead, in a small JSON file.

The split is deliberate: this file can only move numbers within bounds it declares.
It cannot widen what agents may touch, add a repository, or turn a guardrail off —
those stay in code, where changing them requires a deploy and a human.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

PATH = Path.home() / "agent-loop-work" / "settings.json"

# (minimum, maximum) for every tunable. A stuck finger or a bad request cannot
# push the judge budget somewhere that would drain the plan.
BOUNDS = {
    # 0 means UNLIMITED, not "never judge". The old ceiling of 60 was sized for
    # a Pro plan; on Max the guard is only here to stop a crash loop burning the
    # quota, which it has done once already.
    "max_judge_calls": (0, 5000),
    "max_concurrent_agents": (1, 8),
    "max_concurrent_per_repo": (1, 4),
    "max_attempts_per_issue": (1, 6),
}


@dataclass
class Settings:
    max_judge_calls: int = 400
    max_concurrent_agents: int = 2
    # Per repository, so one busy project cannot take every slot. Raising the
    # global cap without this just moves the starvation rather than fixing it.
    max_concurrent_per_repo: int = 1
    max_attempts_per_issue: int = 3

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        s = cls()
        for k, (lo, hi) in BOUNDS.items():
            if isinstance(data.get(k), int):
                setattr(s, k, max(lo, min(hi, data[k])))
        return s

    def save(self) -> None:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")

    def adjust(self, key: str, delta: int) -> str:
        """Nudge one value, clamped. Returns a line to show the user."""
        if key not in BOUNDS:
            return f"unknown setting: {key}"
        lo, hi = BOUNDS[key]
        old = getattr(self, key)
        new = max(lo, min(hi, old + delta))
        setattr(self, key, new)
        self.save()
        if new == old:
            return f"{key} stays {old} (limit {lo}–{hi})"
        return f"{key}: {old} → {new}"
