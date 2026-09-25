"""Runtime tunables and quota reading.

Settings are adjustable from a phone, so the bounds are the safety property: a
stray tap must not be able to push the judge budget somewhere that drains the
plan, and nothing here may widen what agents are allowed to touch.
"""
import json
import time

import pytest

from agentloop import settings as settings_mod
from agentloop.settings import BOUNDS, Settings
from agentloop.usage import Window, codex_usage


@pytest.fixture()
def st(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "PATH", tmp_path / "settings.json")
    return Settings.load()


def test_defaults_are_conservative(st):
    # 400, not 12: the old ceiling was sized for a Pro plan shared with
    # interactive work. On Max the budget exists to stop a crash loop, not to
    # ration reviews, and a cap that small silently held pull requests.
    assert st.max_judge_calls == 400
    assert st.max_attempts_per_issue == 3


def test_one_builder_is_the_default(st):
    """Standing instruction, and the expensive default to get wrong.

    Four concurrent builders drained a fresh five-hour window in about forty
    minutes and left the loop idle for the remaining four and a half, because
    the window is rolling from the first message rather than a nightly reset.
    Raising this is a deliberate act with a decision tree behind it, not a
    default: see docs/CONCURRENCY.md.
    """
    assert st.max_concurrent_agents == 1
    assert st.max_concurrent_per_repo == 1
    # And the floor stays 1, so nothing can be tuned down to a loop that
    # never starts anything: that failure is indistinguishable from idle.
    assert BOUNDS["max_concurrent_agents"][0] == 1
    assert BOUNDS["max_concurrent_per_repo"][0] == 1


def test_adjust_persists(st, tmp_path):
    st.adjust("max_judge_calls", +3)
    assert Settings.load().max_judge_calls == 403


@pytest.mark.parametrize("key", list(BOUNDS))
def test_cannot_exceed_bounds(st, key):
    lo, hi = BOUNDS[key]
    # A step large enough to saturate ANY bound. +999 used to be plenty; the
    # judge-call ceiling is now 5000, and a step that no longer reaches the
    # bound would have tested nothing while still passing.
    step = 10 ** 7
    st.adjust(key, +step)
    assert getattr(Settings.load(), key) == hi
    st.adjust(key, -step)
    assert getattr(Settings.load(), key) == lo


def test_unknown_key_is_refused(st):
    msg = st.adjust("guarded_paths", 1)
    assert "unknown setting" in msg
    assert not hasattr(Settings.load(), "guarded_paths")


def test_corrupt_file_falls_back_to_defaults(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    p.write_text("}{ not json", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "PATH", p)
    assert Settings.load().max_judge_calls == 400


def test_out_of_range_file_is_clamped_on_load(tmp_path, monkeypatch):
    """A hand-edited file must not bypass the bounds either."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"max_judge_calls": 99999}), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "PATH", p)
    assert Settings.load().max_judge_calls == BOUNDS["max_judge_calls"][1]


# ── usage ────────────────────────────────────────────────────────────────────
def test_window_labels_a_weekly_limit():
    w = Window(17.0, 10080, int(time.time()) + 3 * 86400)
    assert w.label == "weekly"
    assert w.resets_in.startswith("2d") or w.resets_in.startswith("3d")


def test_window_labels_shorter_windows():
    assert Window(1, 300, 0).label == "5h"
    assert Window(1, 1440, 0).label == "1-day"


def test_expired_window_reports_zero_not_negative():
    assert Window(50, 10080, int(time.time()) - 999).resets_in == "0m"


def test_codex_usage_is_graceful_without_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr("agentloop.usage.CODEX_SESSIONS", tmp_path / "nope")
    u = codex_usage()
    assert not u.ok and u.error


def test_codex_usage_parses_a_rollout(tmp_path, monkeypatch):
    d = tmp_path / "sessions" / "2026"
    d.mkdir(parents=True)
    (d / "r.jsonl").write_text(json.dumps({
        "payload": {
            "info": {"total_token_usage": {"total_tokens": 470086}},
            "rate_limits": {"primary": {"used_percent": 17.0,
                                        "window_minutes": 10080,
                                        "resets_at": int(time.time()) + 86400}},
        }}) + "\n", encoding="utf-8")
    monkeypatch.setattr("agentloop.usage.CODEX_SESSIONS", tmp_path / "sessions")
    u = codex_usage()
    assert u.ok
    assert u.primary.used_percent == 17.0
    assert u.primary.label == "weekly"
    assert u.total_tokens == 470086


def test_every_claude_agent_runs_the_same_pinned_opus():
    """Exact ids, never a bare alias: config.py argues this for the judge, and
    the same reasoning covers the implementer and the overseer window. A bare
    "opus" resolves to whatever the CLI calls current, which is the ambiguity
    worth removing from the file that gates merges.
    """
    import re
    from pathlib import Path

    from agentloop.config import IMPLEMENTER, IMPLEMENTER_MODEL, JUDGE, JUDGE_MODEL

    assert IMPLEMENTER_MODEL == "claude-opus-5-5"
    assert JUDGE_MODEL == "claude-opus-5-5"
    # The flag actually handed to the CLI, not just the constant beside it.
    assert IMPLEMENTER[IMPLEMENTER.index("--model") + 1] == "claude-opus-5-5"
    assert JUDGE[JUDGE.index("--model") + 1] == "claude-opus-5-5"

    start = Path(__file__).resolve().parents[1] / "deploy" / "overseer" / "start.sh"
    line = next(ln for ln in start.read_text(encoding="utf-8").splitlines()
                if re.match(r"\s*claude ", ln))
    assert "--model claude-opus-5-5" in line, line
