"""Which stores a command sees, and a record as the rest of claude-tasks reads it.

A store is an itemctl store (a directory holding `store.toml`): the home store at
`$CLAUDE_TASKS_HOME` (default `~/claude-tasks`) for work tied to no single project, and a
`.claude-tasks/` inside a project's repo for that project's records. A store holds ideas,
handovers and, where the build ships that kind, friction entries (`schema`); a `Task` is an idea, and `load_tasks` reads ideas only. There is no registry: inside a
repo the git toplevel gives the store, `--store PATH` names one explicitly, and each store issues
its own id sequence. itemctl owns everything on disk — parsing, the lock, ids, placement, commits.

A repository has one store, in its main checkout: from a linked worktree, `<main checkout>/
.claude-tasks` is the store a read sees and a write goes to, never `<worktree>/.claude-tasks`.
Records are the project's, not a branch's — every session of the repository sees one id sequence,
and a record outlives the worktree it was filed from. A write therefore commits on whatever branch
the main checkout has checked out, so a write is refused unless that checkout is on the
repository's default branch (`check_on_default_branch`); each commit is a pathspec commit of
exactly the store paths it wrote (itemctl's `gitio.commit`): the main checkout's index, its other
staged and unstaged changes, and the worktree's own branch are left as they were. One `add` or
`set` (of any number of ids) is one commit; creating the store is one more. From a linked worktree, `add` and `set` say
so on stderr after the commit (`worktree_notice`), since nothing else shows the write left the
worktree.

`view_roots` is the one site of the two-roots rule — inside a repo that has a store, a read sees
that store *and* the home store; `--store`/`--home` pin one; everywhere else it is the home store
alone. `write_root` is the same rule for the one store a mutation writes to.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from itemctl import gitio
from itemctl import schema as item_schema
from itemctl.model import Finding, Item, NoStore, Snapshot
from itemctl.store import Store

from claude_tasks.env import home_store_root
from claude_tasks.errors import OffDefaultBranch, TasksError
from claude_tasks.schema import FRICTION, KIND

PROJECT_STORE_NAME = ".claude-tasks"
STORE_FILE = "store.toml"


def legacy_ids_of(fields: Mapping[str, object]) -> tuple[str, ...]:
    """Every legacy id `fields` carries, in record order."""
    value = fields.get("legacy_ids")
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, tuple) else ()


def legacy_findings(snap: Snapshot) -> list[Finding]:
    """Duplicate legacy ids, a legacy id that is a live id, and one an `add` would reissue."""
    live = {item.id: item for item in snap.items}
    top = max((item_schema.id_number(snap.schema, i) or 0 for i in live), default=0)
    seen: dict[str, PurePosixPath] = {}
    findings: list[Finding] = []
    for item in snap.items:
        for old in legacy_ids_of(item.fields):
            if old in seen:
                findings.append(
                    Finding(
                        "legacy_id", str(item.path), f"legacy id {old!r} duplicates {seen[old]}"
                    )
                )
                continue
            seen[old] = item.path
            if old in live:
                findings.append(
                    Finding(
                        "legacy_id",
                        str(item.path),
                        f"legacy id {old!r} is the live id of {live[old].path}; an old reference "
                        "must resolve to exactly one record",
                    )
                )
            number = item_schema.id_number(snap.schema, old)
            if number is not None and number > top:
                findings.append(
                    Finding(
                        "legacy_id",
                        str(item.path),
                        f"legacy id {old!r} is above every live id; the next `add` would issue it "
                        "again",
                    )
                )
    return findings


@dataclass(frozen=True)
class Task:
    """One idea record of an opened store."""

    store: Store
    item: Item

    @property
    def fm(self) -> Mapping[str, object]:
        """The record's frontmatter, read-only."""
        return self.item.fields

    @property
    def id(self) -> str:
        """The task id."""
        return self.item.id

    @property
    def state(self) -> str:
        """The task state."""
        return str(self.fm.get("state"))

    @property
    def title(self) -> str:
        """The task title."""
        return str(self.fm.get("title"))

    @property
    def path(self) -> Path:
        """The record file's absolute path."""
        return self.store.root / self.item.path

    @property
    def body(self) -> str:
        """The markdown below the frontmatter."""
        return self.item.body

    def legacy_ids(self) -> tuple[str, ...]:
        """Every id this record used to have, in record order."""
        return legacy_ids_of(self.fm)


