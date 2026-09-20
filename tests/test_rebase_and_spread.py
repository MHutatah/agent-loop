"""The loop rebases, and it does not stack agents in one area.

Both of these are about the same night. On 2026-09-19 four agents ran in one
epic, nine of eleven merged pull requests needed a hand rebase, two were thrown
away and re-queued, and two modules got built twice under different names. The
cap was not the problem: four agents in four epics cost nothing.
"""
import subprocess
from unittest.mock import patch

from agentloop.config import Config, Repo
from agentloop.watchers import (
    _area_of,
    _queue_ready,
    _spread_by_area,
    issue_watcher,
)
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
         patch("agentloop.gh.backlog", return_value=[]), \
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


def test_a_merged_pr_does_not_get_a_fixer(tmp_path):
    """#128 was merged while a pass was mid-flight, and the fixer started anyway
    on the strength of the merge note being read as a reviewer comment. It then
    died on `couldn't find remote ref`, having already been spawned.

    A snapshot list plus a slow tick means this window always exists, so the
    check belongs immediately before the spend."""
    from agentloop.watchers import _run_fixer

    out = []
    cfg = Config(repos=[Repo(slug="o/r")])
    ws = Workspace(tmp_path, "o/r")
    pr = {"number": 128, "headRefName": "agent/79-x"}
    issue = {"number": 79, "title": "t", "body": "b"}

    with patch("agentloop.gh.pr_state", return_value="MERGED"), \
         patch("agentloop.gh.remove_label") as unlabel, \
         patch("agentloop.tmux.spawn") as spawned, \
         patch("agentloop.worktree.Workspace.attach") as attached:
        _run_fixer(cfg, cfg.repos[0], ws, 128, pr, issue, ["CI is failing"], out)

    assert not spawned.called, "no agent session may be spent on a merged PR"
    assert not attached.called
    assert unlabel.called, "and the agent:pr label has to come off, or it is seen every tick"
    assert any("no longer open" in line for line in out)


# ── the dependency queue, which replaced the overseer ────────────────────────
def _backlog_issue(number, epic, depends, kind="type:story", extra=()):
    labels = [{"name": kind}] + [{"name": n} for n in extra]
    if epic:
        labels.append({"name": f"epic:{epic}"})
    dep = ", ".join(f"#{d}" for d in depends) if depends else "nothing"
    return {"number": number, "title": f"story {number}", "labels": labels,
            "body": f"**Epic:** X | **Size:** M | **Depends on:** {dep}\n\nbody"}


def test_an_issue_whose_dependencies_are_closed_gets_queued(tmp_path):
    """The overseer's one good job, as a parse. #7 depends on #3, which is not
    in the open backlog, so it has closed and #7 is startable."""
    backlog = [_backlog_issue(7, "calendar", [3])]
    labelled = []
    cfg = Config(repos=[Repo(slug="o/r")])
    with patch("agentloop.gh.backlog", return_value=backlog), \
         patch("agentloop.gh.add_label",
               side_effect=lambda repo, n, label, dry=False: labelled.append((n, label))):
        out = _queue_ready(cfg, cfg.repos[0], 3)
    assert labelled == [(7, "agent:ready")]
    assert any("#7 queued" in line for line in out)


def test_an_issue_still_blocked_is_left_alone(tmp_path):
    """#8 depends on #7, and #7 is open, so #8 is not startable. Queueing it
    would put an agent on work whose foundation does not exist yet."""
    backlog = [_backlog_issue(7, "calendar", []), _backlog_issue(8, "calendar", [7])]
    labelled = []
    cfg = Config(repos=[Repo(slug="o/r")])
    with patch("agentloop.gh.backlog", return_value=backlog), \
         patch("agentloop.gh.add_label",
               side_effect=lambda repo, n, label, dry=False: labelled.append(n)):
        _queue_ready(cfg, cfg.repos[0], 5)
    assert labelled == [7], "only the unblocked one"


