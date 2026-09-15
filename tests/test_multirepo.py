"""Working on several projects at once, and the three ways one project used to
corrupt another or lose work outright.

Every test here corresponds to a defect that was in the shipped code, and each
one fails if its fix is reverted. That matters more than coverage: all four
failures were silent, and three of them looked exactly like an idle loop.
"""
import subprocess
from pathlib import Path
from unittest.mock import patch

from agentloop import gh, tmux
from agentloop.config import Config, touches_guarded_path
from agentloop.worktree import Workspace, repo_key


# ── the catastrophic one: a shared clone ─────────────────────────────────────
def test_two_repos_never_share_a_clone_or_a_worktree_directory(tmp_path):
    """The clone was `<root>/repo` for every repository.

    `Workspace` took `repo_slug` and used it only to build the clone URL, so the
    second repo's `ensure_clone` found a `.git` already present, fetched the
    FIRST repo's origin, cut worktrees of the first repo's code for the second
    repo's issue numbers, and opened pull requests on the second repo containing
    the first one's work. agentloop.toml says "add more repos by repeating the
    block", so the config file invited it.
    """
    a = Workspace(tmp_path, "MHutatah/ipa-community")
    b = Workspace(tmp_path, "MHutatah/nolog")
    assert a.clone != b.clone
    assert a.trees != b.trees
    assert a.logs != b.logs
    # and the same issue number in each must not resolve to one directory
    assert (a.trees / "issue-12") != (b.trees / "issue-12")


def test_repo_key_is_filesystem_safe_and_distinct_per_owner():
    assert repo_key("MHutatah/nolog") == "MHutatah__nolog"
    # same repo name, different owners, must not collide
    assert repo_key("a/thing") != repo_key("b/thing")
    assert "/" not in repo_key("owner/repo")
    assert repo_key("") == "repo"


def test_a_clone_of_the_wrong_repository_is_refused(tmp_path):
    """Belt and braces on the bug above: even if a directory is mis-seeded by
    hand or by an older version, the loop must not push into it."""
    ws = Workspace(tmp_path, "MHutatah/ipa-community")
    ws.clone.mkdir(parents=True)
    (ws.clone / ".git").mkdir()
    with patch("agentloop.worktree.git", return_value="https://github.com/MHutatah/nolog.git"):
        try:
            ws.ensure_clone()
        except RuntimeError as exc:
            assert "nolog" in str(exc) and "Refusing" in str(exc)
        else:
            raise AssertionError("reused a clone of a different repository")


# ── tmux windows were keyed on the issue number alone ───────────────────────
def test_window_names_are_unique_per_repo_for_the_same_issue_number():
    """tmux permits duplicate window names, so nothing errored: `is_running`
    matched whichever window came first and `exit_code` read the other one's
    status file. Two digits of shared issue number crossed the wires."""
    a = tmux.window_name(repo_key("MHutatah/ipa-community"), 12)
    b = tmux.window_name(repo_key("MHutatah/nolog"), 12)
    assert a != b
    # tmux target syntax uses ':' and '.', so neither may appear in a name
    assert ":" not in a and "." not in a


def test_spawn_runs_end_to_end_with_the_keyed_signature(tmp_path):
    """THIS ONE ESCAPED TO PRODUCTION. Re-keying the tmux helpers on (repo,
    issue) missed `spawn`'s own internal `kill(issue)` call, because a grep for
    `window_name(issue)` finds the naming and not the calls. `spawn` had no test
    at all, so the first evidence was a TypeError on the first real tick against
    a live repository:

        TypeError: kill() missing 1 required positional argument: 'issue'

    Patching only `_tmux` exercises every line of `spawn` for real, which is the
    cheapest thing that would have caught it.
    """
    calls = []

    def fake_tmux(args, **kw):
        calls.append(args)
        return (0, "")

    with patch("agentloop.tmux._tmux", side_effect=fake_tmux):
        rc = tmux.spawn("o__r", 12, ["echo", "hi"], "a prompt",
                        cwd=tmp_path, log_dir=tmp_path)

    assert rc == tmp_path / "issue-12.rc"
    assert (tmp_path / "issue-12.prompt").read_text(encoding="utf-8") == "a prompt"
    # the window it opened carries the repo key, not a bare issue number
    new_window = [a for a in calls if a and a[0] == "new-window"]
    assert new_window and "o__r--12" in new_window[0]
    assert "issue-12" not in new_window[0]


def test_live_windows_reports_repo_and_issue_so_capacity_cannot_leak():
    """As a bare list of issue numbers, two repos each running their own #12
    counted as one live agent, so the concurrency cap leaked a slot per
    collision."""
    listing = ("MHutatah__ipa-community--12 0\n"
               "MHutatah__nolog--12 0\n"
               "MHutatah__nolog--7 1\n"          # dead, must not count
               "dashboard 0\n")                  # not an agent window
    with patch("agentloop.tmux._tmux", return_value=(0, listing)):
        live = tmux.live_windows()
        assert sorted(live) == [("MHutatah__ipa-community", 12),
                                ("MHutatah__nolog", 12)]
        assert len(live) == 2
        assert tmux.live_for("MHutatah__nolog") == [12]


