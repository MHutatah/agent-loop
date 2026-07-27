"""The attempt cap, and the marker that keeps our own comments from looking human.

Both of these were found by PR #29 sitting green, mergeable and unmerged for a
day: the cap had latched with no way to clear it, and the loop's own "stopping
after 3 attempts" comment counted as a human asking for changes.
"""
from unittest.mock import patch

from agentloop import gh
from agentloop.config import MARKER
from agentloop.watchers import _attempts


def _c(body, at):
    return {"body": body, "createdAt": at}


ATTEMPT = "Addressed the points above. <!-- agent-loop:attempt -->"


def test_attempts_counts_our_attempt_comments():
    assert _attempts([_c(ATTEMPT, "2026-01-01T00:00:00Z"),
                      _c("looks good", "2026-01-02T00:00:00Z"),
                      _c(ATTEMPT, "2026-01-03T00:00:00Z")]) == 2


def test_removing_needs_human_gives_the_agent_a_fresh_budget():
    """Taking the label off means "try again" — attempts before it must not count.

    Without this the counter is already at the cap when the label is cleared, so
    the next tick immediately re-escalates and the PR can never be resumed.
    """
    comments = [_c(ATTEMPT, "2026-01-01T00:00:00Z"),
                _c(ATTEMPT, "2026-01-02T00:00:00Z"),
                _c(ATTEMPT, "2026-01-03T00:00:00Z")]
    assert _attempts(comments) == 3                       # capped
    assert _attempts(comments, "2026-01-04T00:00:00Z") == 0   # human reset it
    assert _attempts(comments, "2026-01-02T12:00:00Z") == 1   # only what came after


def test_never_reset_counts_every_attempt():
    """An empty reset stamp must not swallow the cap — that would uncap the loop."""
    assert _attempts([_c(ATTEMPT, "2026-01-01T00:00:00Z")], "") == 1


def test_comment_is_always_marked_as_ours():
    """An unmarked comment of ours reads as human intervention and blocks merge,
    so the marker is stamped in gh.comment rather than at each call site."""
    sent = []
    with patch.object(gh, "run", lambda args, **kw: sent.append(args) or ""):
        gh.comment("o/r", 1, "Stopping after 3 attempts — this needs a human.")
    assert MARKER in sent[0][-1]


def test_comment_does_not_double_stamp():
    sent = []
    with patch.object(gh, "run", lambda args, **kw: sent.append(args) or ""):
        gh.comment("o/r", 1, f"Addressed the points above. {MARKER}")
    assert sent[0][-1].count(MARKER) == 1


def test_last_unlabel_picks_the_most_recent_removal():
    timeline = [
        {"event": "labeled", "label": {"name": "agent:needs-human"},
         "created_at": "2026-01-05T00:00:00Z"},
        {"event": "unlabeled", "label": {"name": "agent:needs-human"},
         "created_at": "2026-01-02T00:00:00Z"},
        {"event": "unlabeled", "label": {"name": "agent:pr"},
         "created_at": "2026-01-09T00:00:00Z"},          # different label
        {"event": "unlabeled", "label": {"name": "agent:needs-human"},
         "created_at": "2026-01-04T00:00:00Z"},
        {"event": "commented", "created_at": "2026-01-10T00:00:00Z"},  # no label key
    ]
    with patch.object(gh, "run", lambda args, **kw: __import__("json").dumps(timeline)):
        assert gh.last_unlabel("o/r", 1, "agent:needs-human") == "2026-01-04T00:00:00Z"


def test_last_unlabel_is_empty_when_never_removed():
    with patch.object(gh, "run", lambda args, **kw: "[]"):
        assert gh.last_unlabel("o/r", 1, "agent:needs-human") == ""


def test_last_unlabel_survives_a_gh_failure():
    """A timeline lookup failing must not uncap the loop — "" counts everything."""
    def boom(args, **kw):
        raise gh.GhError("network")
    with patch.object(gh, "run", boom):
        assert gh.last_unlabel("o/r", 1, "agent:needs-human") == ""
