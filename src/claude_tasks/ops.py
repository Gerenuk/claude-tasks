"""Every mutation: creating a store, upgrading its schema, `add` and `set` (with its `--note`).

Each turns the CLI's tokens into itemctl edits and hands them to one `Store.add`/`Store.set`
call, which locks, validates the whole post-change store against `store.toml` and commits exactly
the record's paths: an invalid record is never written. What stays here is the domain: which
fields `add` writes itself, which never change, how a value is spelled on the command line, and
the body skeleton.

`set` is the one shape for every state change: the state's required fields are enforced by
itemctl's validator in the same call, so there is no per-verb copy of the state table. Its own
checks (immutable fields, `+=` on a list) run through `Store.revise`, under the lock, on the
version of the record the write replaces; a set that ends up changing nothing is `Store.revise`'s
own no-op case, which writes and commits nothing. A refused `set` carries, after itemctl's findings, what each requested record's
state needs (`_refusal_detail`): every required field marked given, missing or invalid, with an
enum's allowed values, so one refusal names every problem. `set` refuses a record of another kind, naming
the command that owns it (`refuse_non_idea`); `add` writes ideas only.

`upgrade_store` moves a store from the idea-only `schema.STORE_TOML_V1` to `schema.store_toml`,
in one commit; it rewrites only that exact text. `init_store` records every store it creates or
opens in the machine-wide `store_index`.
"""

import difflib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from itemctl import fsio, gitio
from itemctl import schema as item_schema
from itemctl.model import (
    AppendLine,
    Assign,
    Edit,
    Extend,
    Item,
    ItemError,
    State,
    Unset,
)
from itemctl.store import Store

from claude_tasks import store_index
from claude_tasks.config import derive_prefix
from claude_tasks.env import home_store_root
from claude_tasks.errors import TasksError
from claude_tasks.schema import (
    ADD_STATES,
    HANDOVER_DIR,
    HANDOVER_FILES_DIR,
    HISTORY_SECTION,
    IDEA,
    IMMUTABLE_FIELDS,
    KIND,
    KIND_OWNERS,
    LIST_FIELDS,
    TYPE,
    check_prefix,
    store_toml,
    store_toml_v1,
)
from claude_tasks.store import (
    STORE_FILE,
    check_on_default_branch,
    git_init,
    open_store,
    prefix_of,
    toplevel,
)

GITIGNORE_NAME = ".gitignore"

#: The lines every store's `.gitignore` holds: itemctl's lock, and each handover's session files.
GITIGNORE_LINES: tuple[str, ...] = (".lock", f"{HANDOVER_DIR}/**/{HANDOVER_FILES_DIR}/")

#: The home store's id prefix; one store, one sequence.
DEFAULT_PREFIX = "CT"


@dataclass(frozen=True)
class Written:
    """Where a mutation left a task."""

    task_id: str
    path: Path

    def line(self) -> str:
        """The `<ID>  <path>` line every mutation prints."""
        return f"{self.task_id}  {self.path}"


def _written(store: Store, item: Item) -> Written:
    """The `Written` of an item itemctl just wrote."""
    return Written(item.id, store.root / item.path)


# --- creating a store ------------------------------------------------------------------------


def init_store(root: Path, prefix: str | None, commit: bool) -> Store:
    """Create the store: its git repo when outside one, `store.toml` and `.gitignore`.

    `prefix` None derives one: the home store keeps `CT`, and a repo store takes its repo
    directory name (`derive_prefix`), which is what `add` passes. A `store.toml` that already
    names a prefix always wins, so this never renames a store; a pre-itemctl store (its
    `config.toml`, no `store.toml`) is refused rather than overwritten. Either way the store is
    recorded in the store index, which refuses a prefix another live store already has.
    """
    root = root.expanduser().absolute()
    config = root / STORE_FILE
    if config.is_file():
        store = open_store(root)
        if prefix is not None and prefix != store.schema.id_prefix:
            raise TasksError(
                f"{config} already sets prefix {store.schema.id_prefix!r}; "
                f"refusing to change it to {prefix!r}"
            )
        store_index.record(prefix_of(store), store.root)
        return store
    if (root / "config.toml").is_file():
        raise TasksError(f"{root} is a pre-itemctl store (config.toml, no store.toml)")
    check_on_default_branch(root)
    root.mkdir(parents=True, exist_ok=True)
    if toplevel(root) is None:
        git_init(root)
    if prefix is None:
        is_home = root.resolve() == home_store_root().resolve()
        prefix = DEFAULT_PREFIX if is_home else derive_prefix(root.parent)
    store_index.record(check_prefix(prefix), root)
    config.write_text(store_toml(prefix), encoding="utf-8")
    created = [config]
    gitignore = root / GITIGNORE_NAME
    if not gitignore.exists():
        gitignore.write_text(_gitignore_text(""), encoding="utf-8")
        created.append(gitignore)
    if commit:
        gitio.check_writable(root)
        gitio.commit(root, created, f"tasks: init {prefix}")
    return open_store(root)


