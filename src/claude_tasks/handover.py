"""The handover kind's flows: `claude-tasks handover new|list|path|set|claim|release|done|commit`
and the pickup context the session-start hook adds.

A handover is the note a paused session leaves for its successor: an itemctl record of the
`handover` kind at `handover/<slug>/item.md` in a store, the session's other files (`agents/`,
`wip.patch`) beside it in `files/`, which git ignores, so only `item.md` is committed. Its shape is
`schema.HANDOVER`; this module is its only writer.

`new` writes to the store a write from its `cwd` goes to (`store.write_root`): the main checkout's
`.claude-tasks/` inside a repository (created and indexed when absent, upgraded in-process when it
still carries the v1 schema), the home store outside one. A repo store whose main checkout is off
its default branch is refused, never swapped for another store.

"Claimed" is not a state: it is an itemctl lease (`claimed_by`, `claimed_at`, `lease_until`, for
`HANDOVER_LEASE`) held by a session id. `claim` refuses while another session's lease is live, and
renews the caller's own; `claim --force` releases the other holder's lease first. A handover is
`open` until `done` moves it to `done` (terminal, under `handover/closed/`), which drops any lease.

A reference is an id (`<PREFIX>-NNNN`) or a slug. It is looked up in the cwd's store, then in the
indexed stores (an id only in the store its prefix names), then in the home store.
"""

import datetime
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from itemctl import changes, fsio, gitio
from itemctl import schema as item_schema
from itemctl.model import Assign, Edit, Item, ItemError, thaw
from itemctl.store import Store

from claude_tasks import store_index
from claude_tasks.env import home_store_root
from claude_tasks.errors import TasksError
from claude_tasks.ops import init_store, upgrade_store
from claude_tasks.schema import HANDOVER, HANDOVER_FILES_DIR, HANDOVER_KIND, KIND_OWNERS
from claude_tasks.store import (
    check_on_default_branch,
    is_store_root,
    open_store,
    prefix_of,
    write_root,
)

#: How long a claim holds before another session may take the handover without `--force`.
HANDOVER_LEASE = datetime.timedelta(days=7)

#: The fields `set` changes; the title is the slug's source, and state and lease move only
#: through claim, release and done.
SETTABLE_FIELDS: tuple[str, ...] = (
    "model",
    "size",
    "cwd",
    "branch",
    "wip_commit",
    "block_resets_at",
)

#: The state `done` moves a handover to.
DONE = "done"

#: The subdirectory of `files/` a paused subagent writes its notes into.
AGENTS_DIR = "agents"

#: Findings `check` reports about age, not validity; `commit` does not refuse over them.
OVERDUE_RULES = frozenset({"stale", "expired"})

ID_RE = re.compile(r"(?P<prefix>[A-Z][A-Z0-9]*)-[0-9]+")

BODY_TEMPLATE = """\
# Handover — {title}

> ⏸ PAUSE IN PROGRESS — delete this line when the pause is finished. A successor who still
> sees it: the limit hit mid-pause; trust git and `files/agents/*.md` over this file.

## Goal
The owner's ask (verbatim when short) and what "done" means.

## State
Per strand: done / in progress (how far) / not started. Branch, WIP commit, anything left
uncommitted, worktrees (path · branch · commit · owning agent).

## Next action
The literal first step — a command or an edit, not a direction.

## Unfiled results
Every number, claim or decision that exists only in this chat — verbatim, with where it came
from (command, file, argument). Do not paraphrase; the successor files these.

## Open questions for the owner

<!-- Add a section only once it has content: Decisions already made (not to re-litigate) ·
Dead ends (tried, what happened, why ruled out) ·
Agents and background work (files/agents/<id>.md, task ids, output paths) · Read first (files
with line ranges, in reading order). -->
"""


