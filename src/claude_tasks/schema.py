"""What a claude-tasks store is: the `store.toml` every store carries, and the domain rules itemctl
does not know.

A store holds these kinds of record, each with one writer:

- `idea` (at the store root) — this CLI's `add`/`set`, the only kind `list`/`show`/`set` touch;
- `friction` (under `friction/`) — `claude-ctl friction`; its block is `friction_kind.kind_toml`.
  Declared only by a build that ships `friction_kind.py` (`FRICTION`): the public build omits
  that module, and its stores hold ideas and handovers alone;
- `handover` (under `handover/<slug>/item.md`, the session's files beside it in `files/`) —
  `claude-tasks handover`.

`store_toml(prefix)` is the one site of the store schema — states, what each requires, the fields
only one state holds, `outcome`'s values. Every store is created (`ops.init_store`) with this text,
and `SCHEMA` parses the same text, so the CLI's own tables (`ADD_STATES`, the list fields) are
derived from it rather than restated. itemctl validates every write and `check` against the
store's copy. `STORE_TOML_V1` is the frozen text of the idea-only schema, the one text `upgrade`
rewrites.

Every other key a record carries passes through untouched: read as it stands, written back after
the declared fields, dumped verbatim.
"""

import importlib.util
import re
import tomllib
from types import ModuleType

from itemctl import schema as item_schema
from itemctl.model import Kind, Schema

from claude_tasks.errors import TasksError

#: The friction kind (`friction_kind`), or None in a build without it. This is the one place the
#: kind is optional; `store_toml`, `KIND_OWNERS` and `store.open_store`'s rules follow it.
FRICTION: ModuleType | None = (
    importlib.import_module("claude_tasks.friction_kind")
    if importlib.util.find_spec("claude_tasks.friction_kind")
    else None
)

#: The kind this CLI's `add`/`list`/`show`/`set` read and write.
KIND = "idea"

#: The handover kind, and the directory its records live under.
HANDOVER_KIND = "handover"
HANDOVER_DIR = "handover"

#: The directory beside a handover's `item.md` holding the session's files; gitignored.
HANDOVER_FILES_DIR = "files"

#: Which command reads and writes each kind that is not an idea; `set`/`show` name it on refusal.
KIND_OWNERS: dict[str, str] = {HANDOVER_KIND: "claude-tasks handover"}
if FRICTION is not None:
    KIND_OWNERS[FRICTION.KIND] = "claude-ctl friction"

#: The only record type; `add` writes it into `type`, which is immutable afterwards.
TYPE = "Idea"

#: The one documented value of `reason` meaning "no reason was recorded". The 2026-09-17 cutover
#: wrote it wherever a record closed long ago carried no reason of its own, and
#: `docs/json-contract.md` names it as *the* string a consumer matches to find those records. It is
#: one literal and not a set, so no later writer can change it without changing that query.
PLACEHOLDER_REASON = "no reason was recorded before the 2026-09-17 rewrite"

#: Fields `set` must never change: a record's identity and when it was filed.
IMMUTABLE_FIELDS: frozenset[str] = frozenset({"id", "type", "created", "legacy_ids"})

#: The section `set --note` appends its dated line to: what the fields do not say, how it got here.
HISTORY_SECTION = "History"

#: A store's id prefix: uppercase, digits allowed after the first letter, because a new store's
#: prefix is its repo directory name stripped of everything else (`QM-Theory` → `QMTHEORY`).
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]*$")

#: Every store's id width: `CT-0183`.
ID_WIDTH = 4

#: The exact `store.toml` of the idea-only schema (v1), `{prefix}` standing for the id prefix.
#: Frozen: a store carrying this text is what `ops.upgrade_store` rewrites to `store_toml`, and
#: any other text it refuses.
STORE_TOML_V1 = """\
format_version = 1
id_prefix = "{prefix}"
id_width = 4
git_commit = true
writer = "claude-tasks"

[kinds.idea]
dir = "."
sections = ["History"]

[kinds.idea.fields]
title = "str"
decided_by = "str"
reason = "str"
trigger = "str"
blocked_by = "list"
outcome = ["built", "dropped", "superseded"]
legacy_ids = "list"

[kinds.idea.states]
open = {{ requires = ["title"] }}
ready = {{ requires = ["title", "decided_by", "reason"] }}
parked = {{ requires = ["title"], requires_any = ["trigger", "blocked_by"], only_here = ["trigger", "blocked_by"] }}
closed = {{ requires = ["title", "outcome", "reason"], only_here = ["outcome"], terminal = true }}
"""

