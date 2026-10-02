"""store_index: one symlink per store under `$CLAUDE_TASKS_INDEX`, `<PREFIX>` → the store root.

`isolated_env` points the index into each test's tmp dir, so no test sees the real index or
another test's stores.
"""

import os
from pathlib import Path

import pytest
from conftest import make_repo
from itemctl.store import Store

from claude_tasks import store_index
from claude_tasks.errors import TasksError
from claude_tasks.ops import init_store


def index_dir(isolated_env: Path) -> Path:
    """The index directory `isolated_env` set."""
    return isolated_env / "store-index"


def test_creating_a_store_indexes_it(isolated_env: Path, home: Store) -> None:
    """`init_store` links the new store's prefix to its root; `stores()` lists it with no notes."""
    link = index_dir(isolated_env) / "CT"
    assert link.is_symlink()
    assert Path(os.readlink(link)) == home.root.resolve()
    assert store_index.stores() == store_index.Index(
        (store_index.IndexedStore("CT", home.root.resolve()),), ()
    )


def test_recording_the_same_root_again_is_a_no_op(isolated_env: Path, home: Store) -> None:
    """Re-recording a store at the root it is already linked to changes nothing (allow)."""
    link = index_dir(isolated_env) / "CT"
    before = os.readlink(link)

    store_index.record("CT", home.root)
    init_store(home.root, None, commit=False)

    assert os.readlink(link) == before


def test_the_same_prefix_at_a_second_root_is_refused(isolated_env: Path, home: Store) -> None:
    """A second live store claiming an indexed prefix is refused, naming both roots, and the link
    keeps its target (deny); `init_store` refuses before writing that store's store.toml."""
    other = make_repo(isolated_env / "other") / ".claude-tasks"
    link = index_dir(isolated_env) / "CT"

    with pytest.raises(TasksError, match="already the store at") as err:
        store_index.record("CT", other)
    assert str(home.root.resolve()) in str(err.value)
    assert Path(os.readlink(link)) == home.root.resolve()

    with pytest.raises(TasksError, match="already the store at"):
        init_store(other, "CT", commit=True)
    assert not (other / "store.toml").exists()


def test_a_dangling_link_is_a_note_and_is_replaced_on_record(
    isolated_env: Path, home: Store
) -> None:
    """A link to a root that holds no store is reported by `stores()`, never raised, and a new
    store with that prefix takes the link over."""
    gone = isolated_env / "gone" / ".claude-tasks"
    index = index_dir(isolated_env)
    os.symlink(gone, index / "GONE")

    listed = store_index.stores()

    assert [s.prefix for s in listed.stores] == ["CT"]
    assert len(listed.notes) == 1
    assert "GONE" in listed.notes[0] and "dangling" in listed.notes[0]

    moved = init_store(make_repo(isolated_env / "moved") / ".claude-tasks", "GONE", commit=True)
    assert Path(os.readlink(index / "GONE")) == moved.root.resolve()
    assert store_index.stores().notes == ()


def test_no_index_directory_holds_no_store(isolated_env: Path) -> None:
    """Before any store is recorded, the index is empty rather than an error."""
    assert not index_dir(isolated_env).exists()
    assert store_index.stores() == store_index.Index((), ())