def _gitignore_text(current: str) -> str:
    """`current` `.gitignore` text with every missing line of GITIGNORE_LINES appended."""
    have = current.splitlines()
    missing = "".join(f"{line}\n" for line in GITIGNORE_LINES if line not in have)
    if not missing:
        return current
    return current + ("\n" if current and not current.endswith("\n") else "") + missing


# --- upgrading a store -------------------------------------------------------------------------


@dataclass(frozen=True)
class Upgrade:
    """What `upgrade_store` changed, or would change on a dry run: nothing when already current."""

    root: Path
    prefix: str
    paths: tuple[Path, ...]
    dry_run: bool

    def line(self) -> str:
        """The one line `upgrade` prints."""
        if not self.paths:
            return f"{self.prefix}  {self.root}: already current"
        verb = "would rewrite" if self.dry_run else "upgraded"
        return f"{self.prefix}  {self.root}: {verb} " + ", ".join(p.name for p in self.paths)


def _upgrade_steps(root: Path, prefix: str) -> list[fsio.Write]:
    """The writes taking the store at `root` to the current schema; empty when it is current.

    `store.toml` is rewritten only from the exact v1 text; any other text raises with its diff
    against v1, since a hand-edited schema is not this function's to overwrite.
    """
    config = root / STORE_FILE
    text = config.read_text(encoding="utf-8")
    steps: list[fsio.Write] = []
    if text != store_toml(prefix):
        v1 = store_toml_v1(prefix)
        if text != v1:
            diff = "".join(
                difflib.unified_diff(
                    v1.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    "v1 store.toml",
                    str(config),
                )
            )
            raise TasksError(
                f"{config} is neither the v1 nor the current schema text, so upgrade will not "
                f"rewrite it; its difference from v1:\n{diff}"
            )
        steps.append(fsio.Write(config, store_toml(prefix)))
    gitignore = root / GITIGNORE_NAME
    current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if (wanted := _gitignore_text(current)) != current:
        steps.append(fsio.Write(gitignore, wanted))
    return steps


def upgrade_store(store: Store, dry_run: bool) -> Upgrade:
    """Take `store` from the v1 schema to the current one — `store.toml` rewritten and the
    handover files line added to `.gitignore` — in one commit; `dry_run` computes the same and
    writes nothing. A store already current is left untouched. Either way a real run records the
    store in the machine-wide index, so machine-wide reads see it before its next write. The
    `store` object keeps the schema it was opened with: a caller that goes on writing reopens it
    (`store.open_store`).
    """
    root, prefix = store.root, prefix_of(store)
    if dry_run:
        return Upgrade(root, prefix, tuple(s.path for s in _upgrade_steps(root, prefix)), True)
    check_on_default_branch(root)
    store_index.record(prefix, root)
    with fsio.lock(root):
        steps = _upgrade_steps(root, prefix)
        paths = tuple(s.path for s in steps)
        if not steps:
            return Upgrade(root, prefix, paths, False)
        if store.schema.git_commit:
            gitio.check_writable(root)
        journal = fsio.Journal()
        try:
            for step in steps:
                journal.apply(step)
            open_store(root)
            if store.schema.git_commit:
                gitio.commit(root, list(paths), f"tasks: upgrade {prefix} to the current schema")
        except BaseException:
            journal.rollback()
            raise
    return Upgrade(root, prefix, paths, False)


# --- field values ------------------------------------------------------------------------------


def _refuse_immutable(fld: str) -> None:
    """Raise when `fld` is a field `set` never changes."""
    if fld in IMMUTABLE_FIELDS:
        raise TasksError(f"field {fld!r} is immutable (assigned once by add)")


