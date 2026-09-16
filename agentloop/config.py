"""Configuration — every knob that decides what the agents may touch.

Deliberately explicit: this file is the security boundary. An autonomous loop with
merge rights is only as safe as the limits declared here.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# ── labels: the control surface you drive from your phone ────────────────────
LABEL_READY = "agent:ready"          # you add this -> an agent picks the issue up
LABEL_WIP = "agent:working"          # the loop adds this so it never double-starts
LABEL_PR = "agent:pr"                # marks PRs this system owns
LABEL_NEEDS_HUMAN = "agent:needs-human"   # gave up / hit a cap / touched a guarded path
LABEL_STOP = "agent:stop"            # kill switch: on the issue OR the repo's stop-issue

# All five must exist in the repo. Each is load-bearing, and a missing one fails
# quietly somewhere different, so `doctor` checks for them up front.
CONTROL_LABELS = (LABEL_READY, LABEL_WIP, LABEL_PR, LABEL_NEEDS_HUMAN, LABEL_STOP)

# Stamped into every comment the loop posts. "Did a human ask for changes?" has to
# mean "a comment we did not write" — the loop pushes under your own account,
# so authorship cannot distinguish us from him, and it read its own fix-up comment
# as a review request and re-fixed the same PR forever.
MARKER = "<!-- agent-loop -->"

# ── models ───────────────────────────────────────────────────────────────────
# Codex implements (roomier ChatGPT Plus quota); Claude judges (few, high-leverage
# calls) so the Claude Pro budget stays available for interactive work.
#
# `--sandbox workspace-write` lets the agent edit the worktree it is running in
# but nothing outside it. (`--full-auto` is the deprecated spelling.) Codex also
# refuses to run outside a git repo unless told otherwise — our worktrees are
# repos, so that check is a useful backstop and is left on.
# THE IMPLEMENTER IS STILL CODEX, AND THAT IS A SANDBOX DECISION, NOT A MODEL ONE.
#
# `--sandbox workspace-write` confines it to the worktree it was started in. The
# `claude` CLI has no equivalent flag: checked on 2.1.220, the options are
# --permission-mode, --allowedTools/--disallowedTools and
# --dangerously-skip-permissions, none of which is a filesystem boundary, and
# the agent needs Bash to run the repo's test command, so a tool allowlist does
# not confine it either. This box runs sanad, nolog, hermes, litellm, matrix,
# ipacommunity and ipauat, which are services other people use, so swapping a
# confined implementer for an unconfined one is a real regression that a newer
# model does not pay for.
#
# Set AGENTLOOP_IMPLEMENTER to override, space-separated, e.g.
#   AGENTLOOP_IMPLEMENTER="claude -p --model claude-opus-5 --dangerously-skip-permissions"
# Two things change if you do. Claude Code can write .git, which is what the
# codex sandbox blocks and therefore the reason a conflicting pull request
# cannot currently be rebased (see the loop's own issue on that). And the whole
# loop then draws on one subscription rather than two, so total throughput falls
# even though each agent is stronger.
IMPLEMENTER = (os.environ.get("AGENTLOOP_IMPLEMENTER", "").split()
               or ["codex", "exec", "--sandbox", "workspace-write"])

# THE JUDGE RUNS ON OPUS 5, the current most capable Opus-tier model.
#
# It used to be Sonnet, chosen when the loop shared a Pro plan with interactive
# work and the quota difference per review decided whether the day got a handful
# of pull requests or many. On Max that constraint is gone, and the judge is the
# one place in this system where being right matters more than being cheap: it
# is a small number of high-leverage calls, and a verdict it gets wrong either
# merges a defect or holds good work for a day.
#
# Exact model IDs only, never a date-suffixed variant. "opus"/"sonnet" bare
# aliases resolve to whatever the CLI decides is current, which is precisely the
# ambiguity worth removing from a file that gates merges.
JUDGE_MODEL = os.environ.get("AGENTLOOP_JUDGE_MODEL", "claude-opus-5")
# READ-ONLY TOOLS, and a working directory, because a diff is not enough.
#
# The judge's own docstring promised to decide whether "an acceptance criterion
# is unmet, or met only superficially", and it was handed the diff as text with
# no repository and no tools. It cannot see the function a changed line calls,
# whether a new test asserts anything, or whether the thing it is asked to
# verify already existed. Anthropic's own reviewer for this job, the
# `code-review` plugin shipped for claude-code-action, reads the repository;
# a diff-only reviewer is the cheap version of a different, weaker task.
#
# Read, Grep and Glob only. The judge must not edit the branch it is judging,
# and it must not run the tests: CI already does that, and gate.py refuses to
# merge without it, so a judge that could run them would only be able to
# disagree with the authority.
# FLAG ORDER MATTERS AND IT IS NOT COSMETIC. `--allowedTools <tools...>` is
# VARIADIC, so any positional argument after it is swallowed as another tool
# name. runner.invoke() appends the prompt as the final positional argument, so
# with --allowedTools last the judge ran with no prompt at all and the CLI
# answered "Input must be provided either through stdin or as a prompt argument
# when using --print". Every review came back unusable, and because an unusable
# verdict used to read as a rejection, three PRs were failed three times each
# and escalated with an empty 0/10 review. Keep a single-value flag last.
JUDGE = ["claude", "-p", "--allowedTools", "Read,Grep,Glob",
         "--output-format", "json", "--model", JUDGE_MODEL]


@dataclass
class Repo:
    """One repository the loop is allowed to act on."""

    slug: str                      # "owner/repo"
    default_branch: str = "main"
    auto_merge: bool = False
    test_cmd: str = "python -m pytest -q"


@dataclass
class Config:
    repos: list[Repo] = field(default_factory=list)

    # Concurrency: two is a sane default for a small VPS that runs other things.
    max_concurrent_agents: int = 2

    # And a per-repository ceiling. Capacity used to be global only, and the
    # scheduler walked `cfg.repos` in order handing every free slot to whoever
    # was listed first, so a second project did not run slowly, it never ran.
    max_concurrent_per_repo: int = 1

    # An agent gets this many attempts at one issue (initial + fixes) before the
    # loop stops and hands it to a human. Without this a confused agent can burn
    # a whole day's quota looping on the same failure.
    max_attempts_per_issue: int = 3

    # Wall-clock ceiling for a single agent invocation.
    agent_timeout_s: int = 1800

    # Judge calls share your Claude quota with your own interactive use, so the
    # loop keeps a reserve: it stops judging well before the plan's limit rather
    # than leaving you unable to use Claude yourself. Codex is unaffected — it
    # implements on the separate ChatGPT subscription.
    max_judge_calls: int = 400
    judge_window_hours: float = 5.0

    # Paths an agent may never change and still auto-merge. Touching one forces
    # human review: these are the things that could disable the guardrails
    # themselves or leak credentials.
    guarded_paths: tuple[str, ...] = (
        ".github/workflows/", ".github/actions/",
        "LICENSE", "LICENSE-DATA",
        ".env", "secrets", "deploy/",
        "agentloop/config.py",
    )

    dry_run: bool = False

    @classmethod
    def load(cls) -> Config:
        # Three layers, deliberately: which repos and whether they may auto-merge
        # come from agentloop.toml (yours to declare); day-to-day tunables come
        # from settings.json so they can be nudged from a phone; everything
        # security-relevant stays here in code, where changing it needs a deploy.
        from agentloop.settings import Settings

        s = Settings.load()
        return cls(
            repos=_load_repos(),
            max_judge_calls=s.max_judge_calls,
            max_concurrent_agents=s.max_concurrent_agents,
            max_concurrent_per_repo=s.max_concurrent_per_repo,
            max_attempts_per_issue=s.max_attempts_per_issue,
            dry_run=os.environ.get("AGENTLOOP_DRY_RUN", "") == "1",
        )


def config_path() -> Path:
    """Where the repo list lives. Explicit env var, else next to the project,
    else the XDG-ish location — so a clone works with no arguments."""
    env = os.environ.get("AGENTLOOP_CONFIG")
    if env:
        return Path(env)
    local = Path(__file__).resolve().parent.parent / "agentloop.toml"
    if local.exists():
        return local
    return Path.home() / ".config" / "agentloop" / "config.toml"


def _load_repos() -> list[Repo]:
    """Repos the loop may act on. Empty is a valid, safe answer: with no config
    the loop simply does nothing rather than guessing at a repository."""
    path = config_path()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out = []
    for r in data.get("repo", []):
        slug = r.get("slug")
        if not slug:
            continue
        out.append(Repo(
            slug=slug,
            default_branch=r.get("default_branch", "main"),
            # auto_merge is opt-in per repo: letting an agent merge unattended
            # should always be a decision someone typed, never a default.
            auto_merge=bool(r.get("auto_merge", False)),
            test_cmd=r.get("test_cmd", "python -m pytest -q"),
        ))
    return out


def touches_guarded_path(files: list[str], cfg: Config) -> list[str]:
    """Which changed files fall under a guarded path (blocks auto-merge)."""
    return [f for f in files
            if any(f == g or f.startswith(g) for g in cfg.guarded_paths)]
