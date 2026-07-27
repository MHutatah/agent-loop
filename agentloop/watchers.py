"""The two loops.

issue_watcher: ready-labelled issue -> worktree -> Codex implements -> tests -> PR
pr_watcher:    open agent PR -> judge / fix CI / answer your comments -> merge or escalate

Both are single-pass functions, invoked by a systemd timer rather than looping
forever in-process: a crash then costs one tick instead of the whole service, and
`systemctl` is the on/off switch.
"""
from __future__ import annotations

import logging
from pathlib import Path

from agentloop import gh, tmux
from agentloop.budget import Budget
from agentloop.config import (
    IMPLEMENTER,
    LABEL_NEEDS_HUMAN,
    LABEL_PR,
    LABEL_READY,
    LABEL_STOP,
    LABEL_WIP,
    MARKER,
    Config,
    Repo,
)
from agentloop.gate import decide
from agentloop.judge import Verdict, judge_pr
from agentloop.runner import invoke, looks_limited
from agentloop.worktree import Workspace, branch_name

log = logging.getLogger("agentloop.watchers")

IMPLEMENT_PROMPT = """Implement this GitHub issue in the repository you are in.

Work only on what the issue asks. Follow the repository's existing conventions,
CLAUDE.md, and its tests. Every acceptance criterion must be genuinely satisfied,
and behaviour you add or change must be covered by a test.

Do not: touch CI workflows, licences, deployment files, or secrets; delete or
disable tests to make a suite pass; make changes unrelated to this issue.

When you are done, run the test suite and make sure it passes.

=== ISSUE #{number}: {title} ===
{body}
"""

FIX_PROMPT = """You are fixing an open pull request in the repository you are in.

Address every point below, then re-run the test suite until it passes. Change only
what is needed — do not rewrite unrelated code, and do not weaken or delete tests.

=== ORIGINAL ISSUE #{number}: {title} ===
{body}

=== WHAT NEEDS FIXING ===
{problems}
"""


def _judged_tag(sha: str, passed: bool) -> str:
    """Stamped into a judge comment so a later tick can tell it already ruled on
    this exact commit, and what it decided."""
    return f"<!-- agent-loop:judged:{sha}:{'pass' if passed else 'fail'} -->"


def _verdict_for(comments: list[dict], sha: str) -> bool | None:
    """Our verdict for this commit, or None if we haven't judged it."""
    if not sha:
        return None
    for c in comments:
        body = c.get("body") or ""
        if f"agent-loop:judged:{sha}:pass" in body:
            return True
        if f"agent-loop:judged:{sha}:fail" in body:
            return False
    return None


def _attempts(issue_body_comments: list[dict], since: str = "") -> int:
    """How many times we've run an agent on this PR since the human last reset it.

    `since` is when a human removed `agent:needs-human`. Attempts before that
    don't count: taking the label off is how you say "try again", and without
    this the counter is already at the cap, so the loop re-escalates on the next
    tick and the label can never be cleared. PR #29 sat green and mergeable for a
    day that way.
    """
    return sum(1 for c in issue_body_comments
               if "<!-- agent-loop:attempt -->" in (c.get("body") or "")
               and (c.get("createdAt") or "") > since)


# ── issue watcher ────────────────────────────────────────────────────────────
def issue_watcher(cfg: Config, repo: Repo, workspace_root: str) -> list[str]:
    """One non-blocking pass: collect finished agents, then start new ones.

    Collection runs first so a freed slot is reused within the same tick.
    """
    out: list[str] = []
    ws = Workspace(workspace_root, repo.slug)
    logs = Path(workspace_root) / "logs"

    out += _collect_finished(cfg, repo, ws, logs)

    issues = gh.ready_issues(repo.slug, LABEL_READY, LABEL_WIP, LABEL_STOP)
    if not issues:
        out.append("no ready issues")
        return out

    running = tmux.live_windows() if not cfg.dry_run else []
    free_slots = cfg.max_concurrent_agents - len(running)
    if free_slots <= 0:
        out.append(f"at capacity ({len(running)}/{cfg.max_concurrent_agents} agents live)")
        return out

    ws.ensure_clone(dry=cfg.dry_run)
    for issue in issues[:free_slots]:
        n, title = issue["number"], issue["title"]
        try:
            path, _branch = ws.create(n, title, repo.default_branch, dry=cfg.dry_run)
            prompt = IMPLEMENT_PROMPT.format(
                number=n, title=title, body=(issue.get("body") or "")[:8000])
            gh.add_label(repo.slug, n, LABEL_WIP, dry=cfg.dry_run)
            if cfg.dry_run:
                out.append(f"#{n} would spawn agent in tmux window issue-{n}")
                continue
            tmux.spawn(n, IMPLEMENTER, prompt, cwd=path, log_dir=logs)
            out.append(f"#{n} agent started (watch: tmux window issue-{n}): {title}")
        except Exception as exc:                     # noqa: BLE001 — one issue must not sink the tick
            log.exception("issue #%s failed to start", n)
            gh.remove_label(repo.slug, n, LABEL_WIP, dry=cfg.dry_run)
            out.append(f"#{n} error: {exc}")
    return out


