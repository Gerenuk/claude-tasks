"""The `claude-tasks` command line: add · list · show · set · check · board · hook · upgrade ·
handover.

The CLI writes records and guards them; it does not query them. `list` is a dump and not a query —
one NDJSON object per record with `--json`, no filter of any kind — because `duckdb` and `jq` do
selection better than any flag here could, and it carries both roots at once so a repo's ideas and
the home store's read as one queue that id prefixes tell apart. `set` is the only way a record
changes state: one shape, `set ID [ID …] state=… field=… [--note …]` (several ids are one commit), with the new state's required
fields enforced by the validator in that same call. Every one of them reads and writes ideas only;
a store's friction entries and handovers belong to the commands `schema.KIND_OWNERS` names.
`upgrade` is the one command about a store rather than its records: it moves the store to the
current schema. `handover` is the handover kind's writer (`handover.py`), the /pause skill's
interface; its `list --json` is one JSON document, not NDJSON.
"""

import argparse
import datetime
import json
import os
import signal
import sys
from pathlib import Path

from itemctl.model import ItemError
from itemctl.store import Store

from claude_tasks import handover
from claude_tasks.board import (
    board_stores,
    default_out_path,
    open_board,
    render_board,
    write_board,
)
from claude_tasks.env import home_store_root
from claude_tasks.errors import OffDefaultBranch, TasksError
from claude_tasks.hooks import run_context_hook
from claude_tasks.lookup import find_task, legacy_notice
from claude_tasks.ops import (
    Written,
    add_task,
    init_store,
    parse_value,
    set_many,
    upgrade_store,
)
from claude_tasks.schema import ADD_STATES, HANDOVER_MODELS, HANDOVER_SIZES
from claude_tasks.store import (
    Task,
    load_tasks,
    open_store,
    view_stores,
    worktree_notice,
    write_root,
)
from claude_tasks.validate import check_store
from claude_tasks.views import front_door_lines, list_line, ndjson_line, record_object

#: The state add files a record in when --state is not given.
DEFAULT_ADD_STATE = "open"


def emit(written: Written, store: Store) -> None:
    """Print a mutation's `<ID>  <path>` line, and on stderr where it went when that is the main
    checkout's store reached from a linked worktree."""
    print(written.line())
    notice = worktree_notice(store.root, Path.cwd())
    if notice is not None:
        print(notice, file=sys.stderr)


def store_of(args: argparse.Namespace) -> Store:
    """The one store a mutation writes to, created on first use (the only two stores that are)."""
    return init_store(write_root(args.store, args.home, Path.cwd()), None, commit=True)


def stores_of(args: argparse.Namespace) -> list[Store]:
    """What a read sees: this repo's store when it has one plus the home store, or the pinned one."""
    return view_stores(args.store, args.home, Path.cwd())


def tasks_of_view(args: argparse.Namespace) -> list[Task]:
    """Every record of the view, the repo store's first."""
    return [task for store in stores_of(args) for task in load_tasks(store)]


def task_of(args: argparse.Namespace, ref: str) -> Task:
    """The record a reference names, reporting on stderr when the ref is an id it used to have."""
    task = find_task(stores_of(args), ref)
    notice = legacy_notice(task, ref)
    if notice is not None:
        print(notice, file=sys.stderr)
    return task


# --- handlers --------------------------------------------------------------------------------


def read_body(args: argparse.Namespace) -> str:
    """The body `--body-file` names, or stdin when it is `-`."""
    if args.body_file == "-":
        return sys.stdin.read()
    path = Path(args.body_file)
    if not path.is_file():
        raise TasksError(f"--body-file {path} does not exist")
    return path.read_text(encoding="utf-8")


def add_fields(args: argparse.Namespace) -> dict[str, object]:
    """The frontmatter fields `--field K=V` sets, beyond the ones add writes itself.

    Every field but the managed ones goes through here: `id`, `title`, `state`, `created` and
    `type` are written by `add` itself, `legacy_ids` comes from `--legacy-id`, and whatever else a
    writer wants is carried on the record.
    """
    fields: dict[str, object] = {}
    for token in args.field:
        if "=" not in token:
            raise TasksError(f"--field {token!r}: expected field=value")
        name, raw = token.split("=", 1)
        if name in fields:
            raise TasksError(f"--field {name!r} is given twice")
        if name == "legacy_ids":
            raise TasksError("--field legacy_ids: pass each old id as --legacy-id ID instead")
        value = parse_value(name, raw)
        if value is None:
            raise TasksError(f"--field {token!r}: add writes no null fields; omit it")
        fields[name] = value
    return fields


