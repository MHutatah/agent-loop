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


class Workspace:
    """A bare-ish shared clone plus per-issue worktrees hanging off it."""

    def __init__(self, root: str | Path, repo_slug: str):
        self.root = Path(root)
        self.repo_slug = repo_slug
        self.clone = self.root / "repo"
        self.trees = self.root / "trees"

    def ensure_clone(self, *, dry: bool = False) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.trees.mkdir(exist_ok=True)
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
            git(["fetch", "--prune", "origin"], self.clone)

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

    def remove(self, issue_number: int, *, dry: bool = False) -> None:
        path = self.trees / f"issue-{issue_number}"
        if dry or not path.exists():
            return
        git(["worktree", "remove", "--force", str(path)], self.clone, check=False)
        git(["worktree", "prune"], self.clone, check=False)

    def has_changes(self, path: Path) -> bool:
        return bool(git(["status", "--porcelain"], path))

    def commit_all(self, path: Path, message: str) -> None:
        git(["add", "-A"], path)
        git(["-c", "user.name=agent-loop",
             "-c", "user.email=agent-loop@users.noreply.github.com",
             "commit", "-m", message], path)

    def push(self, path: Path, branch: str) -> None:
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
