"""Honouring a usage limit, not merely recognising it.

#19 fixed recognition. This suite covers what the loop does next, which was
the expensive half: `_collect_finished` released a limited issue so "a later
tick" could retry it, and the later tick is ten minutes away. Measured on the
box across three days: 238 builder spawns, 178 killed by a usage limit inside
two minutes, eight issues respawned 25 to 28 times each, and 22 to 28% of all
tokens spent on prompt prefixes for agents that died reading a refusal.

The three things that must hold, in order of what they cost when broken:

1. A limit stops every repo, because one subscription funds them all.
2. Collecting and reaping still run while limited: they cost nothing and free
   state, and a loop that stops collecting never notices the work it finished.
3. The reset time comes off the notice rather than a fixed guess, and a time
   already past today means tomorrow.
"""
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from agentloop.config import Config, Repo
from agentloop.cooldown import BLIND_WAIT, blocked_until, note_limit, resets_at
from agentloop.runner import invoke
from agentloop.watchers import issue_watcher, pr_watcher

# The strings tests/test_limits.py pins, with the times they must resolve to.
OBSERVED = [
    ("You've hit your session limit · resets 4am (UTC)", 4, 0),
    ("You've hit your session limit · resets 2:10am (UTC)", 2, 10),
    ("You've hit your usage limit · resets 4am (UTC)", 4, 0),
    ("5-hour limit reached ∙ resets 3pm", 15, 0),
    ("resets at 11pm", 23, 0),
    ("resets 23:00", 23, 0),
    ("resets 12am", 0, 0),
    ("resets 12pm", 12, 0),
]


def test_reset_time_is_read_off_the_notice():
    now = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    for text, hour, minute in OBSERVED:
        when = resets_at(text, now=now)
        assert when is not None, text
        assert (when.hour, when.minute) == (hour, minute), text


def test_a_time_already_past_means_tomorrow():
    # The spiral this module stops resumes instantly if the cooldown lands in
    # the past, which is what "resets 4am" read at 23:00 would do naively.
    now = datetime(2026, 9, 21, 23, 0, tzinfo=UTC)
    when = resets_at("You've hit your session limit · resets 4am (UTC)", now=now)
    assert when == datetime(2026, 9, 22, 4, 0, tzinfo=UTC)


def test_a_notice_with_no_time_still_holds():
    now = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    assert resets_at("Claude usage limit reached", now=now) is None


def test_note_and_read_back(tmp_path):
    now = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    until = note_limit(tmp_path, "resets 4am (UTC)", now=now)
    assert until == datetime(2026, 9, 21, 4, 0, tzinfo=UTC)
    assert blocked_until(tmp_path, now=now) == until
    # And it lapses on its own, with no cleanup step to forget.
    assert blocked_until(tmp_path, now=until + timedelta(seconds=1)) is None


def test_no_cooldown_file_means_not_limited(tmp_path):
    assert blocked_until(tmp_path) is None


def test_an_unreadable_cooldown_fails_towards_working(tmp_path):
    # Jamming shut is worse than the bug this prevents: that wastes a window,
    # this would waste every window.
    (tmp_path / "limit-cooldown").write_text("not a timestamp", encoding="utf-8")
    assert blocked_until(tmp_path) is None


def test_a_blind_wait_never_shortens_a_parsed_reset(tmp_path):
    # Several agents report the same limit within seconds and only some name
    # the time. The one that does must win.
    now = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    note_limit(tmp_path, "resets 4am (UTC)", now=now)
    again = note_limit(tmp_path, "Claude usage limit reached", now=now)
    assert again == datetime(2026, 9, 21, 4, 0, tzinfo=UTC)
    assert again > now + BLIND_WAIT


def test_a_later_reset_does_extend_the_hold(tmp_path):
    now = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    note_limit(tmp_path, "resets 2am (UTC)", now=now)
    assert note_limit(tmp_path, "resets 6am (UTC)", now=now) == \
        datetime(2026, 9, 21, 6, 0, tzinfo=UTC)