def cmd_add(args: argparse.Namespace) -> int:
    """add: file a new record, creating this repo's store or the home store on first use."""
    store = store_of(args)
    state = args.state if args.state is not None else DEFAULT_ADD_STATE
    written = add_task(store, args.title, state, add_fields(args), read_body(args), args.legacy_id)
    emit(written, store)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """list: dump every record of the view, as NDJSON or as one line each."""
    write = ndjson_line if args.json else list_line
    for task in tasks_of_view(args):
        print(write(task))
    return 0


def cmd_front_door(args: argparse.Namespace) -> int:
    """No subcommand: what is waiting for him, and how to start one.

    This is the one command written for a person rather than a consumer, which is why it shows
    `ready` and `open` only and says what to type next. Everything machine-readable is `list`.
    """
    for line in front_door_lines(tasks_of_view(args), stores_of(args)):
        print(line)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """show: the record a reference names, through either root and through legacy_ids.

    `--json` prints `list --json`'s object for that record plus its `body`, one line.
    """
    task = task_of(args, args.ref)
    if args.json:
        print(json.dumps({**record_object(task), "body": task.body}, ensure_ascii=False))
    else:
        sys.stdout.write(task.path.read_text(encoding="utf-8"))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    """set: fields, states and notes of one or more records, validated together before anything is
    written and committed once."""
    refs = [token for token in args.tokens if "=" not in token]
    assignments = [token for token in args.tokens if "=" in token]
    if not refs:
        raise TasksError("set needs an id before the FIELD=VALUE assignments")
    tasks = [task_of(args, ref) for ref in refs]
    store = tasks[0].store
    if any(task.store.root != store.root for task in tasks):
        raise TasksError(
            "set takes ids of one store: "
            + ", ".join(f"{t.id} is in {t.store.root}" for t in tasks)
        )
    ids = list(dict.fromkeys(task.id for task in tasks))
    for written in set_many(store, ids, assignments, args.note):
        emit(written, store)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """check: validate every store of the view."""
    errors = [e for store in stores_of(args) for e in check_store(store)]
    for error in errors:
        print(error)
    if errors:
        print(f"{len(errors)} error(s) found", file=sys.stderr)
        return 1
    print("ok")
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    """board: render every record of the board's stores into one HTML page, then open it."""
    home = open_store(home_store_root())
    stores = board_stores(home)
    tasks = [task for store in stores for task in load_tasks(store)]
    html = render_board(tasks, stores, datetime.datetime.now(tz=datetime.UTC).astimezone())
    out = Path(args.out).expanduser() if args.out is not None else default_out_path()
    write_board(html, out)
    print(out)
    if not args.no_open:
        open_board(out)
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    """upgrade: move the store a write would use (or the pinned one) to the current schema."""
    store = open_store(write_root(args.store, args.home, Path.cwd()))
    print(upgrade_store(store, args.dry_run).line())
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    """hook: answer one Claude Code hook payload from stdin."""
    response = run_context_hook(args.event, sys.stdin.read())
    print(json.dumps(response, ensure_ascii=False))
    return 0


# --- handover ------------------------------------------------------------------------------


def session_of(args: argparse.Namespace) -> str:
    """`--session`, else the calling Claude session's id; raise when neither names one."""
    session = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session:
        raise TasksError("no CLAUDE_CODE_SESSION_ID in the environment; pass --session ID")
    return session


def cmd_handover_new(args: argparse.Namespace) -> int:
    """handover new: file a handover; print its id, its item.md and its files directory."""
    stamp = args.block_resets_at
    written = handover.new(
        Path(args.cwd) if args.cwd else Path.cwd(),
        args.title,
        args.model,
        args.size,
        session=args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
        body=read_body(args) if args.body_file else None,
        block_resets_at=None if stamp is None else handover.parse_stamp(stamp),
    )
    print(written.id)
    print(written.path)
    print(written.files_dir)
    return 0


def cmd_handover_list(args: argparse.Namespace) -> int:
    """handover list: this store's open handovers, or every indexed store's with --all."""
    notes: list[str] = []
    if args.all:
        found, notes = handover.indexed_handovers(args.done)
        where = "the store index"
    else:
        store = handover.local_store(Path.cwd())
        found = [] if store is None else handover.store_handovers(store, args.done)
        where = str(write_root(None, False, Path.cwd()))
    if args.json:
        report = {"handovers": [h.to_json() for h in found], "notes": notes}
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    now = datetime.datetime.now().astimezone()
    for found_one in found:
        print("\n".join(handover.list_lines(found_one, now)))
    for note in notes:
        print(f"note: {note}")
    if not found and not notes:
        print(f"no {'' if args.done else 'open '}handovers in {where}")
    return 0


