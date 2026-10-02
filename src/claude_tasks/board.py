"""`claude-tasks board`: one self-contained HTML page of every record, written and opened.

The board's stores are the home store plus whatever claude-tasks' configuration's `[board]` table
lists (`config.read_config`). The page embeds `views.record_object(task)` plus `body` for every
record as one JSON script block; `board.html` (packaged as data alongside this module) is the
static template that renders it, so nothing here knows how a card or the filters look.
"""

import datetime
import json
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

from itemctl.store import Store

from claude_tasks.config import read_config
from claude_tasks.errors import TasksError
from claude_tasks.store import Task, is_store_root, open_store, prefix_of
from claude_tasks.views import record_object

#: The token `board.html` carries once for the records array, and once for the header metadata.
RECORDS_TOKEN = "/*__CLAUDE_TASKS_BOARD_RECORDS__*/"
META_TOKEN = "/*__CLAUDE_TASKS_BOARD_META__*/"

#: Where `--out` writes when not given: `$XDG_CACHE_HOME/claude-tasks/board.html`, falling back
#: to `~/.cache` the way the XDG base-directory spec does when the variable is unset or empty.
BOARD_SUBPATH = Path("claude-tasks") / "board.html"


def board_stores(home: Store) -> list[Store]:
    """`home` plus every store the `[board]` configuration lists, opened; raise naming a path
    that is not itself a claude-tasks store (`store.is_store_root`)."""
    stores = [home]
    for path in read_config().board_stores:
        if not is_store_root(path):
            raise TasksError(
                f"{path}: [board] stores names it, but it is not a claude-tasks store "
                "(a `.claude-tasks/` directory or the home store, holding a store.toml)"
            )
        stores.append(open_store(path))
    return stores


def board_payload(tasks: list[Task]) -> str:
    """The board's records as one JSON array: `record_object(task)` plus `body`, per task."""
    return json.dumps(
        [{**record_object(task), "body": task.body} for task in tasks], ensure_ascii=False
    )


def _escape_for_script(payload: str) -> str:
    """`payload` (already-serialized JSON) with `<` escaped so it cannot close or comment out of
    the `<script>` block it is embedded in (`</script>`, `<!--`); `\\u003c` reads back as `<`."""
    return payload.replace("<", "\\u003c")


def _substitute(template: str, token: str, value: str) -> str:
    """`template` with the single occurrence of `token` replaced by `value`; raise otherwise."""
    count = template.count(token)
    if count != 1:
        raise TasksError(
            f"board.html: placeholder {token!r} appears {count} time(s); expected exactly 1"
        )
    return template.replace(token, value)


def render_board(tasks: list[Task], stores: list[Store], generated: datetime.datetime) -> str:
    """The complete board page: the packaged template with the records and header data filled in."""
    template = resources.files("claude_tasks").joinpath("board.html").read_text(encoding="utf-8")
    meta = {
        "generated": generated.isoformat(),
        "stores": [{"prefix": prefix_of(store), "path": str(store.root)} for store in stores],
    }
    html = _substitute(template, RECORDS_TOKEN, _escape_for_script(board_payload(tasks)))
    return _substitute(html, META_TOKEN, _escape_for_script(json.dumps(meta, ensure_ascii=False)))


def default_out_path() -> Path:
    """`--out`'s default, `$XDG_CACHE_HOME/claude-tasks/board.html`, else `~/.cache/...`."""
    raw = os.environ.get("XDG_CACHE_HOME")
    base = Path(raw).expanduser() if raw else Path.home() / ".cache"
    return base / BOARD_SUBPATH


def write_board(html: str, out: Path) -> None:
    """Write `html` to `out`, creating its parent directories."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def open_board(path: Path) -> None:
    """Open `path` with a detached `xdg-open`; raise naming `--no-open` when it is not on PATH."""
    xdg_open = shutil.which("xdg-open")
    if xdg_open is None:
        raise TasksError("xdg-open is not on PATH; pass --no-open to skip opening the board")
    subprocess.Popen(
        [xdg_open, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