def _collect_finished(cfg: Config, repo: Repo, ws: Workspace, logs: Path) -> list[str]:
    """Turn completed agent runs into pull requests."""
    out: list[str] = []
    if cfg.dry_run or not tmux.available():
        return out

    for n in _pending_issues(logs):
        if tmux.is_running(n):
            continue
        rc = tmux.exit_code(n, logs)
        if rc is None:
            continue                       # window died without writing a code
        path = ws.trees / f"issue-{n}"
        text = tmux.output(n, logs)

        if rc != 0:
            # A usage limit is temporary: release the issue untouched so a later
            # tick retries it, rather than burning an attempt or calling for help.
            if looks_limited(text):
                _release(repo, ws, logs, n)
                out.append(f"#{n} agent hit a usage limit — will retry later")
            else:
                _release(repo, ws, logs, n,
                         reason=f"The agent exited with code {rc}. Last output:\n\n"
                                f"```\n{text[-1500:]}\n```")
                out.append(f"#{n} agent failed (rc={rc})")
            continue

        if not path.exists() or not ws.has_changes(path):
            _release(repo, ws, logs, n,
                     reason="The agent produced no changes — the issue may be "
                            "under-specified. Handing back to a human.")
            out.append(f"#{n} no changes produced")
            continue

        try:
            title = gh.run(["issue", "view", str(n), "--repo", repo.slug,
                            "--json", "title", "--jq", ".title"])
            branch = branch_name(n, title)
            ws.commit_all(path, f"{title}\n\nCloses #{n}\n\nImplemented by Codex via agent-loop.")
            ws.push(path, branch)
            gh.create_pr(repo.slug, head=branch, title=title,
                         body=(f"Closes #{n}\n\nImplemented autonomously by **Codex**. "
                               f"Awaiting CI and a review from **Claude**.\n\n"
                               f"<!-- agent-loop:attempt --> {MARKER}"),
                         base=repo.default_branch, cwd=str(path), label=LABEL_PR)
            gh.remove_label(repo.slug, n, LABEL_WIP)
            tmux.kill(n)
            (logs / f"issue-{n}.rc").unlink(missing_ok=True)
            out.append(f"#{n} PR opened on {branch}")
        except Exception as exc:                     # noqa: BLE001
            log.exception("collecting issue #%s failed", n)
            out.append(f"#{n} collect error: {exc}")
    return out


def _release(repo: Repo, ws: Workspace, logs: Path, n: int,
             reason: str | None = None) -> None:
    """Give an issue back. With a reason it's escalated to a human; without one
    (a rate limit) it simply becomes available again for a later tick."""
    gh.remove_label(repo.slug, n, LABEL_WIP)
    if reason:
        gh.add_label(repo.slug, n, LABEL_NEEDS_HUMAN)
        gh.comment(repo.slug, n, reason)
    tmux.kill(n)
    (logs / f"issue-{n}.rc").unlink(missing_ok=True)
    ws.remove(n)


def _pending_issues(logs: Path) -> list[int]:
    """Issues that have an agent run recorded but not yet collected."""
    if not logs.exists():
        return []
    nums = set()
    for f in logs.glob("issue-*.rc"):
        try:
            nums.add(int(f.stem.split("-", 1)[1]))
        except (ValueError, IndexError):
            continue
    return sorted(nums | set(tmux.live_windows()))


# ── pr watcher ───────────────────────────────────────────────────────────────
def pr_watcher(cfg: Config, repo: Repo, workspace_root: str) -> list[str]:
    """One pass over open agent PRs: judge, fix, merge or escalate."""
    out: list[str] = []
    ws = Workspace(workspace_root, repo.slug)
    budget = Budget(Path(workspace_root) / "judge-budget.json",
                    cfg.max_judge_calls, cfg.judge_window_hours)

    for pr in gh.open_prs(repo.slug, LABEL_PR):
        try:
            out += _handle_pr(cfg, repo, ws, budget, pr)
        except Exception as exc:                          # noqa: BLE001
            # One unhappy PR must not abort the pass: before this, a single
            # failing gh call crash-looped the timer every 5 minutes and left
            # every other PR unjudged.
            log.exception("PR #%s failed", pr.get("number"))
            out.append(f"PR #{pr.get('number')} error: {exc}")
    return out