def prefix_of(store: Store) -> str:
    """The store's id prefix."""
    return store.schema.id_prefix


def toplevel(path: Path) -> Path | None:
    """The checkout (main or linked worktree) containing the directory `path`, or None outside git."""
    proc = gitio.run(path, "rev-parse", "--show-toplevel")
    if proc.returncode == 0:
        return Path(proc.stdout.strip())
    if "not a git repository" in proc.stderr:
        return None
    raise TasksError(f"git rev-parse --show-toplevel in {path} failed: {proc.stderr.strip()}")


def worktree_notice(store_root: Path, cwd: Path) -> str | None:
    """The stderr line a write owes when `cwd` is in a linked worktree and `store_root` is its
    repository's one store in the main checkout, else None. Names the commit the write just made
    on the main checkout's HEAD, leaving the sha or the branch out when git cannot give it."""
    top = toplevel(cwd)
    if top is None:
        return None
    main = gitio.main_checkout(top)
    if main == top or store_root != main / PROJECT_STORE_NAME:
        return None
    sha = gitio.run(main, "rev-parse", "--short", "HEAD")
    branch = gitio.run(main, "branch", "--show-current")
    parts = [f"commit {sha.stdout.strip()}"] if sha.returncode == 0 and sha.stdout.strip() else []
    if branch.returncode == 0 and branch.stdout.strip():
        parts.append(f"on {branch.stdout.strip()}")
    detail = f"; {' '.join(parts)}" if parts else ""
    return (
        f"claude-tasks: wrote {store_root} (the repository's one store, in its main checkout"
        f"{detail})"
    )


