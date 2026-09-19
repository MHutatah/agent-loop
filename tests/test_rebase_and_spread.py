"""The loop rebases, and it does not stack agents in one area.

Both of these are about the same night. On 2026-09-19 four agents ran in one
epic, nine of eleven merged pull requests needed a hand rebase, two were thrown
away and re-queued, and two modules got built twice under different names. The
cap was not the problem: four agents in four epics cost nothing.
"""
import subprocess
from unittest.mock import patch

from agentloop.config import Config, Repo
from agentloop.watchers import _area_of, _spread_by_area, issue_watcher
from agentloop.worktree import Workspace


# ── the rebase ───────────────────────────────────────────────────────────────
def _origin_clone_tree(tmp_path):
    """A bare origin, a clone, and a worktree one commit ahead of main."""
    def run(*a, cwd):
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)

    origin = tmp_path / "origin.git"
    origin.mkdir(parents=True)
    run("init", "-q", "--bare", "-b", "main", cwd=origin)

    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    run("init", "-q", "-b", "main", cwd=seed)
    run("config", "user.email", "t@example.com", cwd=seed)
    run("config", "user.name", "t", cwd=seed)
    (seed / "base.txt").write_text("base\n", encoding="utf-8")
    (seed / "shared.txt").write_text("one\n", encoding="utf-8")
    run("add", "-A", cwd=seed)
    run("commit", "-qm", "seed", cwd=seed)
    run("remote", "add", "origin", str(origin), cwd=seed)
    run("push", "-q", "origin", "main", cwd=seed)

    ws = Workspace(tmp_path / "work", "o/r")
    ws.clone.parent.mkdir(parents=True, exist_ok=True)
    run("clone", "-q", str(origin), str(ws.clone), cwd=tmp_path)
    run("config", "user.email", "agent@example.com", cwd=ws.clone)
    run("config", "user.name", "agent", cwd=ws.clone)
    ws.trees.mkdir(parents=True, exist_ok=True)

    tree = ws.trees / "issue-1"
    run("worktree", "add", "-q", "-B", "agent/1-x", str(tree), "origin/main", cwd=ws.clone)
    return ws, tree, seed, origin, run


def test_a_branch_already_on_the_base_is_left_alone(tmp_path):
    ws, tree, _seed, _origin, run = _origin_clone_tree(tmp_path)
    (tree / "mine.txt").write_text("mine\n", encoding="utf-8")
    run("add", "-A", cwd=tree)
    run("commit", "-qm", "work", cwd=tree)
    assert ws.rebase_onto(tree, "main") == "current"


