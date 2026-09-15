"""Entry point. One tick per invocation; systemd timers do the scheduling.

    python -m agentloop issues     # start agents on ready issues
    python -m agentloop prs        # judge / fix / merge open agent PRs
    python -m agentloop status     # what's in flight
    python -m agentloop doctor     # check the host is actually set up
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agentloop import gh
from agentloop.budget import Budget
from agentloop.config import CONTROL_LABELS, LABEL_PR, LABEL_READY, Config
from agentloop.watchers import issue_watcher, pr_watcher
from agentloop.worktree import Workspace, git

WORKSPACE = os.environ.get("AGENTLOOP_WORKSPACE", os.path.expanduser("~/agent-loop-work"))


def _log() -> None:
    logging.basicConfig(
        level=os.environ.get("AGENTLOOP_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )


def doctor(cfg: Config) -> int:
    """Verify the host can actually run the loop before it's trusted overnight."""
    ok = True
    for tool, why in [("gh", "GitHub access"), ("git", "worktrees"),
                      ("codex", "the implementer"), ("claude", "the judge")]:
        path = shutil.which(tool)
        print(f"  {'OK  ' if path else 'MISS'} {tool:<6} — {why}")
        ok &= bool(path)

    try:
        who = gh.run(["api", "user", "--jq", ".login"])
        print(f"  OK   gh auth — {who}")
    except Exception as exc:                          # noqa: BLE001
        print(f"  MISS gh auth — {exc}")
        ok = False

    for name, cmd in [("codex", ["codex", "--version"]),
                      ("claude", ["claude", "--version"])]:
        if not shutil.which(name):
            continue
        try:
            v = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            print(f"  {'OK  ' if v.returncode == 0 else 'WARN'} {name} auth — "
                  f"{(v.stdout or v.stderr).strip()[:60]}")
        except Exception as exc:                      # noqa: BLE001
            print(f"  WARN {name} — {exc}")

    if not cfg.repos:
        print("  MISS config — no repos in agentloop.toml; the loop will do nothing")
        ok = False
    print(f"  caps: {cfg.max_concurrent_agents} agents total, "
          f"{cfg.max_concurrent_per_repo} per repo")
    if cfg.max_concurrent_per_repo > cfg.max_concurrent_agents:
        print("  WARN caps: per-repo ceiling exceeds the global one, so it "
              "never binds and one repo can take every slot")
    for r in cfg.repos:
        print(f"  repo {r.slug} auto_merge={r.auto_merge}")
        # The clone that isolation depends on. Checked here because the failure
        # it guards against is silent: pushing one project's work to another.
        ws = Workspace(WORKSPACE, r.slug)
        if (ws.clone / ".git").exists():
            try:
                origin = git(["remote", "get-url", "origin"], ws.clone, check=False)
            except Exception:                             # noqa: BLE001
                origin = ""
            if origin and r.slug.lower() not in origin.lower():
                print(f"  MISS clone — {ws.clone} points at {origin}, not {r.slug}")
                ok = False
            else:
                print(f"  OK   clone — {ws.clone}")
        missing = gh.missing_labels(r.slug, CONTROL_LABELS)
        if missing:
            print(f"  MISS labels — {r.slug} has no {', '.join(missing)}")
            print(f"       gh label create {missing[0]} --repo {r.slug}")
            ok = False
        else:
            print("  OK   labels — all control labels present")
    print(f"  workspace {WORKSPACE}")
    return 0 if ok else 1


def status(cfg: Config) -> int:
    for repo in cfg.repos:
        ws = Workspace(WORKSPACE, repo.slug)
        ready = gh.ready_issues(repo.slug, LABEL_READY, "agent:working", "agent:stop")
        prs = gh.open_prs(repo.slug, LABEL_PR)
        print(f"\n{repo.slug}")
        print(f"  ready issues : {len(ready)}  {[i['number'] for i in ready]}")
        print(f"  agents live  : {len(ws.active())}/{cfg.max_concurrent_agents} "
              f"{ws.active()}")
        print(f"  open agent PRs: {len(prs)}  {[p['number'] for p in prs]}")
    b = Budget(Path(WORKSPACE) / "judge-budget.json",
               cfg.max_judge_calls, cfg.judge_window_hours)
    print(f"\njudge budget : {b.status()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _log()
    ap = argparse.ArgumentParser(prog="agentloop")
    ap.add_argument("command", choices=["issues", "prs", "status", "doctor"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = Config.load()
    if args.dry_run:
        cfg.dry_run = True
    if cfg.dry_run:
        logging.getLogger("agentloop").info("DRY RUN — no GitHub writes, no agent calls")

    if args.command == "doctor":
        return doctor(cfg)
    if args.command == "status":
        return status(cfg)

    if args.command == "prs":
        # PRs are independent per repo and cost no shared slots, so order is
        # only fairness of attention, not of capacity.
        for repo in _rotated(cfg.repos):
            for line in pr_watcher(cfg, repo, WORKSPACE):
                print(f"[{repo.slug}] {line}")
        return 0

    return _run_issues(cfg)


def _rotated(repos: list) -> list:
    """The repo list, rotated by tick, so position in agentloop.toml is not a
    priority. Persisted rather than random: over a day every repo should lead
    the same number of times, which a coin flip does not guarantee."""
    if len(repos) < 2:
        return list(repos)
    cursor = Path(WORKSPACE) / "cursor"
    try:
        i = int(cursor.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        i = 0
    try:
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text(str((i + 1) % len(repos)), encoding="utf-8")
    except OSError:
        pass
    i %= len(repos)
    return list(repos[i:]) + list(repos[:i])


def _run_issues(cfg: Config) -> int:
    """One pass per repository, in a rotating order.

    Fairness comes from two things and deliberately not from a scheduler. The
    previous version walked `cfg.repos` in config order and each call took every
    free slot it could, so with a global cap of two and a backlog on the first
    repository the second one never started an agent at all: not slow, silent.

    Now `max_concurrent_per_repo` stops any one repo taking the pool, and
    `_rotated` moves which repo goes first each tick so config order is not a
    priority. That is enough, and an earlier draft of this function that
    re-offered leftover slots in extra rounds was worse: it re-ran the collector
    and the reaper on every round, spending GitHub API calls to discover the
    per-repo cap it had already hit. Raise the per-repo cap if you want more
    agents on one project.
    """
    for repo in _rotated(cfg.repos):
        for line in issue_watcher(cfg, repo, WORKSPACE):
            print(f"[{repo.slug}] {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
