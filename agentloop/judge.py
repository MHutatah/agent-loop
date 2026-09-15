"""The judge: Claude reviews Codex's PR against the issue's acceptance criteria.

Cheap by construction — one call, no repo, no tools. The diff is fetched with
`gh pr diff` and passed as text, so judging costs a single Claude message rather
than an agent session. That matters on a Pro plan whose quota is shared with your
own interactive work.

The verdict gates auto-merge, so it is deliberately biased toward NOT passing:
an unmet criterion, a missing test, or an unrelated change all block.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agentloop.config import JUDGE
from agentloop.runner import AgentResult, extract_json, invoke

log = logging.getLogger("agentloop.judge")

PROMPT = """You are reviewing a pull request written by another AI agent.

Decide ONLY whether it correctly and safely implements the issue below. You are the
last gate before this merges automatically with no human reading it, so withhold a
pass unless the work is genuinely complete.

FAIL if any of these are true:
- an acceptance criterion is unmet, or met only superficially
- behaviour changed but no test covers it
- the diff contains changes unrelated to the issue
- it weakens a guardrail, a licence, CI, or anything security-relevant
- it deletes or disables tests to make them pass
- a test was added that does not actually assert the behaviour it names

You are in a checkout of the repository at this commit. READ THE SURROUNDING
CODE before ruling: open the files the diff touches, follow the functions it
calls, and check that a new test asserts something that would fail if the
change were reverted. A criterion is not met because a plausible-looking line
appeared, and "no test covers it" is a claim you can verify rather than
guess.

=== ISSUE #{number}: {title} ===
{body}

=== DIFF ===
{diff}

Reply with ONLY a JSON object, no prose and no code fences:
{{"pass": <true|false>, "score": <1-10>,
  "blocking": ["specific, actionable problem", ...],
  "notes": "one or two sentences"}}
"""


@dataclass
class Verdict:
    passed: bool
    score: int = 0
    blocking: list[str] = field(default_factory=list)
    notes: str = ""
    error: str = ""
    limited: bool = False

    @property
    def usable(self) -> bool:
        """Did we actually get a verdict? An error is NOT a pass."""
        return not self.error

    def as_comment(self) -> str:
        head = ("**Judge: PASS**" if self.passed else "**Judge: CHANGES REQUESTED**")
        lines = [f"{head} — score {self.score}/10", "", self.notes or ""]
        if self.blocking:
            lines += ["", "**Blocking:**", *[f"- {b}" for b in self.blocking]]
        lines += ["", "_Reviewed by Claude against the issue's acceptance criteria._"]
        return "\n".join(lines)


def judge_pr(issue: dict, diff: str, *, cwd: str | None = None,
             timeout: int = 600, dry: bool = False) -> Verdict:
    """Review one PR. `cwd` is the worktree, and passing it is what lets the
    judge open the files around the diff instead of guessing from context
    lines. Without it the review is still attempted, so a missing worktree
    degrades the verdict rather than dropping it."""
    prompt = PROMPT.format(number=issue.get("number", "?"),
                           title=issue.get("title", ""),
                           body=(issue.get("body") or "")[:8000],
                           diff=diff or "(empty diff)")
    res: AgentResult = invoke(JUDGE, prompt, cwd=cwd, timeout=timeout, dry=dry)
    if not res.ok:
        return Verdict(False, error=res.error, limited=res.limited)

    data = extract_json(res.text)
    if not isinstance(data, dict) or "pass" not in data:
        # An unparseable verdict must never read as approval.
        return Verdict(False, error="judge returned no usable verdict",
                       notes=(res.text or "")[:300])
    return Verdict(
        passed=bool(data.get("pass")),
        score=int(data.get("score") or 0),
        blocking=[str(b) for b in (data.get("blocking") or [])],
        notes=str(data.get("notes") or ""),
    )
