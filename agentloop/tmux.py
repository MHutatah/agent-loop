"""Agents run inside tmux windows so you can watch them work.

Two reasons this isn't a plain subprocess:

1. **Observability.** A tmux window can be attached to from anywhere — including a
   read-only web terminal on your phone — so a stuck agent is diagnosable instead
   of merely late.
2. **Non-blocking ticks.** An agent may run for half an hour. Spawning it detached
   means the watcher tick returns immediately and the next tick collects the
   result, rather than a systemd oneshot sitting occupied for the duration.

Every agent gets its own window in one session, plus a log file, so output survives
after the window closes.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

log = logging.getLogger("agentloop.tmux")

SESSION = "agents"


def _tmux(args: list[str], *, check: bool = False, timeout: int = 30) -> tuple[int, str]:
    """Run tmux. A missing binary reports a non-zero code rather than raising, so
    `available()` can answer honestly and callers degrade instead of crashing."""
    try:
        proc = subprocess.run(["tmux", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        if check:
            raise RuntimeError(f"tmux {' '.join(args[:2])}: {exc}") from exc
        return 127, ""
    if check and proc.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args[:2])}: {(proc.stderr or '').strip()[:200]}")
    return proc.returncode, (proc.stdout or "").strip()


def available() -> bool:
    return _tmux(["-V"])[0] == 0


def ensure_session() -> None:
    """The long-lived session everything attaches to."""
    if _tmux(["has-session", "-t", SESSION])[0] != 0:
        _tmux(["new-session", "-d", "-s", SESSION, "-n", "dashboard"], check=True)
        # Keep finished windows around long enough to read what happened.
        _tmux(["set-option", "-t", SESSION, "remain-on-exit", "on"])


def window_name(key: str, issue: int) -> str:
    """The tmux window for one issue of ONE repository.

    The session is global while worktrees and logs are per-repo, so the window
    name is the one identifier that has to carry the repository. It did not:
    windows were named `issue-<n>`, so repo A's issue #12 and repo B's issue #12
    were the same window. tmux permits duplicate window names, so nothing
    errored: `is_running` matched whichever came first, `exit_code` read the
    other one's status file, and the collector opened a pull request for one
    repo's issue against the other. Two digits of shared issue number were
    enough to cross the wires.

    `--` rather than `:` or `.`, both of which are tmux target separators.
    """
    return f"{key}--{issue}"


def _stem(issue: int) -> str:
    """Log file stem. Deliberately NOT window_name: log directories are already
    per-repo, so the key would be redundant in the path and would silently
    orphan every log written before this change."""
    return f"issue-{issue}"


def is_running(key: str, issue: int) -> bool:
    rc, out = _tmux(["list-windows", "-t", SESSION, "-F", "#{window_name} #{pane_dead}"])
    if rc != 0:
        return False
    for line in out.splitlines():
        name, _, dead = line.partition(" ")
        if name == window_name(key, issue):
            return dead.strip() == "0"
    return False


def has_window(key: str, issue: int) -> bool:
    rc, out = _tmux(["list-windows", "-t", SESSION, "-F", "#{window_name}"])
    return rc == 0 and window_name(key, issue) in out.split()


def spawn(key: str, issue: int, command: list[str], prompt: str, cwd: str | Path,
          log_dir: str | Path) -> Path:
    """Start an agent in its own window. Returns the path to its status file.

    The command writes its exit code to `<log_dir>/issue-N.rc` on completion, which
    is how a later tick knows the run finished and whether it succeeded.
    """
    ensure_session()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    out_file = log_dir / f"{_stem(issue)}.log"
    rc_file = log_dir / f"{_stem(issue)}.rc"
    prompt_file = log_dir / f"{_stem(issue)}.prompt"
    prompt_file.write_text(prompt, encoding="utf-8")
    rc_file.unlink(missing_ok=True)

    # Read the prompt from a file: it contains newlines, quotes and Arabic, none of
    # which survive being embedded in a shell command line intact.
    inner = (
        f"{shlex.join(command)} \"$(cat {shlex.quote(str(prompt_file))})\" "
        f"< /dev/null 2>&1 | tee {shlex.quote(str(out_file))}; "
        f"echo ${{PIPESTATUS[0]}} > {shlex.quote(str(rc_file))}"
    )
    kill(key, issue)
    _tmux(["new-window", "-d", "-t", f"{SESSION}:", "-n", window_name(key, issue),
           "-c", str(cwd), "bash", "-lc", inner], check=True)
    log.info("spawned agent for %s#%s in tmux window %s",
             key, issue, window_name(key, issue))
    return rc_file


def exit_code(issue: int, log_dir: str | Path) -> int | None:
    """The finished run's exit code, or None if it's still going."""
    rc_file = Path(log_dir) / f"{_stem(issue)}.rc"
    if not rc_file.exists():
        return None
    try:
        return int(rc_file.read_text(encoding="utf-8").strip() or 1)
    except (ValueError, OSError):
        return 1


def output(issue: int, log_dir: str | Path, tail: int = 20000) -> str:
    """Last `tail` bytes of an agent's log.

    Seeks from the end rather than reading the file and slicing: these logs reach
    hundreds of KB, and the console renders every card on every request — reading
    each one in full was the dominant cost of a page load.
    """
    f = Path(log_dir) / f"{_stem(issue)}.log"
    if not f.exists():
        return ""
    try:
        with f.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - tail))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def log_size(issue: int, log_dir: str | Path) -> int:
    f = Path(log_dir) / f"{_stem(issue)}.log"
    try:
        return f.stat().st_size
    except OSError:
        return 0


def discard_log(issue: int, log_dir: str | Path) -> None:
    """Drop a finished agent's log. Called once its PR merges — otherwise every
    issue the loop has ever run stays on the console forever, and both page
    weight and per-render disk reads grow without bound."""
    for suffix in (".log", ".rc", ".prompt"):
        (Path(log_dir) / f"{_stem(issue)}{suffix}").unlink(missing_ok=True)


def kill(key: str, issue: int) -> None:
    _tmux(["kill-window", "-t", f"{SESSION}:{window_name(key, issue)}"])


def live_windows() -> list[tuple[str, int]]:
    """Every live agent as (repo key, issue), across all repositories.

    Returns pairs rather than issue numbers because capacity is global while
    issue numbers are not unique across repos: as a bare list of ints, two
    repos each running their own #12 counted as one agent, so the concurrency
    cap leaked a slot for every collision.
    """
    rc, out = _tmux(["list-windows", "-t", SESSION, "-F", "#{window_name} #{pane_dead}"])
    if rc != 0:
        return []
    live: list[tuple[str, int]] = []
    for line in out.splitlines():
        name, _, dead = line.partition(" ")
        if dead.strip() != "0" or "--" not in name:
            continue
        key, _, num = name.rpartition("--")
        if key and num.isdigit():
            live.append((key, int(num)))
    return live


def live_for(key: str) -> list[int]:
    """Live issue numbers for one repository."""
    return [n for k, n in live_windows() if k == key]