def default_branch(checkout: Path) -> str | None:
    """The default branch of the repository at `checkout`: `refs/remotes/origin/HEAD`'s target when
    set, else whichever of `main`/`master` exists locally, else None."""
    proc = gitio.run(checkout, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().removeprefix("origin/")
    for name in ("main", "master"):
        if gitio.run(checkout, "show-ref", "--verify", "-q", f"refs/heads/{name}").returncode == 0:
            return name
    return None


def check_on_default_branch(root: Path) -> None:
    """Refuse a write to the repo store at `root` unless its checkout is on the default branch.

    A write commits on whatever branch that checkout has, so on a feature branch or a peer's WIP
    the record would be stranded there. The home store, a path outside any repository and a
    repository with no known default branch are not checked. Never switches branches.
    """
    if root.resolve() == home_store_root().resolve():
        return
    anchor = next(p for p in (root, *root.parents) if p.exists())
    checkout = toplevel(anchor)
    if checkout is None:
        return
    default = default_branch(checkout)
    if default is None:
        return
    proc = gitio.run(checkout, "symbolic-ref", "-q", "--short", "HEAD")
    current = proc.stdout.strip() if proc.returncode == 0 else None
    if current == default:
        return
    found = "a detached HEAD" if current is None else f"branch {current!r}"
    raise OffDefaultBranch(
        f"{checkout} is on {found}, not the default branch {default!r}, so a write there would "
        f"commit the record off {default!r}; switch the main checkout back to {default}"
    )


def git_init(path: Path) -> None:
    """Run `git init` in `path`."""
    gitio.checked(path, "init", "-q")


def is_store_root(root: Path) -> bool:
    """Whether `root` holds a claude-tasks store.

    claude-tasks owns two kinds of directory -- a `.claude-tasks/` and the home store root -- and
    recognises a store in no other, so another tool's `store.toml` is never read as one.
    """
    owned = root.name == PROJECT_STORE_NAME or root.resolve() == home_store_root().resolve()
    return owned and (root / STORE_FILE).is_file()


def open_store(root: Path) -> Store:
    """Open the store at `root`, gated by the legacy-id rules and, where the build ships the
    friction kind, its entry rule; raise when it has no `store.toml`."""
    resolved = root.expanduser().resolve()
    rules = (legacy_findings,) if FRICTION is None else (legacy_findings, FRICTION.entry_rule)
    try:
        return Store(resolved, rules=rules)
    except NoStore as exc:
        raise TasksError(f"{resolved}: no {STORE_FILE}; not a claude-tasks store") from exc


def repo_store_root(cwd: Path) -> Path | None:
    """`<git toplevel>/.claude-tasks` for the repo holding `cwd`, or None outside a repository.

    A linked worktree maps to its main checkout, so every session of one repository shares the
    one store rather than writing into a per-worktree copy.
    """
    top = toplevel(cwd)
    return None if top is None else gitio.main_checkout(top) / PROJECT_STORE_NAME


def pinned_root(store_arg: str) -> Path:
    """`--store PATH`: that store directory, which must already exist, so a typo cannot seed one."""
    raw = Path(store_arg).expanduser().resolve()
    if not raw.is_dir():
        raise TasksError(f"--store {store_arg}: {raw} is not a directory (it must already exist)")
    return gitio.main_checkout(raw)


def view_roots(store_arg: str | None, home: bool, cwd: Path) -> list[Path]:
    """The store roots a read sees: the one `--store`/`--home` pins, else this repo's store when
    it has one plus the home store (left out while it does not exist and the repo store can be
    read), so a mixed queue tells the two apart by id prefix alone."""
    if home and store_arg is not None:
        raise TasksError("--home and --store name different stores; pass one of the two")
    if home:
        return [home_store_root()]
    if store_arg is not None:
        return [pinned_root(store_arg)]
    repo = repo_store_root(cwd)
    repo_roots = [repo] if repo is not None and is_store_root(repo) else []
    home_root = home_store_root()
    # With a repo store to read, a home store not yet created is skipped; with nothing else to
    # read it stays in the view, so `open_store` names the missing store.toml.
    if repo_roots and not is_store_root(home_root):
        return repo_roots
    return [*repo_roots, home_root]


def view_stores(store_arg: str | None, home: bool, cwd: Path) -> list[Store]:
    """Every store of the view, opened; a root that holds no store raises naming what creates one."""
    return [open_store(root) for root in view_roots(store_arg, home, cwd)]


def existing_stores(cwd: Path) -> list[Store]:
    """The view's stores that exist, creating and demanding nothing — what the hooks read."""
    return [open_store(root) for root in view_roots(None, False, cwd) if is_store_root(root)]


def write_root(store_arg: str | None, home: bool, cwd: Path) -> Path:
    """The one root a mutation writes to; unlike a read, this repo's store counts before it exists
    (`add` creates it) and everything outside a repository is the home store."""
    if home and store_arg is not None:
        raise TasksError("--home and --store name different stores; pass one of the two")
    if home:
        return home_store_root()
    if store_arg is not None:
        return pinned_root(store_arg)
    repo = repo_store_root(cwd)
    return repo if repo is not None else home_store_root()


def load_tasks(store: Store) -> list[Task]:
    """Every idea of the store in id order; raise listing every unparsable file or bad id."""
    snap = store.snapshot()
    problems = [f"{store.root / f.where}: {f.message}" for f in snap.broken]
    numbered: list[tuple[int, Task]] = []
    for item in (item for item in snap.items if item.kind == KIND):
        number = item_schema.id_number(store.schema, item.fields.get("id"))
        if number is None:
            problems.append(f"{store.root / item.path}: id {item.fields.get('id')!r} is malformed")
        else:
            numbered.append((number, Task(store, item)))
    if problems:
        raise TasksError("unreadable record(s); run `claude-tasks check`:\n" + "\n".join(problems))
    return [task for _number, task in sorted(numbered, key=lambda pair: pair[0])]
