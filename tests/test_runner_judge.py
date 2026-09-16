"""Rate-limit detection, reply parsing, and judge verdict handling."""
import pytest

from agentloop.judge import Verdict, judge_pr
from agentloop.runner import extract_json, looks_limited
from agentloop.worktree import branch_name


@pytest.mark.parametrize("text", [
    "Error: rate limit exceeded",
    "You've reached your usage limit for the 5 hour window",
    "429 Too Many Requests",
    "Quota exhausted — try again later",
])
def test_limit_phrases_detected(text):
    """Being throttled must be distinguishable from failing: the response is to
    wait and retry the issue later, not to burn an attempt."""
    assert looks_limited(text)


@pytest.mark.parametrize("text", [
    "All tests passed", "committed 3 files", "",
])
def test_normal_output_is_not_a_limit(text):
    assert not looks_limited(text)


def test_extract_json_from_claude_envelope():
    env = '{"type":"result","result":"{\\"pass\\": true, \\"score\\": 8}"}'
    assert extract_json(env) == {"pass": True, "score": 8}


def test_extract_json_through_fences_and_prose():
    txt = 'Here you go:\n```json\n{"pass": false, "score": 3}\n```\nhope that helps'
    assert extract_json(txt) == {"pass": False, "score": 3}


def test_extract_json_returns_none_when_absent():
    assert extract_json("no json here") is None
    assert extract_json("") is None


def test_judge_failure_is_not_a_pass(monkeypatch):
    """If the judge CLI errors, the verdict must be unusable — never approval."""
    from agentloop import judge as judge_mod
    monkeypatch.setattr(judge_mod, "invoke",
                        lambda *a, **k: judge_mod.AgentResult(False, error="boom"))
    v = judge_pr({"number": 1, "title": "t", "body": "b"}, "diff")
    assert not v.passed and not v.usable


def test_judge_garbage_reply_is_not_a_pass(monkeypatch):
    from agentloop import judge as judge_mod
    monkeypatch.setattr(judge_mod, "invoke",
                        lambda *a, **k: judge_mod.AgentResult(True, text="lgtm ship it"))
    v = judge_pr({"number": 1, "title": "t", "body": "b"}, "diff")
    assert not v.passed and not v.usable


def test_judge_parses_a_real_verdict(monkeypatch):
    from agentloop import judge as judge_mod
    reply = ('{"pass": false, "score": 4, '
             '"blocking": ["no test for the new branch"], "notes": "close"}')
    monkeypatch.setattr(judge_mod, "invoke",
                        lambda *a, **k: judge_mod.AgentResult(True, text=reply))
    v = judge_pr({"number": 1, "title": "t", "body": "b"}, "diff")
    assert v.usable and not v.passed
    assert v.blocking == ["no test for the new branch"]
    assert "CHANGES REQUESTED" in v.as_comment()


def test_verdict_comment_shows_pass():
    assert "Judge: PASS" in Verdict(True, 9, notes="good").as_comment()


@pytest.mark.parametrize("num,title,expected", [
    (17, "Derive recent form", "agent/17-derive-recent-form"),
    (3, "[STORY 1.2.2] 365Scores fixtures adapter",
     "agent/3-story-1-2-2-365scores-fixtures-adapter"),
    # long titles are truncated and never end on a dangling separator
    (4, "A" * 80, "agent/4-" + "a" * 40),
    (9, "", "agent/9"),
])
def test_branch_names_are_readable(num, title, expected):
    assert branch_name(num, title) == expected


# ── judging the same commit twice ────────────────────────────────────────────
def test_verdict_lookup_by_commit():
    """The leak that drained the budget: a PR the gate holds was re-judged every
    tick, same diff, a real Claude call each time. A verdict is now remembered
    against the commit it ruled on."""
    from agentloop.watchers import _judged_tag, _verdict_for

    sha = "abc123def456"
    passed = [{"body": f"**Judge: PASS**\n{_judged_tag(sha, True)}"}]
    failed = [{"body": f"**Judge: CHANGES**\n{_judged_tag(sha, False)}"}]

    assert _verdict_for(passed, sha) is True
    assert _verdict_for(failed, sha) is False
    # a new commit has no verdict yet, so it gets judged
    assert _verdict_for(passed, "0000000") is None
    assert _verdict_for([], sha) is None
    # no SHA available -> judge rather than silently skip
    assert _verdict_for(passed, "") is None


def test_the_prompt_survives_the_judge_flag_order():
    """`--allowedTools <tools...>` is VARIADIC, so a positional argument after it
    is swallowed as another tool name. invoke() appends the prompt as the final
    positional, so with --allowedTools last the judge ran with no prompt and the
    CLI answered "Input must be provided either through stdin or as a prompt
    argument when using --print". Every verdict came back unusable.
    """
    from agentloop.config import JUDGE

    VARIADIC = {"--allowedTools", "--allowed-tools",
                "--disallowedTools", "--disallowed-tools"}
    # invoke() appends the prompt as JUDGE + [prompt], so the prompt lands
    # immediately after JUDGE[-1]. No variadic flag may own that position.
    assert JUDGE[-1] not in VARIADIC, JUDGE
    assert JUDGE[-2] not in VARIADIC, JUDGE
    # and the tool allowlist is still actually being passed
    assert "--allowedTools" in JUDGE, JUDGE


def test_an_unusable_verdict_holds_instead_of_failing_the_pr(tmp_path):
    """A JUDGE THAT CANNOT RULE IS NOT A REJECTION. An unusable verdict used to
    fall through to `problems` as "the reviewer did not pass this PR", which put
    an agent on the branch three times and escalated it behind an empty
    "CHANGES REQUESTED - score 0/10" review. One bad flag order failed three
    green pull requests that way.
    """
    from unittest.mock import patch

    from agentloop.config import Config, Repo
    from agentloop.judge import Verdict
    from agentloop.watchers import _handle_pr
    from agentloop.worktree import Workspace

    ws = Workspace(tmp_path, "o/r")
    ws.logs.mkdir(parents=True)
    cfg, repo = Config(), Repo(slug="o/r", default_branch="main")
    pr = {"number": 93, "title": "t", "body": "Closes #4", "labels": [],
          "mergeable": "MERGEABLE", "headRefName": "agent/4-x", "headRefOid": "abc"}

    class _B:
        def allow(self): return True
        def record(self): raise AssertionError("must not spend budget on a non-verdict")
        def status(self): return "n/a"

    with patch("agentloop.gh.pr_checks_state", return_value="pass"), \
         patch("agentloop.gh.pr_review_comments", return_value=[]), \
         patch("agentloop.gh.pr_files", return_value=["lib/x.ts"]), \
         patch("agentloop.gh.pr_diff", return_value="diff"), \
         patch("agentloop.gh.pr_review") as review, \
         patch("agentloop.watchers._run_fixer") as fixer, \
         patch("agentloop.watchers.judge_pr",
               return_value=Verdict(False, error="CLI returned no usable verdict")):
        out = _handle_pr(cfg, repo, ws, _B(), pr)

    fixer.assert_not_called()
    review.assert_not_called()
    assert any("could not rule" in line for line in out), out
