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


def window_name(issue: int) -> str:
    return f"issue-{issue}"


def is_running(issue: int) -> bool:
    rc, out = _tmux(["list-windows", "-t", SESSION, "-F", "#{window_name} #{pane_dead}"])
    if rc != 0:
        return False
    for line in out.splitlines():
        name, _, dead = line.partition(" ")
        if name == window_name(issue):
            return dead.strip() == "0"
    return False


def has_window(issue: int) -> bool:
    rc, out = _tmux(["list-windows", "-t", SESSION, "-F", "#{window_name}"])
    return rc == 0 and window_name(issue) in out.split()


def spawn(issue: int, command: list[str], prompt: str, cwd: str | Path,
          log_dir: str | Path) -> Path:
    """Start an agent in its own window. Returns the path to its status file.

    The command writes its exit code to `<log_dir>/issue-N.rc` on completion, which
    is how a later tick knows the run finished and whether it succeeded.
    """
    ensure_session()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    out_file = log_dir / f"{window_name(issue)}.log"
    rc_file = log_dir / f"{window_name(issue)}.rc"
    prompt_file = log_dir / f"{window_name(issue)}.prompt"
    prompt_file.write_text(prompt, encoding="utf-8")
    rc_file.unlink(missing_ok=True)

    # Read the prompt from a file: it contains newlines, quotes and Arabic, none of
    # which survive being embedded in a shell command line intact.
    inner = (
        f"{shlex.join(command)} \"$(cat {shlex.quote(str(prompt_file))})\" "
        f"< /dev/null 2>&1 | tee {shlex.quote(str(out_file))}; "
        f"echo ${{PIPESTATUS[0]}} > {shlex.quote(str(rc_file))}"
    )
    kill(issue)
    _tmux(["new-window", "-d", "-t", f"{SESSION}:", "-n", window_name(issue),
           "-c", str(cwd), "bash", "-lc", inner], check=True)
    log.info("spawned agent for issue #%s in tmux window %s", issue, window_name(issue))
    return rc_file


def exit_code(issue: int, log_dir: str | Path) -> int | None:
    """The finished run's exit code, or None if it's still going."""
    rc_file = Path(log_dir) / f"{window_name(issue)}.rc"
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
    f = Path(log_dir) / f"{window_name(issue)}.log"
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
    f = Path(log_dir) / f"{window_name(issue)}.log"
    try:
        return f.stat().st_size
    except OSError:
        return 0


def discard_log(issue: int, log_dir: str | Path) -> None:
    """Drop a finished agent's log. Called once its PR merges — otherwise every
    issue the loop has ever run stays on the console forever, and both page
    weight and per-render disk reads grow without bound."""
    for suffix in (".log", ".rc", ".prompt"):
        (Path(log_dir) / f"{window_name(issue)}{suffix}").unlink(missing_ok=True)


def kill(issue: int) -> None:
    _tmux(["kill-window", "-t", f"{SESSION}:{window_name(issue)}"])


def live_windows() -> list[int]:
    """Issue numbers with a window that hasn't exited."""
    rc, out = _tmux(["list-windows", "-t", SESSION, "-F", "#{window_name} #{pane_dead}"])
    if rc != 0:
        return []
    live = []
    for line in out.splitlines():
        name, _, dead = line.partition(" ")
        if name.startswith("issue-") and dead.strip() == "0":
            try:
                live.append(int(name.split("-", 1)[1]))
            except ValueError:
                continue
    return live
