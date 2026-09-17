"""Re-queueing an issue whose old pull request tree is still around.

This is the defect that stopped the loop on 2026-09-17, and it is the kind that
looks healthiest: every tick reported "no ready issues" while two issues sat
labelled agent:ready, because starting either one raised and the raise was one
line in a journal nobody reads.

A worktree is named after the number being worked, and two things work numbers:
the issue starter names its tree after the ISSUE, the fixer names its tree
after the PULL REQUEST. So one branch has two possible homes. `agent/52-...`
lived in `trees/issue-108`, because #108 was the pull request for issue #52,
and nothing ever removed it: the box held 35 trees, one per issue ever worked.

Close the pull request, re-queue the issue, and `create()` dies forever on

    fatal: 'agent/52-story-cancel-an-event-and-keep-it-struck' is already used
    by worktree at '.../trees/issue-108'
"""
import subprocess
from pathlib import Path

from agentloop.worktree import Workspace, branch_name


def _origin_and_clone(tmp_path: Path, slug: str) -> Workspace:
    """A real bare origin and a real clone, because create() fetches."""
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
    (seed / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("add", "-A", cwd=seed)
    run("commit", "-qm", "seed", cwd=seed)
    run("remote", "add", "origin", str(origin), cwd=seed)
    run("push", "-q", "origin", "main", cwd=seed)

    ws = Workspace(tmp_path / "work", slug)
    ws.clone.parent.mkdir(parents=True, exist_ok=True)
    run("clone", "-q", str(origin), str(ws.clone), cwd=tmp_path)
    run("config", "user.email", "agent@example.com", cwd=ws.clone)
    run("config", "user.name", "agent", cwd=ws.clone)
    ws.trees.mkdir(parents=True, exist_ok=True)
    return ws


def test_an_issue_can_be_requeued_when_its_old_pr_tree_still_holds_the_branch(tmp_path):
    ws = _origin_and_clone(tmp_path, "MHutatah/ipa-community")
    title = "Cancel an event and keep it struck through for a week"
    branch = branch_name(52, title)

    # The fixer's tree for pull request #108, holding issue #52's branch.
    pr_tree = ws.trees / "issue-108"
    subprocess.run(["git", "worktree", "add", "-q", "-B", branch, str(pr_tree),
                    "origin/main"], cwd=ws.clone, check=True, capture_output=True)
    assert pr_tree.exists()

    # The pull request is closed and the issue is re-queued. This raised
    # RuntimeError("git worktree add -B: fatal: ... already used by worktree").
    path, made = ws.create(52, title)

    assert made == branch
    assert path == ws.trees / "issue-52"
    on = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path,
                        check=True, capture_output=True, text=True).stdout.strip()
    assert on == branch
    # and the tree that was squatting on it is gone rather than merely pruned
    assert not pr_tree.exists()


def test_a_worktree_outside_our_trees_directory_is_left_alone(tmp_path):
    """The release only ever removes our own trees.

    Another checkout of the same clone elsewhere on the box belongs to somebody
    else, and this box runs other people's services. Being unable to start an
    issue is a stopped loop; deleting a directory we do not own is worse.
    """
    ws = _origin_and_clone(tmp_path, "MHutatah/ipa-community")
    title = "Cancel an event and keep it struck through for a week"
    branch = branch_name(52, title)

    outside = tmp_path / "somebody-elses-checkout"
    subprocess.run(["git", "worktree", "add", "-q", "-B", branch, str(outside),
                    "origin/main"], cwd=ws.clone, check=True, capture_output=True)

    # create() cannot have the branch, so it still fails, and it fails loudly
    # rather than by removing what is not ours.
    try:
        ws.create(52, title)
    except RuntimeError as error:
        assert "already used by worktree" in str(error)
    assert outside.exists()