@dataclass(frozen=True)
class Handover:
    """One handover record and the store it lives in."""

    store: Store
    item: Item

    @property
    def id(self) -> str:
        """The record's id, `<PREFIX>-NNNN`."""
        return self.item.id

    @property
    def title(self) -> str:
        """The record's title."""
        return str(self.item.fields["title"])

    @property
    def state(self) -> str:
        """`open` or `done`."""
        return str(self.item.fields["state"])

    @property
    def created(self) -> datetime.datetime:
        """When it was filed; raise when the record carries no timestamp."""
        created = self.item.fields.get("created")
        if not isinstance(created, datetime.datetime):
            raise TasksError(f"{self.path}: created {created!r} is not a timestamp")
        return created

    @property
    def claimed_by(self) -> str | None:
        """The session holding its lease, live or expired, or None when unclaimed."""
        holder = self.item.fields.get("claimed_by")
        return None if holder is None else str(holder)

    @property
    def path(self) -> Path:
        """The `item.md` file."""
        return self.store.root / self.item.path

    @property
    def files_dir(self) -> Path:
        """The gitignored directory beside `item.md` holding `agents/` and `wip.patch`."""
        return self.path.parent / HANDOVER_FILES_DIR

    def field(self, name: str) -> str:
        """The field `name` as text; an absent field is the empty string."""
        value = self.item.fields.get(name)
        return "" if value is None else str(value)

    def to_json(self) -> dict[str, object]:
        """The record as `handover list --json` gives it: its frontmatter (timestamps as ISO
        text), plus `path`, `files_dir`, `store` and `store_prefix`."""
        fields = {
            name: value.isoformat() if isinstance(value, datetime.date) else thaw(value)
            for name, value in self.item.fields.items()
        }
        return {
            **fields,
            "path": str(self.path),
            "files_dir": str(self.files_dir),
            "store": str(self.store.root),
            "store_prefix": prefix_of(self.store),
        }


# --- reading -----------------------------------------------------------------------------------


def store_handovers(store: Store, done: bool = False) -> list[Handover]:
    """The store's handovers, oldest first; `done` ones only when `done`."""
    found = [Handover(store, item) for item in store.items(kind=HANDOVER_KIND, closed=done)]
    return sorted(found, key=lambda h: h.created)


def local_store(cwd: Path) -> Store | None:
    """The store `new` would write to from `cwd`, when it exists."""
    root = write_root(None, False, cwd)
    return open_store(root) if is_store_root(root) else None


def indexed_handovers(done: bool = False) -> tuple[list[Handover], list[str]]:
    """Every handover of every indexed store, oldest first, and a note per index link or store
    that could not be read — reported, never raised, so one broken store hides no other."""
    index = store_index.stores()
    notes = list(index.notes)
    found: list[Handover] = []
    for entry in index.stores:
        try:
            found += store_handovers(open_store(entry.root), done)
        except (TasksError, ItemError) as exc:
            notes.append(f"{entry.root}: {exc}")
    return sorted(found, key=lambda h: h.created), notes


def _candidate_stores(ref: str, cwd: Path) -> Iterator[Store]:
    """The stores a reference is looked up in, in order, each once: the cwd's, the indexed ones
    (only the one its prefix names, for an id), the home store."""
    match = ID_RE.fullmatch(ref)
    prefix = match["prefix"] if match else None
    seen: set[Path] = set()
    roots = [write_root(None, False, cwd)]
    roots += [e.root for e in store_index.stores().stores if prefix in (None, e.prefix)]
    roots.append(home_store_root())
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved in seen or not is_store_root(resolved):
            continue
        seen.add(resolved)
        store = open_store(resolved)
        if prefix is None or prefix_of(store) == prefix:
            yield store


def find(ref: str, cwd: Path) -> Handover:
    """The handover `ref` (an id or a slug) names; raise when none does, or it is another kind."""
    for store in _candidate_stores(ref, cwd):
        matches = [i for i in store.items(closed=True) if ref in (i.id, i.slug)]
        handovers = [i for i in matches if i.kind == HANDOVER_KIND]
        if len(handovers) > 1:
            raise TasksError(f"{ref!r} names {', '.join(i.id for i in handovers)}; give the id")
        if handovers:
            return Handover(store, handovers[0])
        if matches and ID_RE.fullmatch(ref):
            owner = KIND_OWNERS.get(matches[0].kind, "claude-tasks")
            raise TasksError(f"{ref} is a {matches[0].kind} record, not a handover; use `{owner}`")
    raise TasksError(f"no handover {ref!r} in {write_root(None, False, cwd)}, the index or home")