_HEADER_TOML = """\
format_version = 1
id_prefix = "{prefix}"
id_width = {width}
git_commit = true
writer = "claude-tasks"
"""

_IDEA_TOML = """\
[kinds.idea]
dir = "."
sections = ["{history}"]

[kinds.idea.fields]
title = "str"
decided_by = "str"
reason = "str"
trigger = "str"
blocked_by = "list"
outcome = ["built", "dropped", "superseded"]
legacy_ids = "list"

[kinds.idea.states]
open = {{ requires = ["title"] }}
ready = {{ requires = ["title", "decided_by", "reason"] }}
parked = {{ requires = ["title"], requires_any = ["trigger", "blocked_by"], only_here = ["trigger", "blocked_by"] }}
closed = {{ requires = ["title", "outcome", "reason"], only_here = ["outcome"], terminal = true }}
"""

_HANDOVER_TOML = """\
[kinds.handover]
dir = "{directory}"
attachments = true

[kinds.handover.fields]
title = "str"
model = ["haiku", "sonnet", "opus", "fable"]
size = ["P", "S", "M", "L"]
cwd = "str"
branch = "str"
wip_commit = "str"
block_resets_at = "datetime"
paused_session = "str"
closed_by = "str"
closed_at = "datetime"

[kinds.handover.states]
open = {{ requires = ["title", "model", "size", "cwd"] }}
done = {{ requires = ["title", "model", "size", "cwd", "closed_by", "closed_at"], only_here = ["closed_by", "closed_at"], terminal = true }}
"""


def check_prefix(prefix: object) -> str:
    """Return `prefix` when it matches PREFIX_RE; raise naming it otherwise."""
    if not isinstance(prefix, str) or not PREFIX_RE.match(prefix):
        raise TasksError(f"prefix {prefix!r} is not {PREFIX_RE.pattern!r}")
    return prefix


def store_toml(prefix: str) -> str:
    """The `store.toml` text of a claude-tasks store with this id prefix: its header, then the
    idea, friction (when `FRICTION` is shipped) and handover kinds."""
    blocks = [
        _HEADER_TOML.format(prefix=check_prefix(prefix), width=ID_WIDTH),
        _IDEA_TOML.format(history=HISTORY_SECTION),
    ]
    if FRICTION is not None:
        blocks.append(FRICTION.kind_toml(FRICTION.DIR))
    blocks.append(_HANDOVER_TOML.format(directory=HANDOVER_DIR))
    return "\n".join(blocks)


def store_toml_v1(prefix: str) -> str:
    """The v1 `store.toml` text a store with this id prefix carried before the upgrade."""
    return STORE_TOML_V1.format(prefix=check_prefix(prefix))


#: The store schema as itemctl parses it: the source of every table below.
SCHEMA: Schema = item_schema.parse(tomllib.loads(store_toml("CT")), "claude_tasks.schema")

#: The idea kind.
IDEA: Kind = SCHEMA.kind(KIND)

#: The handover kind.
HANDOVER: Kind = SCHEMA.kind(HANDOVER_KIND)

#: States `add` may file a record in: every non-terminal state, initial state first.
ADD_STATES: tuple[str, ...] = tuple(s.name for s in IDEA.states if not s.terminal)

#: Declared list fields: `set FIELD=a,b` splits their value on commas.
LIST_FIELDS: frozenset[str] = frozenset(f.name for f in IDEA.fields if f.type == "list")


def _choices(kind: Kind, name: str) -> tuple[str, ...]:
    """The values the enum field `name` of `kind` allows; raise when it is no enum field."""
    fld = kind.field(name)
    if fld is None or not fld.choices:
        raise TasksError(f"kind {kind.name!r} declares no enum field {name!r}")
    return fld.choices


#: The model tiers a handover names for its successor, and the sizes of the work left.
HANDOVER_MODELS: tuple[str, ...] = _choices(HANDOVER, "model")
HANDOVER_SIZES: tuple[str, ...] = _choices(HANDOVER, "size")