def test_the_cooldown_is_account_wide(tmp_path):
    # Written to the shared root, so a Workspace for any repo sees it. If this
    # ever becomes per-repo, a limit hit on one project lets the others keep
    # spawning into the same exhausted window.
    from agentloop.worktree import Workspace
    a = Workspace(tmp_path, "MHutatah/ipa-community")
    b = Workspace(tmp_path, "MHutatah/arab-football-unified-api")
    now = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    note_limit(a.shared, "resets 4am (UTC)", now=now)
    assert blocked_until(b.shared, now=now) == datetime(2026, 9, 21, 4, 0, tzinfo=UTC)


def test_a_null_byte_in_the_prompt_reaches_the_cli_stripped():
    # PR #140 added a binary fixture, so the judge prompt carried a NUL and
    # subprocess raised ValueError out of invoke() every five minutes for a
    # day, though invoke() promises it never raises for a CLI failure.
    #
    # ASSERTING ON WHAT THE CHILD RECEIVED, not merely that the call returned.
    # The first version of this test ran a child that ignored its argument, so
    # it passed with the fix reverted: POSIX raises on the NUL but Windows
    # quietly truncates the command line, and neither showed up in `ok`.
    echo = "import sys; sys.stdout.write(sys.argv[1])"
    res = invoke([sys.executable, "-c", echo], "left\0right")
    assert res.ok, res.error
    assert res.text == "leftright"


def test_a_missing_cli_is_still_a_result_not_a_raise():
    res = invoke(["definitely-not-a-real-cli-38204"], "hello")
    assert not res.ok
    assert "not found" in res.error


# ── the guard in the watchers, which is the part that saves the window ───────
#
# The tests above call cooldown.py directly, so they all pass with the call
# sites removed. These two drive the watchers, which is the only way to catch
# a loop that has stopped honouring the hold.

def _held(tmp_path):
    now = datetime.now(UTC)
    note_limit(tmp_path, "resets 4am (UTC)", now=now)
    return Config(repos=[Repo(slug="o/r")])


def test_a_held_issue_watcher_starts_nothing_but_still_collects(tmp_path):
    cfg = _held(tmp_path)
    ready = [{"number": 42, "title": "t", "body": "", "labels": []}]
    spawned, collected = [], []
    with patch("agentloop.gh.ready_issues", return_value=ready), \
         patch("agentloop.gh.backlog", return_value=[]), \
         patch("agentloop.gh.issues_with_open_pr", return_value=set()), \
         patch("agentloop.gh.add_label"), \
         patch("agentloop.tmux.live_windows", return_value=[]), \
         patch("agentloop.tmux.spawn", side_effect=lambda *a, **k: spawned.append(a)), \
         patch("agentloop.watchers._collect_finished",
               side_effect=lambda *a: collected.append("yes") or ["collected"]), \
         patch("agentloop.watchers._reap", return_value=[]):
        out = issue_watcher(cfg, cfg.repos[0], tmp_path)

    assert not spawned, "a held loop must not spawn an agent into a dead window"
    assert collected, "collection costs no tokens and must keep running"
    assert any("usage limit until" in line for line in out), out


def test_a_held_pr_watcher_judges_nothing(tmp_path):
    cfg = _held(tmp_path)
    opened = []
    with patch("agentloop.gh.open_prs",
               side_effect=lambda *a, **k: opened.append(a) or [{"number": 7}]):
        out = pr_watcher(cfg, cfg.repos[0], tmp_path)

    assert not opened, "a held loop must not even list PRs to judge"
    assert any("usage limit until" in line for line in out), out


def test_an_expired_hold_lets_the_loop_run_again(tmp_path):
    # The hold must lapse by itself. Nothing clears this file, so if it did not
    # expire on read the loop would stop for good at the first limit.
    past = datetime.now(UTC) - timedelta(hours=2)
    (tmp_path / "limit-cooldown").write_text(past.isoformat(), encoding="utf-8")
    cfg = Config(repos=[Repo(slug="o/r")])
    with patch("agentloop.gh.open_prs", return_value=[]) as listed:
        pr_watcher(cfg, cfg.repos[0], tmp_path)
    assert listed.called, "an expired hold must not keep the loop stopped"
