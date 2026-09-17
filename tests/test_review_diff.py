"""What the judge is handed, and the three ways handing it `gh pr diff` was wrong.

All three were measured on 2026-09-17 against the calendar epic's pull requests:
the diff was mostly redundant with files the judge then read itself, a quarter
of its bytes were carriage returns, and it arrived with no map.
"""
import subprocess

from agentloop.worktree import Workspace


def _clone_with_tree(tmp_path, *, crlf_file=False, big=False):
    """A clone, and a worktree on a branch one commit ahead of origin/main."""
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
    # PINNED, so this test means the same thing on both platforms. With
    # autocrlf on, as it is on the author's Windows machine, git normalises the
    # blob and a line-ending-only change is not even committable, which is
    # exactly why the noise shows up on the Linux box and not in local runs.
    run("config", "core.autocrlf", "false", cwd=seed)
    # Written with CRLF, the way this repository's files are.
    (seed / "keep.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    run("add", "-A", cwd=seed)
    run("commit", "-qm", "seed", cwd=seed)
    run("remote", "add", "origin", str(origin), cwd=seed)
    run("push", "-q", "origin", "main", cwd=seed)

    ws = Workspace(tmp_path / "work", "o/r")
    ws.clone.parent.mkdir(parents=True, exist_ok=True)
    run("clone", "-q", str(origin), str(ws.clone), cwd=tmp_path)
    run("config", "user.email", "agent@example.com", cwd=ws.clone)
    run("config", "user.name", "agent", cwd=ws.clone)
    run("config", "core.autocrlf", "false", cwd=ws.clone)
    ws.trees.mkdir(parents=True, exist_ok=True)

    tree = ws.trees / "issue-1"
    run("worktree", "add", "-q", "-B", "agent/1-x", str(tree), "origin/main",
        cwd=ws.clone)

    if crlf_file:
        # ONLY the line endings change. Nothing else about the file moves.
        (tree / "keep.txt").write_bytes(b"one\ntwo\nthree\n")
    if big:
        (tree / "big.txt").write_text("x" * 200_000 + "\n", encoding="utf-8")
    if not crlf_file and not big:
        (tree / "keep.txt").write_bytes(b"one\r\ntwo\r\nthree\r\nfour\r\n")
    run("add", "-A", cwd=tree)
    run("commit", "-qm", "work", cwd=tree)
    return ws, tree


def test_a_line_ending_only_change_is_not_shown_as_a_rewrite(tmp_path):
    """The one that cost the most, because it misleads as well as costs.

    `gh pr diff` renders a CRLF-to-LF run as a whole-file rewrite. `lib/roles.ts`
    reached the judge as +155/-135, a rewrite of the permission system, when the
    real change was twenty added lines and nothing touched.
    """
    ws, tree = _clone_with_tree(tmp_path, crlf_file=True)
    out = ws.review_diff(tree, "main")
    # It costs the judge nothing at all: no hunk, and not even a line in the
    # stat, because semantically nothing happened. `gh pr diff` on the same
    # commit renders every line of the file as removed and re-added.
    assert out.strip() == "(no diff against the base)"
    assert "+one" not in out and "-one" not in out


def test_the_stat_comes_first_so_the_judge_has_a_map(tmp_path):
    ws, tree = _clone_with_tree(tmp_path)
    out = ws.review_diff(tree, "main")
    assert out.index("keep.txt") < out.index("diff --git")
    assert "+four" in out          # and the real change is still there


def test_a_cap_says_it_is_a_cap_and_where_the_rest_is(tmp_path):
    """Truncating silently is the worst option: a judge cannot report having
    seen half of something it was not told was half."""
    ws, tree = _clone_with_tree(tmp_path, big=True)
    out = ws.review_diff(tree, "main", max_chars=500)
    assert "was cut at 500" in out
    assert "INSIDE the worktree" in out
    assert len(out) < 3_000        # the point of the exercise


def test_a_diff_under_the_cap_is_sent_whole_with_no_notice(tmp_path):
    ws, tree = _clone_with_tree(tmp_path)
    out = ws.review_diff(tree, "main")
    assert "was cut at" not in out