def _split(raw: str) -> tuple[str, ...]:
    """A comma-separated value's items, stripped, empties dropped."""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def parse_value(fld: str, raw: str) -> object | None:
    """A `field=value` token's value as stored; None removes the field. Raise on an immutable field.

    `null` removes; a declared list field splits on commas; a declared field of another type is
    read as that type (`outcome` must be one of its values); every other field is the string given.
    """
    _refuse_immutable(fld)
    if raw == "null":
        return None
    if fld in LIST_FIELDS:
        return _split(raw) or None
    return item_schema.value_from_text(IDEA, fld, raw)


def _assignment_edits(
    fm: Mapping[str, object], tokens: Sequence[str]
) -> tuple[list[Edit], dict[str, object]]:
    """The itemctl edits of `field=value` / `field+=value` tokens applied left to right, and the
    fields the record has after them. `+=` appends comma-separated items to a list field,
    refusing a non-list current value and a value already present."""
    new = dict(fm)
    edits: list[Edit] = []
    for token in tokens:
        if "=" not in token:
            raise TasksError(f"invalid assignment {token!r}; expected field=value or field+=value")
        name, raw = token.split("=", 1)
        if name.endswith("+"):
            fld = name[:-1]
            _refuse_immutable(fld)
            current = new.get(fld, ())
            if not isinstance(current, tuple) or not all(isinstance(v, str) for v in current):
                raise TasksError(
                    f"field {fld!r} is {current!r}; expected a list of strings before appending"
                )
            for value in _split(raw):
                if value in current:
                    raise TasksError(f"field {fld!r}: value {value!r} is already present")
                current = (*current, value)
                edits.append(Extend(fld, value))
            new[fld] = current
            continue
        value = parse_value(name, raw)
        if value is None:
            new.pop(name, None)
            edits.append(Unset(name))
        else:
            new[name] = value
            edits.append(Assign(name, value))
    return edits, new


# --- set -------------------------------------------------------------------------------------


def refuse_non_idea(item: Item) -> None:
    """Raise when `item` is not an idea, naming the command that reads and writes its kind."""
    if item.kind != KIND:
        owner = KIND_OWNERS.get(item.kind, "the tool that owns it")
        raise TasksError(f"{item.id} is a {item.kind} record, not an idea; use `{owner}`")


def _field_status(fld: str, fields: Mapping[str, object]) -> str:
    """One required field as a refusal shows it: given, missing or invalid, with its allowed
    values when it is an enum."""
    declared = IDEA.field(fld)
    allowed = ", ".join(declared.choices) if declared else ""
    if fld not in fields:
        return f"{fld}: missing" + (f"; one of {allowed}" if allowed else "")
    if allowed and fields[fld] not in declared.choices:
        return f"{fld}: {fields[fld]!r} is invalid; one of {allowed}"
    return f"{fld}: given"


def _state_needs(state: State, fields: Mapping[str, object]) -> list[str]:
    """The line per field `state` requires, each marked given, missing or invalid in `fields`."""
    lines = [f"  {_field_status(fld, fields)}" for fld in state.requires]
    if state.requires_any:
        wanted = " or ".join(state.requires_any)
        met = any(fld in fields for fld in state.requires_any)
        lines.append(f"  {wanted}: {'given' if met else 'missing; at least one'}")
    return lines


def _refusal_detail(task_id: str, fields: Mapping[str, object]) -> list[str]:
    """What the requested `fields` of record `task_id` lack, every problem at once: the states
    there are and what each needs when `state` names none, else what that state needs and which
    required fields are missing or invalid in this call. Empty when nothing required is wrong."""
    requested = fields.get("state")
    if (state := IDEA.find_state(requested)) is not None:
        lines = _state_needs(state, fields)
        bad = any("missing" in ln or "invalid" in ln for ln in lines)
        return [f"{task_id}: state {state.name} needs:", *lines] if bad else []
    lines = [f"{task_id}: {requested!r} is not a state; the states and what each needs:"]
    for state in IDEA.states:
        lines += [f"state {state.name}:", *_state_needs(state, fields)]
    return lines


def _explained(exc: ItemError, requested: Mapping[str, Mapping[str, object]]) -> TasksError:
    """`exc`'s message followed by `_refusal_detail` of every record the call asked to change."""
    detail = [
        ln for task_id, fields in requested.items() for ln in _refusal_detail(task_id, fields)
    ]
    return TasksError("\n".join([str(exc), *detail]))


