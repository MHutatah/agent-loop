"""The auto-merge gate.

Every condition here must hold before a PR merges unattended. Written as one pure
function over already-gathered facts so the policy is testable without GitHub, and
so the reason for a refusal is always explicit rather than implied by control flow.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentloop.config import Config, touches_guarded_path
from agentloop.judge import Verdict


@dataclass
class GateResult:
    merge: bool
    reason: str

    def __bool__(self) -> bool:      # `if gate:` reads naturally
        return self.merge


def decide(*, repo_auto_merge: bool, checks: str, verdict: Verdict,
           changed_files: list[str], mergeable: str, cfg: Config,
           human_requested_changes: bool = False) -> GateResult:
    """Should this PR merge itself right now?

    Order matters: cheapest and most absolute refusals first, so the reason we
    surface is the most fundamental one.
    """
    if not repo_auto_merge:
        return GateResult(False, "auto-merge is disabled for this repo")

    if human_requested_changes:
        return GateResult(False, "a human left review comments — waiting for them")

    guarded = touches_guarded_path(changed_files, cfg)
    if guarded:
        return GateResult(False, f"touches guarded path(s): {', '.join(guarded[:3])}")

    if mergeable.upper() == "CONFLICTING":
        return GateResult(False, "merge conflict")

    if checks == "fail":
        return GateResult(False, "CI is failing")
    if checks == "pending":
        return GateResult(False, "CI still running")
    if checks == "unknown":
        # We could not read CI, which is a tool problem, not a fact about the
        # change. Refuse — but say so differently, because this one needs fixing
        # rather than waiting.
        return GateResult(False, "could not read CI status — refusing until it's readable")
    if checks == "none":
        # No CI at all means nothing verified the change; a judge's opinion is
        # not a substitute for a test run.
        return GateResult(False, "no CI checks configured — refusing to merge blind")

    if not verdict.usable:
        return GateResult(False, f"no usable judge verdict ({verdict.error})")
    if not verdict.passed:
        return GateResult(False, "judge requested changes")

    return GateResult(True, f"CI green and judge passed ({verdict.score}/10)")
