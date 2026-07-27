"""Thin `gh` CLI wrapper.

`gh` already does auth, pagination and the GitHub API surface we need, so this
stays a shell-out rather than a REST client. Every mutating call honours dry-run
so the whole loop can be exercised without touching a real repository.
"""
from __future__ import annotations

import contextlib
import json
import logging
import subprocess

from agentloop.config import MARKER

log = logging.getLogger("agentloop.gh")


class GhError(RuntimeError):
    pass


def run(args: list[str], *, cwd: str | None = None, dry: bool = False,
        mutating: bool = False, timeout: int = 120) -> str:
    if dry and mutating:
        log.info("[dry-run] gh %s", " ".join(args))
        return ""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                          encoding="utf-8", cwd=cwd, timeout=timeout)
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])}…: {(proc.stderr or '').strip()[:300]}")
    return (proc.stdout or "").strip()


def _json(args: list[str], cwd: str | None = None):
    out = run(args, cwd=cwd)
    return json.loads(out) if out else []


# ── issues ───────────────────────────────────────────────────────────────────
def ready_issues(repo: str, ready_label: str, wip_label: str,
                 stop_label: str) -> list[dict]:
    """Open issues labelled ready, not already being worked, not stopped."""
    issues = _json([
        "issue", "list", "--repo", repo, "--state", "open",
        "--label", ready_label, "--limit", "50",
        "--json", "number,title,body,labels,url",
    ])
    out = []
    for i in issues:
        names = {lbl["name"] for lbl in i.get("labels", [])}
        if wip_label in names or stop_label in names:
            continue
        out.append(i)
    return out


def add_label(repo: str, number: int, label: str, *, dry=False) -> None:
    run(["issue", "edit", str(number), "--repo", repo, "--add-label", label],
        dry=dry, mutating=True)


def remove_label(repo: str, number: int, label: str, *, dry=False) -> None:
    # label may not be present; that isn't worth failing a run over
    with contextlib.suppress(GhError):
        run(["issue", "edit", str(number), "--repo", repo, "--remove-label", label],
            dry=dry, mutating=True)


def comment(repo: str, number: int, body: str, *, dry=False) -> None:
    """Post a comment, always marked as ours.

    The marker is stamped here rather than at each call site because forgetting
    it is silently destructive: "a human asked for changes" is defined as "a
    comment we did not write", so an unmarked comment of our own reads as human
    intervention and blocks the PR from ever merging. Two call sites had already
    forgotten it. One guard here means none can.
    """
    if MARKER not in body:
        body = f"{body}\n\n{MARKER}"
    run(["issue", "comment", str(number), "--repo", repo, "--body", body],
        dry=dry, mutating=True)


# ── pull requests ────────────────────────────────────────────────────────────
def open_prs(repo: str, label: str) -> list[dict]:
    return _json([
        "pr", "list", "--repo", repo, "--state", "open", "--label", label,
        "--limit", "50",
        "--json", "number,title,headRefName,headRefOid,mergeable,mergeStateStatus,url,labels,body",
    ])


def pr_diff(repo: str, number: int, max_chars: int = 120_000) -> str:
    """The diff, as text — the judge reads this instead of cloning anything."""
    return run(["pr", "diff", str(number), "--repo", repo])[:max_chars]


def pr_files(repo: str, number: int) -> list[str]:
    data = _json(["pr", "view", str(number), "--repo", repo, "--json", "files"])
    return [f["path"] for f in (data or {}).get("files", [])]


def pr_checks_state(repo: str, number: int) -> str:
    """-> 'pass' | 'fail' | 'pending' | 'none' | 'unknown'.

    Reads `statusCheckRollup` rather than `gh pr checks --json`, which this gh
    version does not support — that call raised, the error was swallowed as "no
    checks configured", and PRs with green CI were refused for a reason that was
    simply untrue.

    'unknown' exists to keep those cases apart: an inability to see CI is a tool
    problem, while 'none' is a fact about the repository. Both refuse to merge,
    but only one of them is worth waking someone up about.
    """
    try:
        rows = _json(["pr", "view", str(number), "--repo", repo,
                      "--json", "statusCheckRollup"])
    except GhError:
        return "unknown"
    rows = (rows or {}).get("statusCheckRollup") or []
    if not rows:
        return "none"
    # Checks report `conclusion` once finished and `status` while running;
    # legacy commit statuses use `state`.
    states = set()
    for r in rows:
        if (r.get("status") or "").upper() in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING"}:
            states.add("PENDING")
        else:
            states.add((r.get("conclusion") or r.get("state") or "").upper())
    if states & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
        return "fail"
    if states & {"PENDING", ""}:
        return "pending"
    return "pass"


def pr_review_comments(repo: str, number: int) -> list[dict]:
    """Human comments on the PR — the signal that you want changes."""
    data = _json(["pr", "view", str(number), "--repo", repo, "--json", "comments"])
    return (data or {}).get("comments", [])


def last_unlabel(repo: str, number: int, label: str) -> str:
    """When a human last removed `label` — the "try again" signal.

    Returns an ISO timestamp, or "" if it was never removed (so a plain
    string comparison against a comment's createdAt counts everything).
    """
    try:
        events = json.loads(run([
            "api", f"repos/{repo}/issues/{number}/timeline", "--paginate",
        ]) or "[]")
    except (GhError, json.JSONDecodeError):
        return ""
    return max((e.get("created_at") or "" for e in events
                if e.get("event") == "unlabeled"
                and (e.get("label") or {}).get("name") == label), default="")


def pr_review(repo: str, number: int, body: str, *, approve: bool, dry=False) -> None:
    """Post the judge's verdict on a PR.

    Always a comment, never `--approve`: the loop pushes under the same account
    that owns the token, and GitHub rejects approving your own pull request
    ("Can not approve your own pull request"). Nothing is lost — the merge gate
    reads the judge's verdict directly, so GitHub's review state was only ever
    decoration, and `approve` is kept in the signature because the verdict is
    what the comment says.
    """
    run(["pr", "review", str(number), "--repo", repo, "--comment", "--body", body],
        dry=dry, mutating=True)


def merge_pr(repo: str, number: int, *, dry=False) -> None:
    run(["pr", "merge", str(number), "--repo", repo, "--squash", "--delete-branch"],
        dry=dry, mutating=True)


def create_pr(repo: str, *, head: str, title: str, body: str, base: str,
              cwd: str, dry=False) -> str:
    return run(["pr", "create", "--repo", repo, "--head", head, "--base", base,
                "--title", title, "--body", body],
               cwd=cwd, dry=dry, mutating=True)


def issues_with_label(repo: str, label: str, limit: int = 20) -> list[dict]:
    """Issues carrying a label, PARSED. The console previously measured the raw
    JSON text's length to decide whether any existed, which silently ignored the
    threshold it appeared to apply."""
    return _json(["issue", "list", "--repo", repo, "--state", "open",
                  "--label", label, "--limit", str(limit),
                  "--json", "number,title,url"]) or []


def pr_judged_verdict(comments: list[dict], sha: str) -> bool | None:
    """Our recorded verdict for this commit, read back from the PR thread."""
    if not sha:
        return None
    for c in comments:
        body = c.get("body") or ""
        if f"agent-loop:judged:{sha}:pass" in body:
            return True
        if f"agent-loop:judged:{sha}:fail" in body:
            return False
    return None
