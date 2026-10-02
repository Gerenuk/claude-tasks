"""A write to a repo store is refused unless the store's checkout is on the default branch.

Records commit in the main checkout, on whatever branch it has; a feature branch or a peer's WIP
would strand them. The check refuses instead of switching, and writes nothing.
"""

from pathlib import Path

import pytest
from conftest import body_file, commit_count, git, idea, make_repo, run_cli
from itemctl.store import Store

from claude_tasks.ops import init_store
from claude_tasks.store import default_branch

Capsys = pytest.CaptureFixture[str]


@pytest.fixture
def repo(isolated_env: Path, home: Store, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo on `main` holding a committed `.claude-tasks` store with one record (PR-0001), with
    the home store initialised and the cwd inside the repo; `body.md` is already written."""
    path = make_repo(isolated_env / "proj")
    idea(init_store(path / ".claude-tasks", "PR", commit=True), "first")
    monkeypatch.chdir(path)
    body_file()
    git(path, "add", "body.md")
    git(path, "commit", "-q", "-m", "body")
    return path


def state(path: Path) -> tuple[int, str, str]:
    """Commit count, working-tree status and current branch: what a refused write must not change."""
    return (
        commit_count(path),
        git(path, "status", "--porcelain"),
        git(path, "branch", "--show-current"),
    )


def test_write_on_the_default_branch_succeeds(repo: Path, capsys: Capsys) -> None:
    """With the main checkout on `main`, add and set commit as before."""
    before = commit_count(repo)
    for argv in (
        ("add", "--title", "second", "--body-file", "body.md"),
        ("set", "PR-0001", "size=M"),
    ):
        code, _out, err = run_cli(capsys, *argv)
        assert code == 0, err
    assert commit_count(repo) == before + 2


def test_add_with_the_main_checkout_on_a_feature_branch_is_refused(
    repo: Path, capsys: Capsys
) -> None:
    """On `feat/x` add exits 1 naming the branch, the default and `--home`; nothing is written."""
    git(repo, "checkout", "-q", "-b", "feat/x")
    before = state(repo)
    code, out, err = run_cli(capsys, "add", "--title", "second", "--body-file", "body.md")
    assert code == 1
    assert out == ""
    assert "'feat/x'" in err
    assert "'main'" in err
    assert "switch the main checkout back to main, or file in the home store with --home" in err
    assert state(repo) == before
    assert not (repo / ".claude-tasks" / "second.md").exists()


def test_set_with_the_main_checkout_on_a_feature_branch_is_refused(
    repo: Path, capsys: Capsys
) -> None:
    """set is refused the same way, leaving the record and history as they were."""
    git(repo, "checkout", "-q", "-b", "feat/x")
    record = repo / ".claude-tasks" / "first.md"
    text = record.read_text()
    before = state(repo)
    code, _out, err = run_cli(capsys, "set", "PR-0001", "size=M")
    assert code == 1
    assert "'feat/x'" in err
    assert state(repo) == before
    assert record.read_text() == text


def test_the_first_add_on_a_feature_branch_creates_no_store(
    isolated_env: Path, home: Store, monkeypatch: pytest.MonkeyPatch, capsys: Capsys
) -> None:
    """A refused add creates neither the store directory nor its init commit."""
    bare = make_repo(isolated_env / "fresh")
    monkeypatch.chdir(bare)
    body_file()
    git(bare, "add", "body.md")
    git(bare, "commit", "-q", "-m", "body")
    git(bare, "checkout", "-q", "-b", "feat/x")
    before = state(bare)
    code, _out, err = run_cli(capsys, "add", "--title", "t", "--body-file", "body.md")
    assert code == 1
    assert "'feat/x'" in err
    assert state(bare) == before
    assert not (bare / ".claude-tasks").exists()


def test_a_write_from_a_worktree_is_judged_by_the_main_checkouts_branch(
    repo: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: Capsys
) -> None:
    """Whichever worktree the write comes from, the branch that counts is the main checkout's."""
    worktree = isolated_env / "proj-wt" / "feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    monkeypatch.chdir(worktree)
    code, _out, err = run_cli(capsys, "add", "--title", "ok", "--body-file", "body.md")
    assert code == 0, err
    git(repo, "checkout", "-q", "-b", "feat/x")
    before = state(repo)
    code, _out, err = run_cli(capsys, "add", "--title", "no", "--body-file", "body.md")
    assert code == 1
    assert "'feat/x'" in err
    assert state(repo) == before


def test_a_detached_head_is_refused(repo: Path, capsys: Capsys) -> None:
    """A detached main checkout is refused, and the message says so."""
    git(repo, "checkout", "-q", "--detach")
    before = state(repo)
    code, _out, err = run_cli(capsys, "add", "--title", "t", "--body-file", "body.md")
    assert code == 1
    assert "detached HEAD" in err
    assert "'main'" in err
    assert state(repo) == before


def test_home_store_writes_are_not_checked(repo: Path, capsys: Capsys) -> None:
    """`--home` files from a repo on a feature branch: the home store is another repository."""
    git(repo, "checkout", "-q", "-b", "feat/x")
    before = state(repo)
    code, _out, err = run_cli(capsys, "add", "--home", "--title", "t", "--body-file", "body.md")
    assert code == 0, err
    assert state(repo) == before


def test_a_repo_with_no_main_master_or_origin_head_skips_the_check(
    repo: Path, capsys: Capsys
) -> None:
    """With no default branch to name, a write on any branch goes through."""
    git(repo, "branch", "-m", "main", "develop")
    assert default_branch(repo) is None
    before = commit_count(repo)
    code, _out, err = run_cli(capsys, "add", "--title", "t", "--body-file", "body.md")
    assert code == 0, err
    assert commit_count(repo) == before + 1


def test_origin_head_names_the_default_branch_over_a_local_main(repo: Path, capsys: Capsys) -> None:
    """`refs/remotes/origin/HEAD` -> `origin/trunk` makes `trunk` the default, so `main` is refused."""
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert default_branch(repo) == "trunk"
    before = state(repo)
    code, _out, err = run_cli(capsys, "add", "--title", "t", "--body-file", "body.md")
    assert code == 1
    assert "'main'" in err
    assert "'trunk'" in err
    assert state(repo) == before


def test_master_is_the_default_when_main_does_not_exist(repo: Path) -> None:
    """A repo whose only default-looking branch is `master` has `master` as its default."""
    git(repo, "branch", "-m", "main", "master")
    assert default_branch(repo) == "master"