# ── committed work must not read as "the agent did nothing" ─────────────────
def _repo(path: Path) -> None:
    def run(*a):
        subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)

    path.mkdir(parents=True, exist_ok=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "seed")
    # a local 'origin/main' so ahead_of has something to compare against
    run("update-ref", "refs/remotes/origin/main", "HEAD")


def test_committed_work_is_visible_after_the_tree_goes_clean(tmp_path):
    """THE MISDIAGNOSIS BUG. The collector committed, pushed, then failed to open
    the PR on a network blip. The next tick asked `has_changes`, which is
    `git status --porcelain` and is empty once the work is committed, concluded
    "the agent produced no changes — the issue may be under-specified", labelled
    the issue for a human and deleted the worktree while the branch sat pushed
    on GitHub. The author was told their spec was vague about finished work.
    """
    tree = tmp_path / "t"
    _repo(tree)
    ws = Workspace(tmp_path / "ws", "o/r")

    (tree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    assert ws.has_changes(tree) is True
    assert ws.ahead_of(tree, "main") == 0

    ws.commit_all(tree, "implement it")
    assert ws.has_changes(tree) is False        # what fooled the old collector
    assert ws.ahead_of(tree, "main") == 1       # the question it should have asked


def test_changed_paths_sees_committed_and_uncommitted_work(tmp_path):
    tree = tmp_path / "t2"
    _repo(tree)
    ws = Workspace(tmp_path / "ws2", "o/r")
    (tree / "a.py").write_text("a\n", encoding="utf-8")
    ws.commit_all(tree, "a")
    (tree / "b.py").write_text("b\n", encoding="utf-8")
    assert ws.changed_paths(tree, "main") == ["a.py", "b.py"]


# ── containment has to happen before the push ───────────────────────────────
def test_a_secret_written_by_the_agent_is_caught_before_the_push(tmp_path):
    """gate.py checks guarded paths when deciding whether to MERGE, which is too
    late to matter: the branch is on GitHub by then, and refusing the merge does
    not unpublish a credential. `git add -A` means anything the agent wrote
    while testing goes with it.
    """
    tree = tmp_path / "t3"
    _repo(tree)
    ws = Workspace(tmp_path / "ws3", "o/r")
    cfg = Config()

    (tree / ".env").write_text("ANTHROPIC_API_KEY=sk-live\n", encoding="utf-8")
    (tree / "legit.py").write_text("ok\n", encoding="utf-8")
    ws.commit_all(tree, "work plus a stray secret")

    changed = ws.changed_paths(tree, "main")
    assert ".env" in changed
    assert touches_guarded_path(changed, cfg) == [".env"]


# ── the lost issue: nothing reconciled GitHub against the box ───────────────
def test_an_issue_stranded_by_a_reboot_is_released(tmp_path):
    """THE WORST OF THE FOUR, because it is indistinguishable from an idle loop.

    `spawn` deletes the `.rc` status file before starting the agent and writes it
    on completion. The collector only ever looked at issues with a `.rc` file or
    a live tmux window, so killing tmux between those two moments left an issue
    with neither: never collected, never released, `agent:working` on it
    forever, and `ready_issues` excludes that label so it could never be picked
    up again. The work stopped existing and nothing reported anything.
    """
    from agentloop.config import LABEL_NEEDS_HUMAN, LABEL_WIP, Repo
    from agentloop.watchers import _reap

    ws = Workspace(tmp_path, "o/r")
    ws.logs.mkdir(parents=True)
    cfg, repo = Config(), Repo(slug="o/r", default_branch="main")

    with patch("agentloop.tmux.available", return_value=True), \
         patch("agentloop.tmux.is_running", return_value=False), \
         patch("agentloop.tmux.kill"), \
         patch("agentloop.gh.issues_with_label", return_value=[{"number": 5}]), \
         patch("agentloop.gh.remove_label") as unlabel, \
         patch("agentloop.gh.add_label") as label, \
         patch("agentloop.gh.comment"):
        out = _reap(cfg, repo, ws)

    assert any("stranded" in line for line in out), out
    unlabel.assert_called_once_with("o/r", 5, LABEL_WIP)
    # Released for a retry, NOT escalated: a reboot is our failure, not a
    # statement about the issue, and needs-human would require a human to clear
    # a label for a machine's crash.
    assert LABEL_NEEDS_HUMAN not in [c.args[2] for c in label.call_args_list]


def test_the_reaper_leaves_finished_and_in_flight_work_alone(tmp_path):
    """It must only touch issues nothing owns. Reaping one the collector is
    about to turn into a PR would delete the worktree under it."""
    from agentloop.config import Repo
    from agentloop.watchers import _reap

    ws = Workspace(tmp_path, "o/r")
    ws.logs.mkdir(parents=True)
    (ws.logs / "issue-9.rc").write_text("0", encoding="utf-8")   # finished
    cfg, repo = Config(), Repo(slug="o/r", default_branch="main")

    with patch("agentloop.tmux.available", return_value=True), \
         patch("agentloop.tmux.is_running", side_effect=lambda k, n: n == 8), \
         patch("agentloop.gh.issues_with_label",
               return_value=[{"number": 8}, {"number": 9}]), \
         patch("agentloop.gh.remove_label") as unlabel:
        out = _reap(cfg, repo, ws)

    assert out == []                    # 8 is running, 9 is collectable
    unlabel.assert_not_called()


# ── finished work must leave the queue ──────────────────────────────────────
def _issue(number, *labels):
    return {"number": number, "title": "t", "body": "",
            "labels": [{"name": nm} for nm in labels]}


def test_an_escalated_issue_is_not_picked_straight_back_up():
    """OBSERVED LIVE. Nothing removes `agent:ready`, and `ready_issues` excluded
    only `agent:working` and `agent:stop`, so an issue handed back to a human
    kept its ready label and was re-selected on the very next tick: a fresh
    agent on the issue somebody had just been asked to look at.

    The attempt cap does not help. `watchers._attempts` counts comments on a
    PULL REQUEST, so an issue that fails before producing one has no cap at all
    and could loop until the quota ran out.
    """
    from agentloop.config import LABEL_NEEDS_HUMAN, LABEL_READY, LABEL_STOP, LABEL_WIP

    rows = [_issue(1, LABEL_READY),
            _issue(2, LABEL_READY, LABEL_WIP),
            _issue(3, LABEL_READY, LABEL_STOP),
            _issue(4, LABEL_READY, LABEL_NEEDS_HUMAN)]
    with patch("agentloop.gh._json", return_value=rows):
        picked = [i["number"] for i in gh.ready_issues(
            "o/r", LABEL_READY, LABEL_WIP, LABEL_STOP, LABEL_NEEDS_HUMAN)]
    assert picked == [1], picked


def test_opening_a_pull_request_takes_the_issue_out_of_the_queue(tmp_path):
    """THE ONE THAT ACTUALLY HAPPENED. The tick that opened PR #88 started a
    second agent on the same issue in the same pass, because collection removed
    `agent:working` and left `agent:ready` on. It would have kept doing that
    until the issue closed.

    `auto_merge = true` hid it: the merge closed the issue before the next tick.
    With auto_merge off, one issue re-implements itself indefinitely.
    """
    from agentloop.config import LABEL_READY, LABEL_WIP, Repo
    from agentloop.watchers import _collect_finished

    ws = Workspace(tmp_path, "o/r")
    ws.logs.mkdir(parents=True)
    (ws.logs / "issue-3.rc").write_text("0", encoding="utf-8")
    tree = ws.trees / "issue-3"
    tree.mkdir(parents=True)
    cfg, repo = Config(), Repo(slug="o/r", default_branch="main")

    with patch("agentloop.tmux.available", return_value=True), \
         patch("agentloop.tmux.is_running", return_value=False), \
         patch("agentloop.tmux.exit_code", return_value=0), \
         patch("agentloop.tmux.output", return_value=""), \
         patch("agentloop.tmux.kill"), \
         patch.object(Workspace, "ahead_of", return_value=1), \
         patch.object(Workspace, "has_changes", return_value=False), \
         patch.object(Workspace, "changed_paths", return_value=["lib/arabic.ts"]), \
         patch.object(Workspace, "push"), \
         patch("agentloop.gh.run", return_value="a title"), \
         patch("agentloop.gh.pr_for_branch", return_value=None), \
         patch("agentloop.gh.create_pr") as create, \
         patch("agentloop.gh.remove_label") as unlabel:
        out = _collect_finished(cfg, repo, ws)

    assert any("PR opened" in line for line in out), out
    create.assert_called_once()
    dropped = {c.args[2] for c in unlabel.call_args_list}
    assert dropped == {LABEL_WIP, LABEL_READY}, (
        f"collection dropped {dropped}; leaving {LABEL_READY} on is what made "
        "the next tick start another agent on an issue that already has a PR")


def test_ordinary_work_is_not_blocked(tmp_path):
    """The guard is worth nothing if it fires on normal changes."""
    tree = tmp_path / "t4"
    _repo(tree)
    ws = Workspace(tmp_path / "ws4", "o/r")
    (tree / "src.py").write_text("real change\n", encoding="utf-8")
    ws.commit_all(tree, "ordinary")
    assert touches_guarded_path(ws.changed_paths(tree, "main"), Config()) == []
