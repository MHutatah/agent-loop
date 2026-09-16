"""The two loops.

issue_watcher: ready-labelled issue -> worktree -> Codex implements -> tests -> PR
pr_watcher:    open agent PR -> judge / fix CI / answer your comments -> merge or escalate

Both are single-pass functions, invoked by a systemd timer rather than looping
forever in-process: a crash then costs one tick instead of the whole service, and
`systemctl` is the on/off switch.
"""
from __future__ import annotations

import contextlib
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
    touches_guarded_path,
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

    Collection runs first so a freed slot is reused within the same tick, then
    the reaper, so an issue stranded by a reboot is available again before this
    pass decides what to start.

    Capacity is enforced here rather than by the caller: global and per-repo
    ceilings both apply, which is what stops one project taking the pool.
    """
    out: list[str] = []
    ws = Workspace(workspace_root, repo.slug)
    logs = ws.logs
    logs.mkdir(parents=True, exist_ok=True)

    out += _collect_finished(cfg, repo, ws)
    out += _reap(cfg, repo, ws)

    issues = gh.ready_issues(repo.slug, LABEL_READY, LABEL_WIP, LABEL_STOP,
                             LABEL_NEEDS_HUMAN)

    # AN ISSUE THAT ALREADY HAS AN OPEN PR IS NOT WAITING TO BE STARTED, and the
    # labels cannot be trusted to say so within one tick. GitHub's issue-list
    # index is eventually consistent: the collector above removes agent:ready
    # and agent:working, and a `gh issue list --label` a second later still
    # returns the issue as ready and not working. That happened on the tick that
    # opened PRs for #2 and #89 and immediately started a second agent on #89.
    #
    # The PR is the durable fact, so it is the one to filter on. This also
    # covers a stale ready label left by hand and a restart mid-collection.
    claimed = gh.issues_with_open_pr(repo.slug, LABEL_PR)
    if claimed:
        blocked = [i for i in issues if i["number"] in claimed]
        issues = [i for i in issues if i["number"] not in claimed]
        for i in blocked:
            out.append(f"#{i['number']} already has an open PR, not restarting")

    if not issues:
        out.append("no ready issues")
        return out

    # Capacity is global AND per repo. Global alone let whichever repository the
    # config happened to list first take every slot on a busy morning, so the
    # others were not slow, they were silent.
    running = tmux.live_windows() if not cfg.dry_run else []
    mine = [n for k, n in running if k == ws.key]
    free_slots = min(cfg.max_concurrent_agents - len(running),
                     cfg.max_concurrent_per_repo - len(mine))
    if free_slots <= 0:
        out.append(f"at capacity ({len(running)}/{cfg.max_concurrent_agents} global, "
                   f"{len(mine)}/{cfg.max_concurrent_per_repo} for this repo)")
        return out

    ws.ensure_clone(dry=cfg.dry_run)
    for issue in issues[:free_slots]:
        n, title = issue["number"], issue["title"]
        try:
            path, _branch = ws.create(n, title, repo.default_branch, dry=cfg.dry_run)
            prompt = IMPLEMENT_PROMPT.format(
                number=n, title=title, body=(issue.get("body") or "")[:8000])
            gh.add_label(repo.slug, n, LABEL_WIP, dry=cfg.dry_run)
            window = tmux.window_name(ws.key, n)
            if cfg.dry_run:
                out.append(f"#{n} would spawn agent in tmux window {window}")
                continue
            tmux.spawn(ws.key, n, IMPLEMENTER, prompt, cwd=path, log_dir=logs)
            out.append(f"#{n} agent started (watch: tmux window {window}): {title}")
        except Exception as exc:                     # noqa: BLE001 — one issue must not sink the tick
            log.exception("issue #%s failed to start", n)
            gh.remove_label(repo.slug, n, LABEL_WIP, dry=cfg.dry_run)
            out.append(f"#{n} error: {exc}")
    return out


def _reap(cfg: Config, repo: Repo, ws: Workspace) -> list[str]:
    """Release issues GitHub thinks are in progress that nothing is working on.

    THE LOST-ISSUE BUG LIVED HERE, in the absence of this function. `spawn`
    deletes the `.rc` status file before starting the agent and writes it on
    completion, and the collector only ever looked at issues that had a `.rc`
    file or a live tmux window. Kill tmux between those two moments, whether by a
    reboot, `kill-server` or the OOM killer, and an issue has neither. It was therefore
    never collected and never released, while `agent:working` stayed on it
    forever and `ready_issues` excludes that label, so it could never be picked
    up again either. The work simply stopped existing, silently, which is the
    worst failure this system can have: it is indistinguishable from an idle
    loop.

    GitHub's labels are the authority here, not the filesystem, because they are
    the only part of the state that survives the box.
    """
    out: list[str] = []
    if cfg.dry_run or not tmux.available():
        return out
    for issue in gh.issues_with_label(repo.slug, LABEL_WIP, limit=50):
        n = issue.get("number")
        if not n or tmux.is_running(ws.key, n):
            continue
        if (ws.logs / f"issue-{n}.rc").exists():
            continue                       # finished; the collector owns it
        path = ws.trees / f"issue-{n}"
        if path.exists() and ws.ahead_of(path, repo.default_branch) > 0:
            continue                       # has real work; the collector owns it
        _release(repo, ws, n)
        out.append(f"#{n} was stranded with {LABEL_WIP} and nothing running "
                   f"and was released for a later tick")
    return out


def _collect_finished(cfg: Config, repo: Repo, ws: Workspace) -> list[str]:
    """Turn completed agent runs into pull requests."""
    out: list[str] = []
    if cfg.dry_run or not tmux.available():
        return out
    logs = ws.logs

    for n in _pending_issues(logs, ws.key):
        if tmux.is_running(ws.key, n):
            continue
        rc = tmux.exit_code(n, logs)
        path = ws.trees / f"issue-{n}"
        committed = ws.ahead_of(path, repo.default_branch) if path.exists() else 0

        # Resume a half-collected run BEFORE reading the exit code. Commits on
        # the branch are proof the agent succeeded, whatever happened to the
        # status file afterwards, and the previous order of these checks is what
        # let a failed `pr create` be reported as an under-specified issue.
        if rc is None and committed == 0:
            continue                       # still starting, or the reaper's job

        text = tmux.output(n, logs)

        if rc not in (None, 0) and committed == 0:
            # A usage limit is temporary: release the issue untouched so a later
            # tick retries it, rather than burning an attempt or calling for help.
            if looks_limited(text):
                _release(repo, ws, n)
                out.append(f"#{n} agent hit a usage limit — will retry later")
            else:
                _release(repo, ws, n,
                         reason=f"The agent exited with code {rc}. Last output:\n\n"
                                f"```\n{text[-1500:]}\n```")
                out.append(f"#{n} agent failed (rc={rc})")
            continue

        if not path.exists():
            _release(repo, ws, n,
                     reason="The worktree is gone, so there is nothing to collect. "
                            "Re-label when you want this retried.")
            out.append(f"#{n} worktree missing")
            continue

        if committed == 0 and not ws.has_changes(path):
            # Only NOW is this diagnosis honest: nothing committed and nothing
            # pending really is an agent that did nothing.
            _release(repo, ws, n,
                     reason="The agent produced no changes — the issue may be "
                            "under-specified. Handing back to a human.")
            out.append(f"#{n} no changes produced")
            continue

        # CONTAINMENT BEFORE THE PUSH, not at merge time. gate.py checks guarded
        # paths when deciding whether to merge, which is too late to matter: the
        # branch is on GitHub by then. If the agent wrote a credential while
        # testing, refusing the merge does not unpublish it.
        guarded = touches_guarded_path(
            ws.changed_paths(path, repo.default_branch), cfg)
        if guarded:
            _release(repo, ws, n,
                     reason=("Refusing to push: this change touches guarded "
                             f"path(s) `{'`, `'.join(guarded[:5])}`. Nothing was "
                             "pushed, so review the worktree on the box rather "
                             "than a branch."),
                     keep_tree=True)
            out.append(f"#{n} blocked before push: {', '.join(guarded[:3])}")
            continue

        try:
            title = gh.run(["issue", "view", str(n), "--repo", repo.slug,
                            "--json", "title", "--jq", ".title"])
            branch = branch_name(n, title)
            if ws.has_changes(path):
                ws.commit_all(path, f"{title}\n\nCloses #{n}\n\n"
                                    f"Implemented by Codex via agent-loop.")
            ws.push(path, branch)          # force-with-lease: safe to repeat
            existing = gh.pr_for_branch(repo.slug, branch)
            if existing:
                # A previous tick pushed and then failed to finish. Adopt its PR
                # instead of failing forever on "a pull request already exists".
                if LABEL_PR not in {lbl["name"] for lbl in existing.get("labels", [])}:
                    gh.add_label(repo.slug, existing["number"], LABEL_PR)
                out.append(f"#{n} PR #{existing['number']} already open — adopted")
            else:
                gh.create_pr(repo.slug, head=branch, title=title,
                             body=(f"Closes #{n}\n\nImplemented autonomously by "
                                   f"**Codex**. Awaiting CI and a review from "
                                   f"**Claude**.\n\n"
                                   f"<!-- agent-loop:attempt --> {MARKER}"),
                             base=repo.default_branch, cwd=str(path), label=LABEL_PR)
                out.append(f"#{n} PR opened on {branch}")
            # BOTH labels, and the ready one is the important half. Only WIP
            # was removed here, so the same tick that opened the PR re-selected
            # the issue and started another agent on it, and kept doing that
            # until the issue closed. auto_merge=true hid it: the merge closed
            # the issue before the next tick. With auto_merge off, one issue
            # re-implemented itself indefinitely.
            gh.remove_label(repo.slug, n, LABEL_WIP)
            gh.remove_label(repo.slug, n, LABEL_READY)
            tmux.kill(ws.key, n)
            (logs / f"issue-{n}.rc").unlink(missing_ok=True)
        except Exception as exc:                     # noqa: BLE001
            # Deliberately leaves the `.rc` in place so the next tick retries.
            # That retry is only safe because every step above is idempotent.
            log.exception("collecting issue #%s failed", n)
            out.append(f"#{n} collect error: {exc}")
    return out


def _release(repo: Repo, ws: Workspace, n: int, reason: str | None = None,
             *, keep_tree: bool = False) -> None:
    """Give an issue back. With a reason it's escalated to a human; without one
    (a rate limit, or the reaper) it simply becomes available again.

    `keep_tree` preserves the worktree for the one case where it is the only
    copy of the work: a change blocked before it was ever pushed.
    """
    gh.remove_label(repo.slug, n, LABEL_WIP)
    if reason:
        # Escalating takes it out of the queue too. Leaving ready on meant the
        # next tick picked it straight back up, so the label said "a human
        # should look at this" while a machine kept trying.
        gh.remove_label(repo.slug, n, LABEL_READY)
        gh.add_label(repo.slug, n, LABEL_NEEDS_HUMAN)
        gh.comment(repo.slug, n, reason)
    tmux.kill(ws.key, n)
    (ws.logs / f"issue-{n}.rc").unlink(missing_ok=True)
    if not keep_tree:
        ws.remove(n)


def _pending_issues(logs: Path, key: str = "") -> list[int]:
    """Issues that have an agent run recorded but not yet collected.

    `key` scopes the live-window half to ONE repository. Without it this unioned
    issue numbers with the (repo, issue) pairs live_windows now returns, and
    sorted() raised on comparing an int to a tuple.
    """
    if not logs.exists():
        return []
    nums = set()
    for f in logs.glob("issue-*.rc"):
        try:
            nums.add(int(f.stem.split("-", 1)[1]))
        except (ValueError, IndexError):
            continue
    return sorted(nums | set(tmux.live_for(key) if key else []))


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
    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        # A CONFLICTING PR WAS A DEAD END. gate.decide refuses it with "merge
        # conflict" and nothing ever rebased it, so the PR sat open forever with
        # a correct reason and no route out. That is the normal outcome as soon
        # as two agents work one area in parallel: the first merge conflicts the
        # second, which is exactly what happened to ipa-community #91 the moment
        # #90 went in. Hand it to the fixer instead of holding it.
        problems.append(
            f"This branch conflicts with origin/{repo.default_branch}. Fetch and "
            "rebase onto it, resolve every conflict by KEEPING BOTH SIDES' "
            "intent rather than discarding either, and do not weaken or delete a "
            "test to make the merge simpler. Then re-run the suite.")
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
            tree = ws.trees / f"issue-{num}"
            verdict = judge_pr(issue, gh.pr_diff(repo.slug, num),
                               cwd=str(tree) if tree.exists() else None,
                               dry=cfg.dry_run)
            if verdict.limited:
                out.append(f"PR #{num} judge rate-limited — will retry")
                return out
            if not verdict.usable:
                # A JUDGE THAT CANNOT RULE IS NOT A REJECTION. This used to fall
                # through to `problems`, so a broken judge call became "the
                # reviewer did not pass this PR", put an agent on it three
                # times, and escalated it behind a review reading
                # "CHANGES REQUESTED - score 0/10" with an empty body. Three
                # green pull requests were failed that way by one bad flag
                # order. Hold instead, post nothing, and say so.
                out.append(f"PR #{num} judge could not rule ({verdict.error}) "
                           "- holding, not failing")
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
        tmux.discard_log(num, ws.logs)
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
            # attach(), never create(): create() resets the branch to the base.
            path = ws.attach(num, pr["headRefName"])
        # Make origin/<base> present in the worktree so the agent can rebase.
        if not cfg.dry_run:
            from agentloop.worktree import git
            git(["fetch", "origin", repo.default_branch], path, check=False)
        prompt = FIX_PROMPT.format(number=num, title=issue["title"],
                                   body=issue["body"][:4000],
                                   problems="\n".join(f"- {p}" for p in problems))
        res = invoke(IMPLEMENTER, prompt, cwd=str(path),
                     timeout=cfg.agent_timeout_s, dry=cfg.dry_run)
        # LEAVE A LOG. The fixer runs synchronously through invoke() rather than
        # tmux.spawn, so unlike an implementer run it wrote nothing anywhere.
        # #91 failed three times and escalated with no diagnostics at all, and
        # the actual cause, a missing git identity, had to be found by hand.
        if not cfg.dry_run:
            with contextlib.suppress(OSError):
                ws.logs.mkdir(parents=True, exist_ok=True)
                (ws.logs / f"issue-{num}.fix.log").write_text(
                    "\n".join([f"ok={res.ok} limited={res.limited}",
                               f"error={res.error}", "", res.text or ""]),
                    encoding="utf-8")
        if not res.ok:
            out.append(f"PR #{num} fixer {'limited' if res.limited else 'failed'}")
            return
        if not cfg.dry_run and ws.has_changes(path):
            ws.commit_all(path, f"Address review on #{num}")
            ws.push(path, pr["headRefName"], base=repo.default_branch)
        gh.comment(repo.slug, num,
                   "Addressed the points above. <!-- agent-loop:attempt --> " + MARKER,
                   dry=cfg.dry_run)
    except Exception as exc:                          # noqa: BLE001
        log.exception("fixer for PR #%s failed", num)
        out.append(f"PR #{num} fixer error: {exc}")
