"""The second opinion, and the two rules that make it safe to have.

It is consulted only on changes where being wrong is expensive, and its ABSENCE
must never change an outcome: `astra` is unreachable on a ChatGPT account today,
so a second opinion that blocked merges when it could not be obtained would be
worse than not having one.
"""
from unittest.mock import patch

from agentloop import second_voice
from agentloop.config import Config, Repo
from agentloop.judge import Verdict
from agentloop.watchers import _handle_pr
from agentloop.worktree import Workspace


def _pr(**kw):
    base = {"number": 92, "title": "t", "body": "Closes #1", "labels": [],
            "mergeable": "MERGEABLE", "headRefName": "agent/1-x", "headRefOid": "abc"}
    base.update(kw)
    return base


class _Budget:
    def allow(self): return True
    def record(self): pass
    def status(self): return "n/a"


def test_only_expensive_changes_are_worth_a_second_opinion():
    cfg = Config()
    assert second_voice.is_critical(["lib/roles.ts"], set(), cfg) is True
    assert second_voice.is_critical(["lib/db.ts"], set(), cfg) is True
    assert second_voice.is_critical(["proxy.ts"], set(), cfg) is True
    assert second_voice.is_critical(["app/glossary/page.tsx"],
                                    {"concern:privacy"}, cfg) is True
    # ordinary work must not pay for it
    assert second_voice.is_critical(["app/glossary/page.tsx"], set(), cfg) is False
    assert second_voice.is_critical(["tests/x.test.mjs"], {"size:S"}, cfg) is False


def test_an_unavailable_opinion_changes_nothing(tmp_path):
    """astra is not reachable on a ChatGPT account, so this is the common case,
    not the edge case. None means "no opinion", never "fine" and never "stop"."""
    ws = Workspace(tmp_path, "o/r")
    ws.logs.mkdir(parents=True)
    cfg, repo = Config(), Repo(slug="o/r", default_branch="main")
    with patch("agentloop.gh.pr_checks_state", return_value="pass"), \
         patch("agentloop.gh.pr_review_comments", return_value=[]), \
         patch("agentloop.gh.pr_files", return_value=["lib/roles.ts"]), \
         patch("agentloop.gh.pr_diff", return_value="diff"), \
         patch("agentloop.gh.pr_review"), patch("agentloop.gh.comment") as comment, \
         patch("agentloop.gh.add_label") as label, \
         patch("agentloop.watchers.judge_pr",
               return_value=Verdict(True, score=8, notes="ok")), \
         patch("agentloop.second_voice.consult", return_value=None):
        out = _handle_pr(cfg, repo, ws, _Budget(), _pr())
    comment.assert_not_called()
    assert not any(c.args[2] == "agent:needs-human" for c in label.call_args_list)
    assert any("held" in line for line in out), out   # held by auto_merge=False, not by us


def test_a_dissent_holds_the_pr_for_a_human(tmp_path):
    ws = Workspace(tmp_path, "o/r")
    ws.logs.mkdir(parents=True)
    cfg, repo = Config(), Repo(slug="o/r", default_branch="main", auto_merge=True)
    op = second_voice.Opinion(concern=True, summary="added_by stores the phone number",
                              points=["lib/db.ts:112 writes identity, not rateKey"])
    with patch("agentloop.gh.pr_checks_state", return_value="pass"), \
         patch("agentloop.gh.pr_review_comments", return_value=[]), \
         patch("agentloop.gh.pr_files", return_value=["lib/db.ts"]), \
         patch("agentloop.gh.pr_diff", return_value="diff"), \
         patch("agentloop.gh.pr_review"), patch("agentloop.gh.comment") as comment, \
         patch("agentloop.gh.add_label") as label, \
         patch("agentloop.gh.merge_pr") as merge, \
         patch("agentloop.watchers.judge_pr",
               return_value=Verdict(True, score=9, notes="ok")), \
         patch("agentloop.second_voice.consult", return_value=op):
        out = _handle_pr(cfg, repo, ws, _Budget(), _pr())
    merge.assert_not_called()          # a dissent beats a passing judge
    assert any(c.args[2] == "agent:needs-human" for c in label.call_args_list)
    assert "CONCERN" in comment.call_args.args[2]
    assert any("second voice raised a concern" in line for line in out), out


def test_ordinary_work_is_never_delayed_by_it(tmp_path):
    ws = Workspace(tmp_path, "o/r")
    ws.logs.mkdir(parents=True)
    cfg, repo = Config(), Repo(slug="o/r", default_branch="main")
    with patch("agentloop.gh.pr_checks_state", return_value="pass"), \
         patch("agentloop.gh.pr_review_comments", return_value=[]), \
         patch("agentloop.gh.pr_files", return_value=["app/glossary/page.tsx"]), \
         patch("agentloop.gh.pr_diff", return_value="diff"), \
         patch("agentloop.gh.pr_review"), patch("agentloop.gh.comment"), \
         patch("agentloop.watchers.judge_pr",
               return_value=Verdict(True, score=8, notes="ok")), \
         patch("agentloop.second_voice.consult") as consult:
        _handle_pr(cfg, repo, ws, _Budget(), _pr())
    consult.assert_not_called()


def test_a_reply_is_read_from_the_last_json_not_the_first():
    """codex prints a transcript that includes the prompt, and the prompt itself
    contains a JSON shape with a "concern" key. Taking the first match would
    parse the instructions back as the answer."""
    from agentloop.runner import AgentResult
    transcript = ('user\nReply with ONLY a JSON object:\n'
                  '{"concern": <true|false>, "summary": "one sentence"}\n'
                  'assistant\n{"concern": true, "summary": "real finding", "points": ["x"]}')
    with patch("agentloop.second_voice.invoke",
               return_value=AgentResult(True, text=transcript)):
        op = second_voice.consult({"number": 1, "title": "t", "body": ""}, "diff")
    assert op is not None and op.concern is True
    assert op.summary == "real finding"