def cmd_handover_path(args: argparse.Namespace) -> int:
    """handover path: the handover's item.md, or its files directory with --files."""
    found = handover.find(args.ref, Path.cwd())
    print(found.files_dir if args.files else found.path)
    return 0


def cmd_handover_set(args: argparse.Namespace) -> int:
    """handover set: change scalar fields; state and lease move only through their own verbs."""
    print(handover.set_fields(args.ref, args.assignments, Path.cwd()).path)
    return 0


def cmd_handover_claim(args: argparse.Namespace) -> int:
    """handover claim: lease the handover to this session before resuming it."""
    print(handover.claim(args.ref, session_of(args), Path.cwd(), force=args.force).path)
    return 0


def cmd_handover_release(args: argparse.Namespace) -> int:
    """handover release: drop this session's lease so another may pick the handover up."""
    print(handover.release(args.ref, session_of(args), Path.cwd(), force=args.force).path)
    return 0


def cmd_handover_done(args: argparse.Namespace) -> int:
    """handover done: close the handover once its work has landed."""
    print(handover.done(args.ref, args.closed_by, Path.cwd()).path)
    return 0


def cmd_handover_commit(args: argparse.Namespace) -> int:
    """handover commit: commit the edited item.md, and nothing else."""
    found, committed = handover.commit(args.ref, Path.cwd())
    if not committed:
        print(f"{found.path}: nothing to commit", file=sys.stderr)
    print(found.path)
    return 0


