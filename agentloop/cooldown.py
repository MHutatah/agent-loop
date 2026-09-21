"""When the subscription says "resets 4am", believe it and stop until then.

WHY THIS EXISTS. Recognising a limit was fixed in #19; acting on it was not.
`_collect_finished` released a limited issue untouched so a later tick could
retry it, and the later tick is ten minutes away, so the loop retried into a
limit with hours left to run. Measured on the box over three days: 238 builder
spawns, 178 of them killed by a usage limit within two minutes, the same eight
issues respawned 25 to 28 times each.

That is not merely wasted effort, it is the reason the window never recovers.
A doomed spawn is not free: the agent starts, pays for a fresh prompt prefix
(both CLAUDE.md files, the tool definitions, the issue body, the repo context)
and only then reads the refusal and dies. Those prefixes were 22 to 28% of
everything the loop spent. The loop was buying its next window before the
window opened, which is why a reset drained in half an hour.

So the reset time gets recorded and honoured. The notice already carries it
("resets 4am (UTC)") and `looks_limited` was throwing it away after matching
on it.

ACCOUNT-WIDE, NOT PER REPO. One subscription funds every repo, so a limit seen
while working on one must stop them all. The file therefore lives in the shared
workspace root beside judge-budget.json, not under repos/<key>/.

This blocks STARTING work. Collecting finished agents and reaping stranded ones
cost no tokens and free state that a limit makes more valuable, so they carry
on: see the guard placement in watchers.py.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("agentloop.cooldown")

# "resets 4am (UTC)", "resets at 2:10am", "resets 3pm", "resets 23:00".
# The hour is required and everything else is optional, because the observed
# strings in tests/test_limits.py differ in every other part.
_RESET = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)

# Used when a notice says we are limited but names no time. Long enough to
# break the ten-minute respawn cycle that caused this, short enough that
# misreading a notice costs one window rather than a day.
BLIND_WAIT = timedelta(minutes=30)


def resets_at(text: str, *, now: datetime | None = None) -> datetime | None:
    """The UTC moment a limit notice says the window reopens, or None.

    Times are read as UTC because that is what the CLI prints, and a loop that
    guessed the local zone would be wrong by hours on this box.
    """
    m = _RESET.search(text or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if hour > 23 or minute > 59:
        return None
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    now = now or datetime.now(UTC)
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # "resets 4am" read at 11pm means tomorrow's 4am. Without this the cooldown
    # lands in the past and the spiral this module exists to stop resumes.
    if when <= now:
        when += timedelta(days=1)
    return when


def _path(workspace_root: str | Path) -> Path:
    return Path(workspace_root) / "limit-cooldown"


def blocked_until(workspace_root: str | Path, *,
                  now: datetime | None = None) -> datetime | None:
    """When work may start again, or None if it may start now.

    An unreadable or malformed file reads as "not limited": this gate must fail
    towards working, because the failure it prevents is expensive and the
    failure it would cause by jamming shut is total.
    """
    try:
        raw = _path(workspace_root).read_text(encoding="utf-8").strip()
        when = datetime.fromisoformat(raw)
    except (OSError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return when if when > now else None


def note_limit(workspace_root: str | Path, text: str, *,
               now: datetime | None = None) -> datetime:
    """Record that we are limited, and until when. Returns the effective time."""
    now = now or datetime.now(UTC)
    when = resets_at(text, now=now) or now + BLIND_WAIT
    # NEVER SHORTEN AN EXISTING COOLDOWN. Several agents report the same limit
    # within seconds of each other and only some of their messages name the
    # time, so a blind 30 minutes must not overwrite a parsed 4am.
    current = blocked_until(workspace_root, now=now)
    if current and current >= when:
        return current
    p = _path(workspace_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(when.isoformat(), encoding="utf-8")
    log.info("usage limit: holding all repos until %s", when.isoformat())
    return when
