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
    assert st.max_judge_calls == 12
    assert st.max_concurrent_agents == 2
    assert st.max_attempts_per_issue == 3


def test_adjust_persists(st, tmp_path):
    st.adjust("max_judge_calls", +3)
    assert Settings.load().max_judge_calls == 15


@pytest.mark.parametrize("key", list(BOUNDS))
def test_cannot_exceed_bounds(st, key):
    lo, hi = BOUNDS[key]
    st.adjust(key, +999)
    assert getattr(Settings.load(), key) == hi
    st.adjust(key, -999)
    assert getattr(Settings.load(), key) == lo


def test_unknown_key_is_refused(st):
    msg = st.adjust("guarded_paths", 1)
    assert "unknown setting" in msg
    assert not hasattr(Settings.load(), "guarded_paths")


def test_corrupt_file_falls_back_to_defaults(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    p.write_text("}{ not json", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "PATH", p)
    assert Settings.load().max_judge_calls == 12


def test_out_of_range_file_is_clamped_on_load(tmp_path, monkeypatch):
    """A hand-edited file must not bypass the bounds either."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"max_judge_calls": 9999}), encoding="utf-8")
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
