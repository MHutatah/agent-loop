"""Invoking the subscription CLIs headlessly, and detecting when they're throttled.

Both `codex exec` and `claude -p` run non-interactively against a consumer
subscription, so this is the same trick triad uses — shell out, read stdout, and
recognise a rate-limit reply as a distinct outcome from a failure, because the
right response to "limited" is to wait, not to retry or escalate.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass

log = logging.getLogger("agentloop.runner")

# Phrases either CLI emits when the plan's quota is exhausted.
_LIMIT_PATTERNS = re.compile(
    r"rate.?limit|usage limit|quota|too many requests|429|"
    r"limit reached|try again (later|in)|upgrade your plan",
    re.I,
)


@dataclass
class AgentResult:
    ok: bool
    text: str = ""
    limited: bool = False
    error: str = ""

    @property
    def retryable_later(self) -> bool:
        return self.limited


def looks_limited(text: str) -> bool:
    return bool(_LIMIT_PATTERNS.search(text or ""))


def invoke(cmd: list[str], prompt: str, *, cwd: str | None = None,
           timeout: int = 1800, dry: bool = False) -> AgentResult:
    """Run one agent CLI once. Never raises for CLI failure — returns ok=False."""
    if dry:
        log.info("[dry-run] %s (cwd=%s) prompt=%d chars", cmd[0], cwd, len(prompt))
        return AgentResult(ok=True, text='{"pass": true, "score": 9, "blocking": [], '
                                         '"notes": "dry-run stub"}')
    try:
        # stdin MUST be closed. Both CLIs read stdin when it is a pipe and will
        # silently swallow whatever is there as extra prompt text — during setup
        # a `claude -p` call inherited a shell heredoc and treated the remaining
        # script lines as instructions. An agent must only ever see the prompt
        # we hand it.
        proc = subprocess.run([*cmd, prompt], capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL,
                              cwd=cwd, timeout=timeout)
    except FileNotFoundError:
        return AgentResult(False, error=f"CLI not found: {cmd[0]!r} — is it installed?")
    except subprocess.TimeoutExpired:
        return AgentResult(False, error=f"timed out after {timeout}s")

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        return AgentResult(False, limited=looks_limited(combined),
                           error=(proc.stderr or "non-zero exit").strip()[:400])
    if not (proc.stdout or "").strip() and looks_limited(combined):
        return AgentResult(False, limited=True, error="provider reported a usage limit")
    return AgentResult(True, text=proc.stdout or "")


def extract_json(text: str) -> dict | None:
    """Pull the outermost JSON object out of a model reply.

    `claude -p --output-format json` wraps the answer in an envelope, and models
    add prose or fences regardless of instructions, so parse defensively.
    """
    if not text:
        return None
    # Claude's envelope: {"type":"result","result":"<the actual text>",...}
    try:
        env = json.loads(text)
        if isinstance(env, dict) and "result" in env and isinstance(env["result"], str):
            text = env["result"]
        elif isinstance(env, dict) and "pass" in env:
            return env
    except (json.JSONDecodeError, TypeError):
        pass
    text = re.sub(r"```[a-z]*", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