def test_a_branch_merely_behind_is_rebased(tmp_path):
    """The case that mattered: nine of eleven were this, not CONFLICTING."""
    ws, tree, seed, _origin, run = _origin_clone_tree(tmp_path)
    (tree / "mine.txt").write_text("mine\n", encoding="utf-8")
    run("add", "-A", cwd=tree)
    run("commit", "-qm", "work", cwd=tree)

    # main moves, in a file this branch never touched
    (seed / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    run("add", "-A", cwd=seed)
    run("commit", "-qm", "their work", cwd=seed)
    run("push", "-q", "origin", "main", cwd=seed)

    assert ws.rebase_onto(tree, "main") == "rebased"
    assert (tree / "theirs.txt").exists()          # the base's work is here
    assert (tree / "mine.txt").exists()            # and so is ours
    assert ws.ahead_of(tree, "main") == 1          # replayed, not merged
    assert not ws.has_changes(tree)                # and the tree is clean


def test_a_real_collision_names_its_files_and_leaves_no_rebase_in_progress(tmp_path):
    """The abort is the load-bearing half. A worktree stopped mid-rebase reads
    as dirty to every later tick, so the collector would commit conflict
    markers as though they were the agent's work."""
    ws, tree, seed, _origin, run = _origin_clone_tree(tmp_path)
    (tree / "shared.txt").write_text("ours\n", encoding="utf-8")
    run("add", "-A", cwd=tree)
    run("commit", "-qm", "our edit", cwd=tree)

    (seed / "shared.txt").write_text("theirs\n", encoding="utf-8")
    run("add", "-A", cwd=seed)
    run("commit", "-qm", "their edit", cwd=seed)
    run("push", "-q", "origin", "main", cwd=seed)

    result = ws.rebase_onto(tree, "main")
    assert result.startswith("conflicts:")
    assert "shared.txt" in result
    assert not ws._rebase_in_progress(tree)
    assert not ws.has_changes(tree)
    # and our commit is still ours, unreplayed
    assert (tree / "shared.txt").read_text(encoding="utf-8") == "ours\n"


# ── the spread ───────────────────────────────────────────────────────────────
def _issue(number, epic):
    labels = [{"name": "type:story"}]
    if epic:
        labels.append({"name": f"epic:{epic}"})
    return {"number": number, "title": f"story {number}", "labels": labels}


def test_four_slots_take_four_areas_rather_than_four_of_one(tmp_path):
    ws = Workspace(tmp_path, "o/r")
    issues = [_issue(1, "calendar"), _issue(2, "calendar"), _issue(3, "calendar"),
              _issue(4, "calendar"), _issue(5, "library"), _issue(6, "path"),
              _issue(7, "platform")]
    picked = _spread_by_area(issues, 4, ws, [])
    assert len(picked) == 4
    assert sorted(_area_of(i) for i in picked) == [
        "epic:calendar", "epic:library", "epic:path", "epic:platform"]


def test_an_area_already_running_is_not_picked_again(tmp_path):
    """Otherwise a tick that starts one agent undoes the previous tick's spread."""
    ws = Workspace(tmp_path, "o/r")
    issues = [_issue(1, "calendar"), _issue(2, "calendar"), _issue(5, "library")]
    picked = _spread_by_area(issues, 1, ws, [1])       # #1 is already working
    assert [i["number"] for i in picked] == [5]


def test_one_area_is_still_worked_when_it_is_all_there_is(tmp_path):
    """A soft preference. Idling with work available is worse than a rebase: an
    unmerged PR does not delay itself, it stops the next wave being queued."""
    ws = Workspace(tmp_path, "o/r")
    issues = [_issue(1, "calendar"), _issue(2, "calendar"), _issue(3, "calendar")]
    picked = _spread_by_area(issues, 2, ws, [])
    assert [i["number"] for i in picked] == [1, 2]


def test_an_issue_with_no_epic_label_is_still_eligible(tmp_path):
    ws = Workspace(tmp_path, "o/r")
    picked = _spread_by_area([_issue(9, None)], 2, ws, [])
    assert [i["number"] for i in picked] == [9]


def test_the_watcher_itself_spreads_and_not_only_the_helper(tmp_path):
    """The three tests above pass with the call site reverted to issues[:n],
    because they exercise the helper directly. This one fails: it drives
    issue_watcher and asserts WHICH issues got an agent.

    That is the same weakness found three times in ipa-community the same week,
    where a guard asserted on source text and a commented-out call satisfied it.
    A helper nothing calls is a helper that does nothing.
    """
    ready = [_issue(1, "calendar"), _issue(2, "calendar"), _issue(3, "library")]
    started = []

    cfg = Config(repos=[Repo(slug="o/r")], max_concurrent_agents=2,
                 max_concurrent_per_repo=2)
    with patch("agentloop.gh.ready_issues", return_value=ready), \
         patch("agentloop.gh.issues_with_open_pr", return_value=set()), \
         patch("agentloop.gh.add_label"), \
         patch("agentloop.tmux.live_windows", return_value=[]), \
         patch("agentloop.tmux.spawn",
               side_effect=lambda key, n, *a, **k: started.append(n)), \
         patch("agentloop.worktree.Workspace.ensure_clone"), \
         patch("agentloop.worktree.Workspace.create",
               side_effect=lambda n, t, b="main", dry=False: (tmp_path, "agent/x")), \
         patch("agentloop.watchers._collect_finished", return_value=[]), \
         patch("agentloop.watchers._reap", return_value=[]):
        issue_watcher(cfg, cfg.repos[0], tmp_path)

    assert sorted(started) == [1, 3], (
        "two slots should have gone to two different epics, not both to calendar")


def test_a_rebased_pr_is_pushed_and_the_tick_stops_there(tmp_path):
    """The early return is the subtle half, so it gets its own check.

    After a rebase the head moved: CI has to run again and any verdict against
    the old sha is about code that no longer exists. Judging in the same tick
    would spend a real Claude call on a diff that has been replaced.
    """
    from agentloop.watchers import _handle_pr

    pr = {"number": 7, "title": "a story", "body": "", "mergeable": "MERGEABLE",
          "headRefName": "agent/7-x", "headRefOid": "old" * 13}
    cfg = Config(repos=[Repo(slug="o/r")])
    ws = Workspace(tmp_path, "o/r")
    (ws.trees / "issue-7").mkdir(parents=True)

    judged = []
    with patch("agentloop.gh.pr_checks_state", return_value="pass"), \
         patch("agentloop.gh.pr_review_comments", return_value=[]), \
         patch("agentloop.gh.pr_files", return_value=["lib/x.ts"]), \
         patch("agentloop.worktree.Workspace.rebase_onto", return_value="rebased"), \
         patch("agentloop.worktree.Workspace.push") as pushed, \
         patch("agentloop.judge.judge_pr", side_effect=lambda *a, **k: judged.append(1)):
        out = _handle_pr(cfg, cfg.repos[0], ws, None, pr)

    assert pushed.called, "a rebased branch has to be pushed or the rebase is local only"
    assert judged == [], "nothing may be judged against the sha the rebase replaced"
    assert any("rebased onto main" in line for line in out)
