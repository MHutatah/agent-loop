"""The judge budget — what stops the loop eating the quota you use yourself.

The Claude allowance is shared between this loop and your own interactive
work, so the guard exists to make the loop stop first.
"""
import json
import time

import pytest

from agentloop.budget import Budget


@pytest.fixture()
def budget(tmp_path):
    return Budget(tmp_path / "b.json", max_calls=3, window_hours=5)


def test_starts_with_full_allowance(budget):
    assert budget.allow() and budget.remaining() == 3 and budget.used() == 0


def test_spends_down_and_then_refuses(budget):
    for _ in range(3):
        assert budget.allow()
        budget.record()
    assert budget.used() == 3
    assert budget.remaining() == 0
    assert not budget.allow()          # the loop stops here, leaving quota for him


def test_old_calls_fall_out_of_the_window(tmp_path):
    b = Budget(tmp_path / "b.json", max_calls=2, window_hours=5)
    stale = time.time() - (6 * 3600)   # older than the window
    (tmp_path / "b.json").write_text(json.dumps([stale, stale]), encoding="utf-8")
    assert b.used() == 0 and b.allow()


def test_partial_window_expiry(tmp_path):
    b = Budget(tmp_path / "b.json", max_calls=2, window_hours=5)
    now = time.time()
    (tmp_path / "b.json").write_text(
        json.dumps([now - 6 * 3600, now - 60]), encoding="utf-8")
    assert b.used() == 1 and b.remaining() == 1


def test_survives_a_corrupt_or_missing_file(tmp_path):
    """A broken counter must fail open, not wedge the loop permanently."""
    p = tmp_path / "b.json"
    b = Budget(p, max_calls=2)
    assert b.allow()                    # missing file
    p.write_text("not json at all", encoding="utf-8")
    assert b.allow() and b.used() == 0
    p.write_text('{"unexpected": "shape"}', encoding="utf-8")
    assert b.allow()


def test_persists_across_instances(tmp_path):
    p = tmp_path / "b.json"
    Budget(p, max_calls=2).record()
    assert Budget(p, max_calls=2).used() == 1   # a restart must not reset the count


def test_status_is_readable_on_a_phone(budget):
    budget.record()
    s = budget.status()
    assert "1/3 judge calls" in s and "resets in" in s


def test_zero_means_unlimited_not_never(tmp_path):
    """0 READ AS A PLAIN CEILING MEANT "NEVER JUDGE", which would hold every
    pull request forever with a reason that reads like a spend decision. On Max
    the natural way to say "stop rationing reviews" is to set it to 0."""
    b = Budget(tmp_path / "b.json", 0, 5.0)
    assert b.unlimited is True
    assert b.allow() is True
    for _ in range(50):
        b.record()
    assert b.allow() is True
    assert b.used() == 50
    assert "no cap" in b.status()


def test_a_real_ceiling_still_stops(tmp_path):
    b = Budget(tmp_path / "c.json", 3, 5.0)
    for _ in range(3):
        assert b.allow() is True
        b.record()
    assert b.allow() is False
    assert "3/3" in b.status()