def _handle_pr(cfg: Config, repo: Repo, ws: Workspace, budget, pr: dict) -> list[str]:
    """Judge, fix, merge or hold ONE pull request."""
    out: list[str] = []
    num = pr["number"]
    labels = {lbl["name"] for lbl in pr.get("labels", [])}
    if LABEL_STOP in labels or LABEL_NEEDS_HUMAN in labels:
        out.append(f"PR #{num} skipped (stopped or awaiting human)")
        return out

    checks = gh.pr_checks_state(repo.slug, num)
    if checks == "pending":
        out.append(f"PR #{num} CI pending")
        return out

    comments = gh.pr_review_comments(repo.slug, num)
    # "A human asked for changes" must mean "a comment we did not write". The loop
    # pushes under your own account, so authorship cannot tell us apart.
    human = [c for c in comments if MARKER not in (c.get("body") or "")]
    files = gh.pr_files(repo.slug, num)
    issue = {"number": num, "title": pr["title"], "body": pr.get("body") or ""}

    # CI red or a human asked for changes -> put an agent back on it
    problems: list[str] = []
    if checks == "fail":
        problems.append("CI is failing — read the failing job output and fix it.")
    if human:
        problems += [f"Reviewer comment: {c['body'][:500]}" for c in human[-3:]]

    verdict = None
    if not problems:
        # Judge a given commit ONCE. Without this a PR the gate holds — a guarded
        # path, a conflict — is re-judged every tick forever: same diff, same
        # verdict, a real Claude call each time. That leak, not review volume, is
        # what exhausts the budget.
        sha = pr.get("headRefOid") or ""
        prior = _verdict_for(comments, sha)
        if prior is not None:
            if prior:
                verdict = Verdict(passed=True, score=0,
                                  notes="already judged at this commit")
            else:
                out.append(f"PR #{num} already judged at {sha[:7]} — waiting for a change")
                return out
        else:
            if not budget.allow():
                out.append(f"PR #{num} judge budget spent ({budget.status()})")
                return out
            verdict = judge_pr(issue, gh.pr_diff(repo.slug, num), dry=cfg.dry_run)
            if verdict.limited:
                out.append(f"PR #{num} judge rate-limited — will retry")
                return out
            if not cfg.dry_run:
                budget.record()
            gh.pr_review(repo.slug, num,
                         f"{verdict.as_comment()}\n{MARKER}{_judged_tag(sha, verdict.passed)}",
                         approve=verdict.passed, dry=cfg.dry_run)
            if not verdict.passed:
                problems = verdict.blocking or ["The reviewer did not pass this PR."]

    if problems:
        reset_at = gh.last_unlabel(repo.slug, num, LABEL_NEEDS_HUMAN)
        if _attempts(comments, reset_at) >= cfg.max_attempts_per_issue:
            gh.add_label(repo.slug, num, LABEL_NEEDS_HUMAN, dry=cfg.dry_run)
            gh.comment(repo.slug, num,
                       f"Stopping after {cfg.max_attempts_per_issue} attempts — "
                       "this needs a human.", dry=cfg.dry_run)
            out.append(f"PR #{num} hit the attempt cap")
            return out
        out.append(f"PR #{num} fixing: {problems[0][:60]}")
        _run_fixer(cfg, repo, ws, num, pr, issue, problems, out)
        return out

    gate = decide(repo_auto_merge=repo.auto_merge, checks=checks, verdict=verdict,
                  changed_files=files, mergeable=pr.get("mergeable") or "",
                  cfg=cfg, human_requested_changes=bool(human))
    if gate:
        gh.merge_pr(repo.slug, num, dry=cfg.dry_run)
        # The work shipped: drop its log so the console does not carry a card for
        # every issue ever run.
        tmux.discard_log(num, Path.home() / "agent-loop-work" / "logs")
        ws.remove(num, dry=cfg.dry_run)
        out.append(f"PR #{num} MERGED — {gate.reason}")
    else:
        out.append(f"PR #{num} held: {gate.reason}")
    return out


def _run_fixer(cfg, repo, ws, num, pr, issue, problems, out) -> None:
    try:
        path = ws.trees / f"issue-{num}"
        if not cfg.dry_run and not path.exists():
            ws.ensure_clone()
            path, _ = ws.create(num, pr["title"], repo.default_branch)
            from agentloop.worktree import git
            git(["checkout", pr["headRefName"]], path)
        prompt = FIX_PROMPT.format(number=num, title=issue["title"],
                                   body=issue["body"][:4000],
                                   problems="\n".join(f"- {p}" for p in problems))
        res = invoke(IMPLEMENTER, prompt, cwd=str(path),
                     timeout=cfg.agent_timeout_s, dry=cfg.dry_run)
        if not res.ok:
            out.append(f"PR #{num} fixer {'limited' if res.limited else 'failed'}")
            return
        if not cfg.dry_run and ws.has_changes(path):
            ws.commit_all(path, f"Address review on #{num}")
            ws.push(path, pr["headRefName"])
        gh.comment(repo.slug, num,
                   "Addressed the points above. <!-- agent-loop:attempt --> " + MARKER,
                   dry=cfg.dry_run)
    except Exception as exc:                          # noqa: BLE001
        log.exception("fixer for PR #%s failed", num)
        out.append(f"PR #{num} fixer error: {exc}")