def _set_planner(
    assignments: list[str], note: str | None, requested: dict[str, Mapping[str, object]]
) -> Callable[[Item], list[Edit]]:
    """The check of `set`'s arguments and the plan `Store.revise` runs on each record: the edits
    of `assignments` and `note`, derived from the record as it is under the store lock. Each
    record's fields after the assignments are left in `requested`, keyed by id, for a refusal."""
    if not assignments and note is None:
        raise TasksError(
            "set changes nothing; pass FIELD=VALUE assignment(s), --note TEXT, or both"
        )
    if note is not None and not note.strip():
        raise TasksError("--note is empty; give the line to append to ## History")

    def plan(item: Item) -> list[Edit]:
        """The edits, checked against the record as it is under the store lock."""
        refuse_non_idea(item)
        edits, requested[item.id] = _assignment_edits(item.fields, assignments)
        if note is not None:
            edits.append(AppendLine(HISTORY_SECTION, note.strip()))
        return edits

    return plan


def set_fields(store: Store, task_id: str, assignments: list[str], note: str | None) -> Written:
    """The one mutation for fields and states: assignments applied (and `note` appended to
    `## History`), validated by itemctl — which is what enforces the new state's required
    fields — before anything is written."""
    return set_many(store, [task_id], assignments, note)[0]


def set_many(
    store: Store, task_ids: Sequence[str], assignments: list[str], note: str | None
) -> list[Written]:
    """`set_fields` on every id of `task_ids`, in one commit (itemctl `Store.revise_many`): each
    record is planned and the whole result validated before the first file is written, so one
    invalid or refused record writes none. The order of `task_ids` is the order of the result."""
    check_on_default_branch(store.root)
    requested: dict[str, Mapping[str, object]] = {}
    plan = _set_planner(assignments, note, requested)
    try:
        items = store.revise_many(task_ids, plan)
    except ItemError as exc:
        raise _explained(exc, requested) from exc
    return [_written(store, item) for item in items]


# --- add -------------------------------------------------------------------------------------


def _sectioned_body(body: str) -> str:
    """Plain prose becomes the Idea section's text above an empty History; a body that already
    has a `##` heading is kept exactly as written — the body's format is the writer's to choose."""
    if any(line.startswith("## ") for line in body.splitlines()):
        return body if body.endswith("\n") else body + "\n"
    return f"## Idea\n\n{body.strip()}\n\n## {HISTORY_SECTION}\n"


def _checked_legacy_ids(legacy_ids: Sequence[str]) -> tuple[str, ...]:
    """`legacy_ids` as `add` stores them; raise on an empty, padded, multi-line or repeated id."""
    for old in legacy_ids:
        if not old or old != old.strip() or len(old.splitlines()) != 1:
            raise TasksError(
                f"--legacy-id {old!r}: expected one non-empty line without surrounding space"
            )
        if legacy_ids.count(old) > 1:
            raise TasksError(f"--legacy-id {old!r} is given twice")
    return tuple(legacy_ids)


def add_task(
    store: Store,
    title: str,
    state: str,
    fields: Mapping[str, object],
    body: str,
    legacy_ids: Sequence[str] = (),
) -> Written:
    """File a new Idea with the next id; a body carrying no section becomes the Idea section.

    `legacy_ids` are the ids the record carried elsewhere, written once here as its `legacy_ids`;
    one that another record of the store already answers to fails the store's legacy-id rule
    (`store.legacy_findings`), so nothing is written.
    """
    check_on_default_branch(store.root)
    old_ids = _checked_legacy_ids(legacy_ids)
    if state not in ADD_STATES:
        raise TasksError(f"--state {state!r}: add files a record only in {ADD_STATES}")
    if not body.strip():
        raise TasksError(
            f"a {TYPE} needs a non-empty body (--body-file PATH, or --body-file - for stdin)"
        )
    for fld in fields:
        if fld in IMMUTABLE_FIELDS or fld in ("title", "state"):
            raise TasksError(f"field {fld!r} is set by add itself; do not pass it as a field")
    edits: list[Edit] = [Assign("type", TYPE), Assign("state", state)]
    edits += [Assign(k, v) for k, v in fields.items() if v is not None]
    if old_ids:
        edits.append(Assign("legacy_ids", old_ids))
    return _written(store, store.add(KIND, title, edits, _sectioned_body(body)))