def add_handover_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The `handover` subcommand and its verbs."""
    top = sub.add_parser("handover", help="the /pause skill's handovers (the handover kind)")
    verbs = top.add_subparsers(dest="verb", required=True, metavar="VERB")

    p = verbs.add_parser("new", help="file a handover in the store a write from --cwd goes to")
    p.add_argument("--title", required=True, help="what the work is, in a few words")
    p.add_argument("--model", required=True, choices=HANDOVER_MODELS, help="the successor's tier")
    p.add_argument("--size", required=True, choices=HANDOVER_SIZES, help="work left")
    p.add_argument("--cwd", metavar="DIR", help="the project directory; default: the current one")
    p.add_argument("--session", metavar="ID", help="default: $CLAUDE_CODE_SESSION_ID")
    p.add_argument(
        "--body-file", metavar="PATH", help="the body (- reads stdin); default: template"
    )
    p.add_argument("--block-resets-at", metavar="ISO", help="when the usage block resets")
    p.set_defaults(func=cmd_handover_new)

    p = verbs.add_parser("list", help="this store's open handovers, or every one with --all")
    p.add_argument("--all", action="store_true", help="every indexed store's handovers")
    p.add_argument("--done", action="store_true", help="include the ones already done")
    p.add_argument("--json", action="store_true", help="one JSON document, for another tool")
    p.set_defaults(func=cmd_handover_list)

    p = verbs.add_parser("path", help="print the handover's item.md, or its files dir")
    p.add_argument("ref", metavar="ID")
    p.add_argument("--files", action="store_true", help="the directory holding agents/, wip.patch")
    p.set_defaults(func=cmd_handover_path)

    p = verbs.add_parser("set", help=f"change {', '.join(handover.SETTABLE_FIELDS)}")
    p.add_argument("ref", metavar="ID")
    p.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_handover_set)

    for verb, text, force in (
        ("claim", "lease the handover to this session", "release another session's lease first"),
        ("release", "drop this session's lease", "drop another session's lease"),
    ):
        p = verbs.add_parser(verb, help=text)
        p.add_argument("ref", metavar="ID")
        p.add_argument("--session", metavar="ID", help="default: $CLAUDE_CODE_SESSION_ID")
        p.add_argument("--force", action="store_true", help=f"{force} (on the owner's word only)")
        p.set_defaults(func=cmd_handover_claim if verb == "claim" else cmd_handover_release)

    p = verbs.add_parser("done", help="close the handover once its work has landed")
    p.add_argument("ref", metavar="ID")
    p.add_argument("--closed-by", required=True, metavar="REF", help="the commit or path")
    p.set_defaults(func=cmd_handover_done)

    p = verbs.add_parser("commit", help="commit the edited item.md (a pathspec commit)")
    p.add_argument("ref", metavar="ID")
    p.set_defaults(func=cmd_handover_commit)


# --- parser ----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for every subcommand."""
    store_opts = argparse.ArgumentParser(add_help=False)
    which = store_opts.add_mutually_exclusive_group()
    which.add_argument("--store", metavar="PATH", help="use the existing store at PATH")
    which.add_argument("--home", action="store_true", help="use the home store")
    # A command that takes --home can write there instead; main() says so on an OffDefaultBranch.
    store_opts.set_defaults(home_way_out=True)

    parser = argparse.ArgumentParser(prog="claude-tasks", description="Idea records in git.")
    # No subcommand is the front door, not an error and not `list`: `claude-tasks` alone is what he
    # types to see what is waiting. Every subcommand overrides `func` with its own handler.
    parser.set_defaults(func=cmd_front_door, store=None, home=False, home_way_out=False)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("add", parents=[store_opts], help="file a new record")
    p.add_argument("--title", required=True)
    p.add_argument("--state", choices=ADD_STATES)
    p.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="K=V",
        help="any other frontmatter field, carried through unvalidated",
    )
    p.add_argument(
        "--legacy-id",
        action="append",
        default=[],
        metavar="ID",
        help="an id the record carried before, kept in legacy_ids; repeat for several",
    )
    p.add_argument(
        "--body-file", required=True, metavar="PATH", help="the record body; - reads stdin"
    )
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", parents=[store_opts], help="dump every record of the view")
    p.add_argument("--json", action="store_true", help="one JSON object per line (NDJSON)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", parents=[store_opts], help="print a record, body included")
    p.add_argument("ref", metavar="ID")
    p.add_argument(
        "--json",
        action="store_true",
        help="one JSON object: the record as `list --json` dumps it, plus `body`",
    )
    p.set_defaults(func=cmd_show)

    p = sub.add_parser(
        "set",
        parents=[store_opts],
        help="set fields, states and notes of one or more records, in one commit",
        usage="claude-tasks set [-h] [--store PATH | --home] ID [ID ...] "
        "[FIELD=VALUE ...] [--note TEXT]",
    )
    # An id never holds `=` and an assignment always does, so one list holds both and cmd_set
    # splits it; argparse cannot tell two adjacent positional lists apart.
    p.add_argument(
        "tokens",
        nargs="+",
        metavar="ID|FIELD=VALUE",
        help="one or more ids, then the FIELD=VALUE changes applied to each of them",
    )
    p.add_argument("--note", metavar="TEXT", help="append a dated line to ## History")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("check", parents=[store_opts], help="validate the stores of the view")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("board", help="render every record into one HTML page and open it")
    p.add_argument("--out", metavar="PATH", help="write the board here instead of the cache path")
    p.add_argument("--no-open", action="store_true", help="do not open the board after writing it")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("upgrade", parents=[store_opts], help="move a store to the current schema")
    p.add_argument("--dry-run", action="store_true", help="say what would change; write nothing")
    p.set_defaults(func=cmd_upgrade)

    add_handover_parser(sub)

    p = sub.add_parser("hook", help="answer a Claude Code hook payload on stdin")
    p.add_argument("event", choices=("session-start",))
    p.set_defaults(func=cmd_hook)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one claude-tasks command; a refusal becomes one stderr line and exit status 1."""
    args_list = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = None
    try:
        args, extras = parser.parse_known_args(args_list)
        # argparse stops filling a positional list at the first option, so `set ID --store X
        # FIELD=VALUE` leaves the assignments over; they belong to set's tokens.
        if extras and args.func is cmd_set and not any(e.startswith("-") for e in extras):
            args.tokens += extras
        elif extras:
            parser.error(f"unrecognized arguments: {' '.join(extras)}")
        return args.func(args)
    except OffDefaultBranch as exc:
        way_out = ", or file in the home store with --home" if args and args.home_way_out else ""
        print(f"claude-tasks: {exc}{way_out}", file=sys.stderr)
        return 1
    except (TasksError, ItemError) as exc:
        print(f"claude-tasks: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # A reader that stops early -- `list --json | head`, a duckdb query that takes what it
        # needs -- closes the pipe under us. That is the consumer working, not an anomaly, so it
        # gets the status a SIGPIPE death would have given rather than a traceback. stdout is
        # redirected to devnull first: without it the interpreter flushes the dead pipe at exit
        # and prints its own "Exception ignored" epilogue to stderr, which is the noise we came
        # to remove.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 128 + signal.SIGPIPE


if __name__ == "__main__":  # `python -m claude_tasks.cli` must not import-and-exit in silence
    sys.exit(main(sys.argv[1:]))
