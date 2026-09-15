"""Isolated git worktrees — one per issue, so concurrent agents never collide.

A worktree gives each agent its own checkout and branch off a single shared clone:
cheap to create, trivial to throw away, and impossible for one agent's half-finished
edits to leak into another's.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("agentloop.worktree")


def git(args: list[str], cwd: str | Path, *, check: bool = True,
        timeout: int = 300) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          cwd=str(cwd), timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {(proc.stderr or '').strip()[:300]}")
    return (proc.stdout or "").strip()


def branch_name(issue_number: int, title: str) -> str:
    """A branch a human can read at a glance in the GitHub mobile UI."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40].strip("-")
    return f"agent/{issue_number}-{slug}" if slug else f"agent/{issue_number}"


def repo_key(repo_slug: str) -> str:
    """One filesystem- and tmux-safe name per repository.

    EVERYTHING per-repo is namespaced with this, and that is not tidiness. The
    first version of this file put the clone at `<root>/repo` and the worktrees
    at `<root>/trees` for every repository, while taking `repo_slug` and using
    it only to build the clone URL. With two repos configured, the second one's
    `ensure_clone` found a `.git` already there, fetched the FIRST repo's
    origin, cut worktrees of the first repo's code for the second repo's issue
    numbers, and opened pull requests on the second repo containing the first
    one's work. agentloop.toml says "add more repos by repeating the block", so
    the configuration file invited exactly that.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "__", repo_slug).strip("_") or "repo"


class Workspace:
    """A shared clone plus per-issue worktrees, both scoped to ONE repository."""

    def __init__(self, root: str | Path, repo_slug: str):
        self.repo_slug = repo_slug
        self.key = repo_key(repo_slug)
        # `root` stays the shared workspace; everything below it is per-repo.
        self.root = Path(root) / "repos" / self.key
        self.clone = self.root / "repo"
        self.trees = self.root / "trees"
        self.logs = self.root / "logs"

    def ensure_clone(self, *, dry: bool = False) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.trees.mkdir(exist_ok=True)
        self.logs.mkdir(exist_ok=True)
        if dry:
            return
        if not (self.clone / ".git").exists():
            log.info("cloning %s", self.repo_slug)
            # Clone over HTTPS explicitly rather than via `gh repo clone`: gh
            # honours its own git_protocol setting, which on this box resolved to
            # SSH and failed with no key registered. The HTTPS URL uses the token
            # through gh's credential helper, which is what an unattended loop
            # should depend on.
            subprocess.run(
                ["git", "clone", f"https://github.com/{self.repo_slug}.git",
                 str(self.clone)],
                check=True, capture_output=True, text=True, timeout=600)
        else:
            # Refuse to reuse a clone of a DIFFERENT repository. The per-repo
            # paths above already make this unreachable, and it is asserted
            # anyway because the bug it prevents is silent and expensive: the
            # loop would push one project's code to another project's branch and
            # nothing in the pipeline afterwards would notice.
            origin = git(["remote", "get-url", "origin"], self.clone, check=False)
            if origin and self.repo_slug.lower() not in origin.lower():
                raise RuntimeError(
                    f"{self.clone} is a clone of {origin!r}, not {self.repo_slug}. "
                    "Refusing to reuse it. Delete that directory and let the loop "
                    "re-clone.")
            git(["fetch", "--prune", "origin"], self.clone)
        # An identity ON THE CLONE, inherited by every worktree.
        #
        # commit_all passes -c user.name and -c user.email inline, so committing
        # always worked and nothing looked wrong. `git rebase` takes no such
        # flags: it read the box's config, found none, and died with
        # "unable to auto-detect email address". The fixer therefore failed
        # every single rebase attempt, burned all three tries on ipa-community
        # #91 and escalated it as if the conflict were hard.
        git(["config", "user.name", "agent-loop"], self.clone, check=False)
        git(["config", "user.email", "agent-loop@users.noreply.github.com"],
            self.clone, check=False)

    def create(self, issue_number: int, title: str, base: str = "main",
               *, dry: bool = False) -> tuple[Path, str]:
        branch = branch_name(issue_number, title)
        path = self.trees / f"issue-{issue_number}"
        if dry:
            return path, branch
        if path.exists():
            self.remove(issue_number)
        git(["fetch", "origin", base], self.clone)
        git(["worktree", "add", "-B", branch, str(path), f"origin/{base}"], self.clone)
        return path, branch

    def attach(self, issue_number: int, branch: str) -> Path:
        """A worktree on an EXISTING pull request branch, taken from origin.

        Never `create()`, which passes `-B <branch> origin/<base>` and so RESETS
        the branch to the base, discarding every commit on it. The fixer used
        create() whenever the local worktree was missing, then force-pushed the
        result, which silently destroyed finished work and closed the pull
        request: ipa-community #91 went from a reviewed, criteria-complete
        branch to a pointer at main in one tick, and the reflog read

            branch: Reset to origin/main
            commit: [STORY] Make short Arabic terms findable in the glossary
            branch: Created from origin/main

        A missing worktree is ordinary. It happens after a reboot, a prune, or
        any manual cleanup, so this path has to be the safe one.
        """
        path = self.trees / f"issue-{issue_number}"
        if path.exists():
            on = git(["rev-parse", "--abbrev-ref", "HEAD"], path, check=False)
            if on == branch:
                return path
            # A LEFTOVER WORKTREE ON THE WRONG BRANCH IS WORSE THAN NONE. The
            # destructive create() path left #91's worktree checked out on
            # agent/91-… while the pull request's head was agent/89-…, so the
            # fixer committed onto a branch nobody was going to push.
            log.warning("worktree for #%s is on %r, not %r; rebuilding",
                        issue_number, on, branch)
            self.remove(issue_number)
        git(["fetch", "origin", branch], self.clone)
        git(["worktree", "add", "--force", "--detach", str(path),
             f"origin/{branch}"], self.clone)
        git(["switch", "-C", branch, "--track", f"origin/{branch}"], path)
        return path

    def remove(self, issue_number: int, *, dry: bool = False) -> None:
        path = self.trees / f"issue-{issue_number}"
        if dry or not path.exists():
            return
        git(["worktree", "remove", "--force", str(path)], self.clone, check=False)
        git(["worktree", "prune"], self.clone, check=False)

    def has_changes(self, path: Path) -> bool:
        """Uncommitted work in the tree. NOT the same question as "did the agent
        produce anything": see `ahead_of`, and the comment on it."""
        return bool(git(["status", "--porcelain"], path))

    def ahead_of(self, path: Path, base: str) -> int:
        """Commits on this worktree that `origin/<base>` does not have.

        This exists because `has_changes` was being used to answer "did the
        agent do anything", and it stops being true the moment the work is
        committed. The collector committed, pushed, then failed to open the PR
        on a network blip; the next tick saw a clean tree, concluded "the agent
        produced no changes — the issue may be under-specified", labelled it
        `agent:needs-human` and deleted the worktree, while the branch sat
        pushed on GitHub. The issue author got told their own spec was vague
        about work that was finished.
        """
        out = git(["rev-list", "--count", f"origin/{base}..HEAD"], path, check=False)
        return int(out) if out.isdigit() else 0

    def changed_paths(self, path: Path, base: str) -> list[str]:
        """Every path this branch touches relative to the base, committed or not.

        Used to enforce guarded paths BEFORE the push rather than at merge time.
        `git add -A` plus a push means a secret the agent wrote while testing
        reaches GitHub first and is only then politely refused a merge, and
        pushing a credential and declining to merge it is not containment.
        """
        committed = git(["diff", "--name-only", f"origin/{base}...HEAD"],
                        path, check=False)
        pending = git(["status", "--porcelain"], path, check=False)
        names = [ln.strip() for ln in committed.splitlines() if ln.strip()]
        for line in pending.splitlines():
            # porcelain v1: XY<space>path, with "orig -> new" for renames
            name = line[3:].strip() if len(line) > 3 else ""
            if " -> " in name:
                name = name.split(" -> ", 1)[1]
            if name:
                names.append(name.strip('"'))
        return sorted(set(names))

    def commit_all(self, path: Path, message: str) -> None:
        git(["add", "-A"], path)
        git(["-c", "user.name=agent-loop",
             "-c", "user.email=agent-loop@users.noreply.github.com",
             "commit", "-m", message], path)

    def push(self, path: Path, branch: str, *, base: str = "") -> None:
        """Push, refusing to push away work.

        `base` turns on the guard: a branch with nothing ahead of the base has
        no work on it, and force-pushing that over a pull request is how #91 was
        destroyed. --force-with-lease does not help, because the lease was
        honest: we really had fetched, and then reset our own branch.
        """
        if base and self.ahead_of(path, base) == 0:
            raise RuntimeError(
                f"refusing to push {branch}: it is not ahead of origin/{base}, "
                "so there is nothing to push and force-pushing would discard "
                "whatever the pull request already had")
        git(["push", "-u", "origin", branch, "--force-with-lease"], path)

    def active(self) -> list[int]:
        """Issue numbers with a live worktree — used to cap concurrency."""
        if not self.trees.exists():
            return []
        out = []
        for p in self.trees.iterdir():
            m = re.fullmatch(r"issue-(\d+)", p.name)
            if m and p.is_dir():
                out.append(int(m.group(1)))
        return out
