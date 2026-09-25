"""The auto-merge gate — the code that lands changes while nobody is watching.

Every test here is a thing that must NOT merge itself. The default answer is no;
a pass requires every condition to hold at once.
"""
import pytest

from agentloop.config import Config, Repo, touches_guarded_path
from agentloop.gate import decide
from agentloop.judge import Verdict

CFG = Config(repos=[Repo("x/y", auto_merge=True)])
GOOD = Verdict(passed=True, score=9, notes="looks right")


def gate(**over):
    kw = dict(repo_auto_merge=True, checks="pass", verdict=GOOD,
              changed_files=["arabfootball/derive/form.py", "tests/test_form.py"],
              mergeable="MERGEABLE", cfg=CFG, human_requested_changes=False)
    kw.update(over)
    return decide(**kw)


def test_merges_when_everything_is_green():
    g = gate()
    assert g.merge and "judge passed" in g.reason


@pytest.mark.parametrize("checks,why", [
    ("fail", "CI is failing"),
    ("pending", "CI still running"),
    ("none", "no CI checks"),
])
def test_ci_must_be_green(checks, why):
    g = gate(checks=checks)
    assert not g.merge and why in g.reason


def test_judge_must_pass():
    assert not gate(verdict=Verdict(passed=False, blocking=["no test"])).merge


def test_unparseable_verdict_is_not_a_pass():
    """A judge that errored must never be read as approval."""
    g = gate(verdict=Verdict(passed=False, error="judge returned no usable verdict"))
    assert not g.merge and "no usable judge verdict" in g.reason


def test_human_comment_pauses_auto_merge():
    g = gate(human_requested_changes=True)
    assert not g.merge and "human" in g.reason


def test_conflicts_block():
    assert not gate(mergeable="CONFLICTING").merge


def test_auto_merge_can_be_disabled_per_repo():
    assert not gate(repo_auto_merge=False).merge


@pytest.mark.parametrize("path", [
    ".github/workflows/ci.yml",     # could disable the CI that gates merges
    "LICENSE",
    "deploy/docker-compose.yml",
    "agentloop/config.py",          # could widen the agent's own permissions
])
def test_guarded_paths_never_auto_merge(path):
    """An agent must not be able to weaken its own guardrails unattended."""
    g = gate(changed_files=["src/ok.py", path])
    assert not g.merge and "guarded path" in g.reason


def test_touches_guarded_path_is_prefix_aware():
    assert touches_guarded_path([".github/workflows/deep/x.yml"], CFG)
    assert not touches_guarded_path(["docs/github-workflows-guide.md"], CFG)


def test_a_guarded_file_does_not_guard_its_siblings():
    """`.env` is guarded; `.env.example` is a committed template with nothing
    secret in it. A bare startswith matched both, so ipa-community #23 was
    refused before its push on 2026-09-21 and its pull request #150 sat open,
    unlabelled and therefore unjudgeable, for two days.
    """
    assert touches_guarded_path([".env"], CFG)
    assert touches_guarded_path(["secrets/key.pem"], CFG)
    assert not touches_guarded_path([".env.example"], CFG)
    assert not touches_guarded_path([".environment/notes.md"], CFG)
    assert not touches_guarded_path(["deployment-notes.md"], CFG)


def test_unreadable_ci_is_refused_and_named_distinctly():
    """A broken CI query once masqueraded as 'no CI configured', so green PRs
    were refused for a reason that wasn't true. Both still refuse — but only one
    of them means someone should go fix the tooling."""
    g = gate(checks="unknown")
    assert not g.merge
    assert "could not read CI" in g.reason
    assert "no CI checks configured" not in g.reason
