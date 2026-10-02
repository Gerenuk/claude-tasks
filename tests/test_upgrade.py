"""`claude-tasks upgrade`: a store carrying the exact v1 schema text becomes a store declaring
every kind this build ships (`schema.SCHEMA`), in one commit; any other text is refused with its diff, and
`--dry-run` writes nothing.

The expected result is checked by what the upgraded store declares (its kinds, as itemctl parses
them) and by the gitignore line a handover's files need, not by comparing to `store_toml` itself.
"""

import subprocess
from pathlib import Path

import pytest
from conftest import commit_count, git, idea, run_cli
from itemctl.store import Store

from claude_tasks import store_index
from claude_tasks.env import store_index_dir
from claude_tasks.schema import SCHEMA, store_toml_v1
from claude_tasks.store import open_store

Capsys = pytest.CaptureFixture[str]


@pytest.fixture
def v1_home(isolated_env: Path) -> Path:
    """A committed home store as every store was before the upgrade: the v1 text, `.lock` ignored,
    one idea filed."""
    root = isolated_env / "home-store"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "store.toml").write_text(store_toml_v1("CT"))
    (root / ".gitignore").write_text(".lock\n")
    git(root, "add", "store.toml", ".gitignore")
    git(root, "commit", "-q", "-m", "init")
    idea(open_store(root), "kept")
    return root


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file under `root` outside `.git`, with its bytes."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def test_upgrade_moves_a_v1_store_to_the_current_kinds_in_one_commit(
    v1_home: Path, capsys: Capsys
) -> None:
    """The v1 text becomes a schema declaring this build's kinds, the handover files
    line is ignored, the existing idea still reads, and it is one clean commit (allow)."""
    commits = commit_count(v1_home)

    code, out, err = run_cli(capsys, "upgrade", "--home")

    assert code == 0, err
    assert "upgraded" in out
    store = Store(v1_home)
    assert [k.name for k in store.schema.kinds] == [k.name for k in SCHEMA.kinds]
    assert str(store.schema.kind("handover").dir) == "handover"
    assert (v1_home / ".gitignore").read_text().splitlines() == [".lock", "handover/**/files/"]
    assert commit_count(v1_home) == commits + 1
    assert git(v1_home, "status", "--porcelain").strip() == ""
    ignored = subprocess.run(
        ["git", "-C", str(v1_home), "check-ignore", "-q", "handover/some-slug/files/wip.patch"],
        check=False,
    )
    assert ignored.returncode == 0
    code, out, err = run_cli(capsys, "list", "--home")
    assert code == 0, err
    assert "kept" in out


def test_upgrade_of_a_current_store_changes_nothing(v1_home: Path, capsys: Capsys) -> None:
    """A second upgrade finds nothing to do and commits nothing."""
    run_cli(capsys, "upgrade", "--home")
    commits, before = commit_count(v1_home), snapshot(v1_home)

    code, out, err = run_cli(capsys, "upgrade", "--home")

    assert code == 0, err
    assert "already current" in out
    assert commit_count(v1_home) == commits
    assert snapshot(v1_home) == before


def test_upgrade_refuses_a_store_toml_it_does_not_know(v1_home: Path, capsys: Capsys) -> None:
    """A hand-edited store.toml is neither v1 nor current: refused with the diff, nothing written
    (deny)."""
    config = v1_home / "store.toml"
    config.write_text(config.read_text().replace('"built", ', '"built", "shelved", '))
    git(v1_home, "commit", "-q", "-am", "hand edit")
    commits, before = commit_count(v1_home), snapshot(v1_home)

    code, out, err = run_cli(capsys, "upgrade", "--home")

    assert code == 1
    assert out == ""
    assert "neither the v1 nor the current schema" in err
    assert '+outcome = ["built", "shelved", "dropped", "superseded"]' in err
    assert commit_count(v1_home) == commits
    assert snapshot(v1_home) == before


def test_upgrade_records_the_store_in_the_index(v1_home: Path, capsys: Capsys) -> None:
    """A real upgrade indexes the store, so machine-wide reads see it before its next write
    (allow); a dry run leaves the index untouched (deny)."""
    for link in store_index_dir().glob("*"):
        link.unlink()

    code, _out, err = run_cli(capsys, "upgrade", "--home", "--dry-run")
    assert code == 0, err
    assert store_index.stores().stores == ()

    code, _out, err = run_cli(capsys, "upgrade", "--home")
    assert code == 0, err
    assert [(s.prefix, s.root) for s in store_index.stores().stores] == [("CT", v1_home)]


def test_dry_run_writes_nothing(v1_home: Path, capsys: Capsys) -> None:
    """`--dry-run` names the files an upgrade would rewrite and leaves every byte and the history
    as they were."""
    commits, before = commit_count(v1_home), snapshot(v1_home)

    code, out, err = run_cli(capsys, "upgrade", "--home", "--dry-run")

    assert code == 0, err
    assert "would rewrite store.toml, .gitignore" in out
    assert commit_count(v1_home) == commits
    assert snapshot(v1_home) == before
    assert Store(v1_home).schema.kinds[0].name == "idea"
    assert len(Store(v1_home).schema.kinds) == 1


def test_upgrade_needs_an_existing_store(isolated_env: Path, capsys: Capsys) -> None:
    """Upgrade never creates a store: with none there it is refused."""
    code, _out, err = run_cli(capsys, "upgrade", "--home")

    assert code == 1
    assert "no store.toml" in err
    assert not (isolated_env / "home-store").exists()