# --- writing ------------------------------------------------------------------------------------


def _handover_store(cwd: Path) -> Store:
    """The store `new` writes to from `cwd`: created and indexed when absent, moved to the
    current schema when it still has no handover kind."""
    root = write_root(None, False, cwd)
    check_on_default_branch(root)
    store = init_store(root, None, commit=True)
    if HANDOVER_KIND not in {kind.name for kind in store.schema.kinds}:
        upgrade_store(store, dry_run=False)
        store = open_store(root)
    return store


def _free_title(store: Store, title: str, now: datetime.datetime) -> str:
    """`title`, or `title (HH:MM)` when another handover of the store already has its slug."""
    taken = {i.slug for i in store.items(kind=HANDOVER_KIND, closed=True)}
    return f"{title} ({now:%H:%M})" if changes.slugify(title) in taken else title


def _branch(cwd: Path) -> str | None:
    """The branch checked out at `cwd`, or None outside git or on a detached HEAD."""
    proc = gitio.run(cwd, "branch", "--show-current")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def new(
    cwd: Path,
    title: str,
    model: str,
    size: str,
    session: str | None = None,
    body: str | None = None,
    block_resets_at: datetime.datetime | None = None,
) -> Handover:
    """File an open handover for the work at `cwd`, its body `body` or the pause template, and
    create its `files/agents/` directory."""
    cwd = cwd.resolve()
    store = _handover_store(cwd)
    title = _free_title(store, title, store.clock())
    optional = {
        "branch": _branch(cwd),
        "block_resets_at": block_resets_at,
        "paused_session": session,
    }
    edits: list[Edit] = [Assign("model", model), Assign("size", size), Assign("cwd", str(cwd))]
    edits += [Assign(name, value) for name, value in optional.items() if value]
    text = body if body is not None else BODY_TEMPLATE.format(title=title)
    handover = Handover(store, store.add(HANDOVER_KIND, title, edits, text))
    (handover.files_dir / AGENTS_DIR).mkdir(parents=True, exist_ok=True)
    return handover


def parse_stamp(text: str) -> datetime.datetime:
    """`--block-resets-at`'s ISO text as the timestamp the field holds; raise on anything else."""
    value = item_schema.value_from_text(HANDOVER, "block_resets_at", text)
    if not isinstance(value, datetime.datetime):
        raise TasksError(f"--block-resets-at {text!r} is not an ISO timestamp")
    return value


def set_fields(ref: str, assignments: list[str], cwd: Path) -> Handover:
    """Apply `KEY=VALUE` assignments to `SETTABLE_FIELDS`, validated and committed as one write."""
    handover = find(ref, cwd)
    if not assignments:
        raise TasksError(f"give at least one KEY=VALUE; `set` takes {', '.join(SETTABLE_FIELDS)}")
    edits: list[Edit] = []
    for assignment in assignments:
        key, separator, raw = assignment.partition("=")
        if not separator:
            raise TasksError(f"{assignment!r}: expected KEY=VALUE")
        if key not in SETTABLE_FIELDS:
            raise TasksError(f"{key!r} is not settable; `set` takes {', '.join(SETTABLE_FIELDS)}")
        edits.append(Assign(key, item_schema.value_from_text(HANDOVER, key, raw)))
    check_on_default_branch(handover.store.root)
    return Handover(handover.store, handover.store.set(handover.id, edits))


def claim(ref: str, session: str, cwd: Path, force: bool = False) -> Handover:
    """Lease the handover to `session` for `HANDOVER_LEASE`, renewing its own lease; refuse while
    another session's lease is live unless `force`, which releases that lease first."""
    handover = find(ref, cwd)
    store = handover.store
    check_on_default_branch(store.root)
    if force and handover.claimed_by not in (None, session):
        store.release(handover.id, holder=session, force=True)
    return Handover(store, store.claim(handover.id, holder=session, ttl=HANDOVER_LEASE))


