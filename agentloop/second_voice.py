"""A second opinion, from a different model family, on the changes that matter.

The judge is Claude reviewing a diff against the issue's acceptance criteria. It
is good at that and structurally blind to one thing: whether the criteria
themselves were right. Two pull requests landed the same week where each passed
its own criteria and they contradicted each other in the same function, because
one issue superseded the other's spec and nothing in the pipeline held both at
once.

So this is not a second judge. It is asked a different question, and only about
changes where being wrong is expensive: does this change look safe, and is the
issue asking for the right thing. A different model family answers, so a shared
blind spot is less likely than it is between two Claude calls.

ADVISORY, AND SILENT WHEN IT FAILS. A consultation that errors, times out, or
returns nothing usable produces None and the pipeline proceeds exactly as it
would have. That is deliberate: `astra` is unreachable on a ChatGPT account
today (see config.SECOND_VOICE_MODEL), and a second opinion that blocks merges
when it cannot be obtained would be worse than not having one. Its dissent is
consequential; its absence is not.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from agentloop.config import SECOND_VOICE, Config, touches_guarded_path
from agentloop.runner import invoke

log = logging.getLogger("agentloop.second_voice")

# Paths where a wrong merge is not undone by reverting the commit. Deliberately
# broader than gate.py's guarded_paths, which exists to block auto-merge; this
# list decides only whether a second opinion is worth its cost.
CRITICAL_PATHS = (
    "lib/roles.ts", "lib/admin.ts", "lib/auth.ts", "proxy.ts",
    "lib/db.ts", "lib/notify.ts", "lib/audience.ts",
    "migrations/", "deploy/", ".github/workflows/",
)

CRITICAL_LABELS = ("concern:privacy", "concern:copyright", "p0-critical")

PROMPT = """You are a second reviewer on a pull request that another AI agent
wrote and another AI model has already approved. You are here because this
change touches something where being wrong is expensive and cannot be undone by
reverting a commit.

Do NOT re-review the code against the issue. That has been done. Answer two
questions the first reviewer structurally cannot:

1. Is the ISSUE asking for the right thing? An acceptance criterion can be
   faithfully implemented and still be wrong, or be superseded by a later
   decision nobody told this agent about.
2. Does this change create a way for the system to be wrong SILENTLY? Lost
   data, a permission that widens, a guard that reports success while doing
   nothing, an identity written where an identifier was meant.

Be concrete or say nothing. "Consider adding tests" is noise. If you see
nothing, say so plainly.

=== ISSUE #{number}: {title} ===
{body}

=== DIFF ===
{diff}

Reply with ONLY a JSON object, no prose and no fences:
{{"concern": <true|false>, "summary": "one sentence", "points": ["specific", ...]}}
"""


@dataclass
class Opinion:
    concern: bool
    summary: str = ""
    points: list[str] | None = None

    def as_comment(self, model: str) -> str:
        head = ("**Second voice: CONCERN**" if self.concern
                else "**Second voice: no objection**")
        lines = [head, "", self.summary or ""]
        for p in self.points or []:
            lines.append(f"- {p}")
        lines += ["", f"_A second opinion from `{model}`, a different model "
                      "family from the judge. Advisory: it asks whether the "
                      "issue was right, not whether the code matches it._"]
        return "\n".join(lines)


def is_critical(changed_files: list[str], labels: set[str], cfg: Config) -> bool:
    """Is this a change worth a second opinion?"""
    if labels & set(CRITICAL_LABELS):
        return True
    if touches_guarded_path(changed_files, cfg):
        return True
    return any(f == c or f.startswith(c) for f in changed_files
               for c in CRITICAL_PATHS)


def consult(issue: dict, diff: str, *, cwd: str | None = None,
            timeout: int = 600, dry: bool = False) -> Opinion | None:
    """Ask the second voice. None means "no opinion available", never "fine"."""
    prompt = PROMPT.format(number=issue.get("number", "?"),
                           title=issue.get("title", ""),
                           body=(issue.get("body") or "")[:6000],
                           diff=(diff or "")[:60_000])
    res = invoke(SECOND_VOICE, prompt, cwd=cwd, timeout=timeout, dry=dry)
    if not res.ok:
        log.info("second voice unavailable: %s", res.error)
        return None
    # codex prints a transcript around the reply, so take the last JSON object
    # rather than the first: the prompt itself contains a JSON shape.
    blobs = re.findall(r"\{[^{}]*\"concern\"[^{}]*\}", res.text or "", re.S)
    if not blobs:
        log.info("second voice returned nothing usable")
        return None
    import json
    try:
        data = json.loads(blobs[-1])
    except json.JSONDecodeError:
        log.info("second voice reply would not parse")
        return None
    return Opinion(concern=bool(data.get("concern")),
                   summary=str(data.get("summary") or ""),
                   points=[str(p) for p in (data.get("points") or [])])
