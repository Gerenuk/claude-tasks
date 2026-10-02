"""The two ways `list` writes a record: one NDJSON line, or one line for a human.

`list` is a dump and not a query — it filters nothing, so there is no filter here. `record_object`
is the one site building a record's dumped shape (`docs/json-contract.md`: the frontmatter
verbatim but `created` as its date, plus `store_prefix`, `store` and `path`), which `ndjson_line` serializes and `board.py` embeds
alongside the body — so a consumer grouping a mixed queue by store never has to parse an id to
learn which root it came from, and the board never drifts from the JSON contract.
"""

import datetime
import json

from itemctl.model import thaw
from itemctl.store import Store

from claude_tasks.errors import TasksError
from claude_tasks.store import Task, prefix_of


def _json_ready(value: object) -> object:
    """`value` made safe for `json.dumps` with no `default=`: `thaw` turns the frontmatter's frozen
    mappings and tuples into plain dicts and lists, a date or datetime becomes its ISO string, and
    anything else json cannot encode is named rather than dropped from the dump."""
    value = thaw(value)
    if isinstance(value, dict):
        return {key: _json_ready(v) for key, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if isinstance(value, datetime.date | datetime.datetime):
        return value.isoformat()
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TasksError(
        f"cannot dump {value!r} ({type(value).__name__}) as JSON; frontmatter takes strings, "
        "numbers, booleans, lists and mappings"
    )


def record_object(task: Task) -> dict[str, object]:
    """One record as a JSON-ready mapping: its frontmatter verbatim, then where it came from.

    `created` is the one key not dumped verbatim: a timestamp is dumped as its own calendar date,
    the contract's `YYYY-MM-DD`, which is what every consumer parses. The one site both `ndjson_line` and the board build
    their record from, so the two never drift on what a record dump contains.
    """
    obj: dict[str, object] = {key: _json_ready(v) for key, v in task.fm.items()}
    created = task.fm.get("created")
    if isinstance(created, datetime.datetime):
        obj["created"] = created.date().isoformat()
    obj["store_prefix"] = prefix_of(task.store)
    obj["store"] = str(task.store.root)
    obj["path"] = str(task.path)
    return obj


def ndjson_line(task: Task) -> str:
    """One `list --json` line: `record_object(task)`, dumped as a single line."""
    return json.dumps(record_object(task), ensure_ascii=False)


def list_line(task: Task) -> str:
    """One plain `list` row for a human at a terminal; not a contract, unlike `ndjson_line`."""
    return f"{task.id:<12} {task.state:<8} {task.title}".rstrip()


#: What the front door shows, in the order it shows them, with the heading each gets. `closed` and
#: `parked` are absent on purpose: the front door answers "what is waiting for me", and a parked
#: record is waiting for its trigger, not for him.
FRONT_DOOR_STATES: tuple[tuple[str, str], ...] = (
    ("ready", "the route is chosen; these are waiting to be built"),
    ("open", "these still need a route"),
)

#: How many records of one state the front door prints before summarising the rest. A front door
#: that prints forty lines is a list, and `list` is already that: this one has to be readable at a
#: glance or it answers nothing.
FRONT_DOOR_MAX = 8


def _oldest_first(tasks: list[Task]) -> list[Task]:
    """Longest-waiting first, which is the order `/ideas` works a queue in; ties keep id order.

    One missing a timestamp sorts last rather than raising, since the front door is a glance and
    `check` is what judges a record.
    """

    def key(task: Task) -> datetime.date:
        """The record's filing date, or the latest date when it has no timestamp."""
        created = task.fm.get("created")
        return created.date() if isinstance(created, datetime.datetime) else datetime.date.max

    return sorted(tasks, key=key)


def front_door_lines(tasks: list[Task], stores: list[Store]) -> list[str]:
    """The bare-`claude-tasks` view: what is waiting, oldest first, and how to start one.

    Written for a person reading a terminal, so it is not a contract and nothing parses it -- a
    consumer wanting these records reads `list --json` and selects there.
    """
    waiting = {state: [t for t in tasks if t.state == state] for state, _ in FRONT_DOOR_STATES}
    counts = " - ".join(f"{len(waiting[state])} {state}" for state, _ in FRONT_DOOR_STATES)
    lines = [f"{counts}   in {', '.join(str(store.root) for store in stores)}"]
    if not any(waiting.values()):
        lines += [
            "",
            "Nothing is waiting. `claude-tasks list` shows parked and closed records too.",
        ]
        return lines
    for state, heading in FRONT_DOOR_STATES:
        chosen = _oldest_first(waiting[state])
        if not chosen:
            continue
        lines += ["", f"{state} -- {heading}"]
        lines += [f"  {task.id:<12} {task.title}" for task in chosen[:FRONT_DOOR_MAX]]
        if len(chosen) > FRONT_DOOR_MAX:
            lines.append(f"  ... and {len(chosen) - FRONT_DOOR_MAX} more, oldest shown first")
    lines += [
        "",
        "Start one:",
        "  claude-tasks show <ID>     read the whole record, routes and history included",
        "  claude-tasks set <ID> ...  record a decision or a state change",
        "  /ideas                     work the whole queue with the asking ritual",
    ]
    return lines