def release(ref: str, session: str, cwd: Path, force: bool = False) -> Handover:
    """Drop the handover's lease; only its holder may, unless `force`."""
    handover = find(ref, cwd)
    check_on_default_branch(handover.store.root)
    return Handover(
        handover.store, handover.store.release(handover.id, holder=session, force=force)
    )


def done(ref: str, closed_by: str, cwd: Path) -> Handover:
    """Close the handover once its work has landed: state `done`, `closed_by`, `closed_at`, its
    lease dropped, the record and its files moved under `handover/closed/`."""
    handover = find(ref, cwd)
    store = handover.store
    check_on_default_branch(store.root)

    def plan(item: Item) -> list[Edit]:
        """The closing edits, refused when the handover is already done."""
        if item.fields.get("state") == DONE:
            raise TasksError(f"{item.id} is already done; file a new handover for further work")
        return [
            Assign("state", DONE),
            Assign("closed_by", closed_by),
            Assign("closed_at", store.clock()),
        ]

    return Handover(store, store.revise(handover.id, plan))


def commit(ref: str, cwd: Path) -> tuple[Handover, bool]:
    """Commit the edited body of the handover's `item.md` — exactly that path — once the record
    still validates; return it and whether there was anything to commit."""
    handover = find(ref, cwd)
    root = handover.store.root
    check_on_default_branch(root)
    rel = str(handover.item.path)
    with fsio.lock(root):
        gitio.check_writable(root)
        findings = [
            f for f in handover.store.check() if f.where == rel and f.rule not in OVERDUE_RULES
        ]
        if findings:
            raise TasksError("\n".join(f"{handover.path}: {f.message}" for f in findings))
        if not gitio.checked(root, "status", "--porcelain", "--", rel).strip():
            return handover, False
        gitio.commit(
            root, [handover.path], f"{HANDOVER_KIND}: edit {handover.id} {handover.item.slug}"
        )
    return handover, True


# --- output -------------------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """A compact human duration: 45s, 12m, 3h05m or 2d04h."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    hours, minutes = divmod(total // 60, 60)
    if hours == 0:
        return f"{minutes}m"
    days, hours = divmod(hours, 24)
    return f"{hours}h{minutes:02d}m" if days == 0 else f"{days}d{hours:02d}h"


def age(handover: Handover, now: datetime.datetime) -> str:
    """How long ago the handover was filed."""
    return f"{format_duration((now - handover.created).total_seconds())} ago"


def list_lines(handover: Handover, now: datetime.datetime) -> list[str]:
    """One handover for a person: id, state, holder, model/size, age, cwd; then its title."""
    holder = f" · claimed by {handover.claimed_by}" if handover.claimed_by else ""
    return [
        f"{handover.id} · {handover.state}{holder} · {handover.field('model')}/"
        f"{handover.field('size')} · {age(handover, now)} · {handover.field('cwd')}",
        f"  {handover.title}",
    ]


def pickup_context(cwd: Path) -> str | None:
    """The additionalContext naming the unclaimed open handovers of `cwd`'s store, or None."""
    store = local_store(cwd)
    waiting = [] if store is None else [h for h in store_handovers(store) if not h.claimed_by]
    if not waiting:
        return None
    now = datetime.datetime.now().astimezone()
    lines = [
        f"  {h.id} · {h.field('model')}/{h.field('size')} · filed {age(h, now)} · {h.title}"
        for h in waiting
    ]
    return "\n".join(
        [
            f"⏸ This project has {len(waiting)} open handover(s) from an earlier /pause:",
            *lines,
            "Continue one by following the /pause skill's §Pickup section: "
            "`claude-tasks handover claim <id>`, then read `$(claude-tasks handover path <id>)`. "
            "Ignore it if the owner asked for something else.",
        ]
    )