def test_it_stops_once_enough_are_in_hand(tmp_path):
    """Labelling the whole backlog would make agent:ready meaningless as a
    statement about what is next, and hand the spread a pile, not a queue."""
    backlog = [_backlog_issue(n, "calendar", []) for n in range(10, 20)]
    labelled = []
    cfg = Config(repos=[Repo(slug="o/r")])
    with patch("agentloop.gh.backlog", return_value=backlog), \
         patch("agentloop.gh.add_label",
               side_effect=lambda repo, n, label, dry=False: labelled.append(n)):
        _queue_ready(cfg, cfg.repos[0], 3)
    assert labelled == [10, 11, 12], "lowest first, and it stops at three"


def test_issues_already_under_the_loop_or_escalated_are_never_requeued(tmp_path):
    """Re-adding agent:ready to an escalated issue is how a confused agent got
    put straight back on the thing somebody was just asked to look at."""
    backlog = [
        _backlog_issue(1, "a", [], extra=["agent:needs-human"]),
        _backlog_issue(2, "a", [], extra=["agent:working"]),
        _backlog_issue(3, "a", [], extra=["agent:ready"]),
        _backlog_issue(4, "a", [], extra=["agent:pr"]),
        _backlog_issue(5, "a", [], extra=["agent:stop"]),
        _backlog_issue(6, "a", []),
    ]
    labelled = []
    cfg = Config(repos=[Repo(slug="o/r")])
    with patch("agentloop.gh.backlog", return_value=backlog), \
         patch("agentloop.gh.add_label",
               side_effect=lambda repo, n, label, dry=False: labelled.append(n)):
        _queue_ready(cfg, cfg.repos[0], 9)
    assert labelled == [6]


def test_an_epic_or_a_question_is_not_work(tmp_path):
    backlog = [_backlog_issue(1, "a", [], kind="type:epic"),
               _backlog_issue(2, "a", [], kind="type:question"),
               _backlog_issue(3, "a", [], kind="type:spike")]
    labelled = []
    cfg = Config(repos=[Repo(slug="o/r")])
    with patch("agentloop.gh.backlog", return_value=backlog), \
         patch("agentloop.gh.add_label",
               side_effect=lambda repo, n, label, dry=False: labelled.append(n)):
        _queue_ready(cfg, cfg.repos[0], 9)
    assert labelled == [3], "stories and spikes are work; epics and questions are not"


def test_the_watcher_itself_queues_and_not_only_the_helper(tmp_path):
    """The five queue tests above call _queue_ready directly, so they pass with
    the call site removed. This one drives issue_watcher, which is the only way
    to catch a loop that has stopped queueing and reports a finished backlog."""
    backlog = [_backlog_issue(42, "library", [])]
    labelled = []

    cfg = Config(repos=[Repo(slug="o/r")])
    with patch("agentloop.gh.backlog", return_value=backlog), \
         patch("agentloop.gh.ready_issues", return_value=[]), \
         patch("agentloop.gh.issues_with_open_pr", return_value=set()), \
         patch("agentloop.gh.add_label",
               side_effect=lambda repo, n, label, dry=False: labelled.append((n, label))), \
         patch("agentloop.tmux.live_windows", return_value=[]), \
         patch("agentloop.watchers._collect_finished", return_value=[]), \
         patch("agentloop.watchers._reap", return_value=[]):
        out = issue_watcher(cfg, cfg.repos[0], tmp_path)

    assert (42, "agent:ready") in labelled, (
        "the watcher has to queue, or the loop starves while reporting no ready issues")
    assert any("#42 queued" in line for line in out)


def test_an_issue_that_already_has_a_pull_request_is_not_queued(tmp_path):
    """It labelled ipa-community #9 ready while #9 already had #131 open. The
    start loop filters the same set, so nothing ran twice, but a ready label on
    work that is already written is a lie about what is next."""
    backlog = [_backlog_issue(9, "library", []), _backlog_issue(10, "library", [])]
    labelled = []
    cfg = Config(repos=[Repo(slug="o/r")])
    with patch("agentloop.gh.backlog", return_value=backlog), \
         patch("agentloop.gh.add_label",
               side_effect=lambda repo, n, label, dry=False: labelled.append(n)):
        _queue_ready(cfg, cfg.repos[0], 5, {9})
    assert labelled == [10]
