"""The machine-wide store index: one symlink per store, `<index>/<PREFIX>` → the store's root.

A machine-wide read (every handover on the machine, say) has no cwd to resolve a store from, so it
reads the index instead. `record` is called by every writer that creates or writes a store
(`ops.init_store`, `claude-ctl friction`'s project write), so a store is indexed from its first
write on. The index answers "which stores exist"; it never maps a project name to the store a
write goes to — a write still resolves its store from the cwd or `--store`.

An id prefix names one store: `record` refuses a prefix already linked to another live store, since
two stores issuing the same ids would make an id name two records. A link whose target holds no
store (the repo moved or was deleted) is dangling: `stores` reports it as a note and never
raises, and `record` replaces it with the live root.

The directory is `env.store_index_dir()` (`$CLAUDE_TASKS_INDEX` overrides it).
"""

import os
from dataclasses import dataclass
from pathlib import Path

from claude_tasks.env import store_index_dir
from claude_tasks.errors import TasksError
from claude_tasks.schema import check_prefix
from claude_tasks.store import STORE_FILE


@dataclass(frozen=True)
class IndexedStore:
    """One live store the index links to."""

    prefix: str
    root: Path


@dataclass(frozen=True)
class Index:
    """What the index holds: its live stores, prefix order, and one note per link it could not
    follow to a store."""

    stores: tuple[IndexedStore, ...]
    notes: tuple[str, ...]


def _holds_store(root: Path) -> bool:
    """Whether `root` is a store directory now."""
    return (root / STORE_FILE).is_file()


def record(prefix: str, root: Path) -> None:
    """Link `<index>/<prefix>` to `root`; a link to the same root is left as it is, a dangling one
    is replaced, and one to another live store raises naming both roots."""
    link = store_index_dir() / check_prefix(prefix)
    target = root.expanduser().resolve()
    if link.is_symlink():
        current = Path(os.readlink(link))
        if current == target:
            return
        if _holds_store(current):
            raise TasksError(
                f"store prefix {prefix!r} is already the store at {current} (index link {link}); "
                f"{target} would issue the same ids — give it another prefix, or remove the link "
                "if that store is gone"
            )
        link.unlink()
    elif link.exists():
        raise TasksError(f"{link} is not a symlink; the store index holds only links")
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)


def stores() -> Index:
    """Every store the index links to, with a note for each link that is dangling or not a link;
    an index directory that does not exist yet holds no store."""
    index = store_index_dir()
    if not index.is_dir():
        return Index((), ())
    live: list[IndexedStore] = []
    notes: list[str] = []
    for link in sorted(index.iterdir()):
        if not link.is_symlink():
            notes.append(f"{link}: not a symlink; the store index holds only links")
            continue
        target = Path(os.readlink(link))
        if _holds_store(target):
            live.append(IndexedStore(link.name, target))
        else:
            notes.append(f"{link} → {target}: no store there (dangling link; remove it)")
    return Index(tuple(live), tuple(notes))
