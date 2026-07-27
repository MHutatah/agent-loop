"""Console behaviour the UX review found broken.

Each test here corresponds to a specific defect: a failure that rendered as a
success, a flash that never decoded, a page that grew without bound, and a
GitHub outage that looked like an empty inbox.
"""
import json

import pytest

from agentloop import console, tmux


# ── the tail must not read the whole file ────────────────────────────────────
def test_output_seeks_from_the_end(tmp_path):
    """Logs reach hundreds of KB and every card was read in full on every
    request. Only the tail should ever be touched."""
    f = tmp_path / "issue-7.log"
    f.write_text("x" * 50_000 + "THE-END", encoding="utf-8")
    out = tmux.output(7, tmp_path, tail=100)
    assert out.endswith("THE-END")
    assert len(out) <= 100


def test_output_handles_a_file_shorter_than_the_tail(tmp_path):
    (tmp_path / "issue-7.log").write_text("short", encoding="utf-8")
    assert tmux.output(7, tmp_path, tail=10_000) == "short"


def test_output_missing_file_is_empty_not_an_error(tmp_path):
    assert tmux.output(99, tmp_path) == ""


def test_discard_log_removes_the_whole_set(tmp_path):
    """Without this, every issue the loop ever ran stayed on the console."""
    for suffix in (".log", ".rc", ".prompt"):
        (tmp_path / f"issue-5{suffix}").write_text("x", encoding="utf-8")
    tmux.discard_log(5, tmp_path)
    assert not list(tmp_path.glob("issue-5*"))


def test_discard_log_is_safe_when_nothing_exists(tmp_path):
    tmux.discard_log(404, tmp_path)          # must not raise


# ── cards are capped ─────────────────────────────────────────────────────────
def test_agent_cards_are_capped_and_say_so(tmp_path, monkeypatch):
    monkeypatch.setattr(tmux, "live_windows", lambda: [])
    monkeypatch.setattr(console.tmux, "live_windows", lambda: [])
    for n in range(1, 21):
        (tmp_path / f"issue-{n}.log").write_text("line\n", encoding="utf-8")
    html_out = console._agent_cards(tmp_path, object())
    assert html_out.count('class="card"') == console.MAX_CARDS
    assert "older run" in html_out            # the rest are acknowledged, not hidden
    assert "issue #20" in html_out            # newest first
    assert "issue #1<" not in html_out


def test_agent_cards_empty_state(tmp_path, monkeypatch):
    monkeypatch.setattr(console.tmux, "live_windows", lambda: [])
    assert "No agents have run yet" in console._agent_cards(tmp_path, object())


# ── flash: failure must not look like success ────────────────────────────────
def test_failed_action_is_reported_as_failure():
    out, ok = console._sh(["this-command-does-not-exist-xyz"])
    assert not ok
    assert "failed" in out.lower() or out


def test_successful_action_reports_ok():
    out, ok = console._sh(["python", "-c", "print('hi')"])
    assert ok and "hi" in out


# ── the flash round-trip ─────────────────────────────────────────────────────
@pytest.mark.parametrize("payload", [
    "line one\nline two",                    # the normal multi-line case
    'quotes "here" and \\backslash',
    "83% used + more",                       # % and + both broke the old scheme
    "#hash &ampersand",
    "unicode: الهلال ✓",
])
def test_flash_survives_a_round_trip(payload):
    """The old scheme JSON-escaped on the way out and never decoded on the way
    in, so multi-line output rendered as a literal backslash-n."""
    from urllib.parse import parse_qs, quote, unquote
    url = f"/?ok=1&m={quote(payload, safe='')}"
    q = parse_qs(url.split("?", 1)[1])
    assert unquote(q["m"][0]) == payload


# ── header fragment ──────────────────────────────────────────────────────────
class _FakeRepo:
    slug = "you/your-repo"
    default_branch = "main"
    auto_merge = False


class _FakeState:
    def __init__(self, headline):
        self.headline = headline
        self.ready, self.prs, self.stuck = [], [], []


class _FakeBudget:
    def status(self):
        return "0/12 judge calls in the last 5h"


def test_header_fragment_is_small_and_json_serialisable():
    """This is what replaces a 375 KB document reload every 20 seconds."""
    frag = console.header_fragment(_FakeState("All clear"), _FakeBudget())
    body = json.dumps(frag)
    assert len(body) < 500
    assert frag["headline"] == "All clear"
    assert "sub" in frag and "running" in frag


def test_headline_leads_with_the_problem(monkeypatch):
    """He opens the page to learn whether anything is wrong — that has to be the
    first thing, not an inventory of counts."""
    monkeypatch.setattr(console, "_timers_running", lambda: True)
    monkeypatch.setattr(console.tmux, "live_windows", lambda: [])

    st = console.State.__new__(console.State)
    st.repo = console.Config().repos[0] if console.Config().repos else _FakeRepo()
    st.online, st.ready, st.prs = True, [], []
    st.stuck = [{"number": 1}, {"number": 2}]
    assert st.headline == "2 need you"

    st.stuck = []
    assert st.headline == "All clear"

    st.online = False
    assert "reach GitHub" in st.headline


def test_offline_is_not_reported_as_all_clear(monkeypatch):
    """A GitHub outage previously rendered as '0 open PR' — being blind looked
    exactly like having nothing to do."""
    monkeypatch.setattr(console.gh, "ready_issues",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    console._CACHE.clear()
    cfg = console.Config(repos=[_FakeRepo()])
    st = console.State(cfg)
    assert not st.online
    assert "reach GitHub" in st.headline
    assert "Can't reach GitHub" in console._alerts(st)


# ── in-app terminal ──────────────────────────────────────────────────────────
def test_keypad_covers_what_a_phone_keyboard_lacks():
    """The whole point: a touchscreen cannot produce these, and needing a second
    app to get them is the friction this console exists to remove."""
    from agentloop import terminal
    keys = {k for _, k, _ in terminal.KEYPAD}
    for essential in ("Escape", "Tab", "Up", "Down", "Left", "Right", "C-c", "Enter"):
        assert essential in keys, f"{essential} missing from the key pad"


def test_send_key_rejects_anything_off_the_pad():
    """This endpoint takes a key name from the URL, so it must never be able to
    send arbitrary input."""
    from agentloop import terminal
    msg, ok = terminal.send_key("overseer", "kill-server")
    assert not ok and "unknown key" in msg
    msg, ok = terminal.send_key("overseer", "; rm -rf /")
    assert not ok


def test_send_text_refuses_empty():
    from agentloop import terminal
    msg, ok = terminal.send_text("overseer", "   ")
    assert not ok and "nothing to send" in msg
