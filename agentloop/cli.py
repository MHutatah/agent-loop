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
from agentloop.config import LABEL_PR, LABEL_READY, Config
from agentloop.watchers import issue_watcher, pr_watcher
from agentloop.worktree import Workspace

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

    for r in cfg.repos:
        print(f"  repo {r.slug} auto_merge={r.auto_merge}")
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

    fn = issue_watcher if args.command == "issues" else pr_watcher
    for repo in cfg.repos:
        for line in fn(cfg, repo, WORKSPACE):
            print(f"[{repo.slug}] {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
