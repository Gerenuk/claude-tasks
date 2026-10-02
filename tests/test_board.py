"""`claude-tasks board`: the `[board]` config table, its store list, and the rendered page.

The oracle for what the page embeds is independent of `board.board_payload`: `_extract_records`
parses the rendered HTML's `<script id="records">` block with `html.parser` (which stops at the
first literal `</script>`, the way a browser's own CDATA parsing would), then `json.loads`s it --
never by calling the production JSON-building code a second time. The expected side is built from
`views.record_object(task)` plus `task.body`, read straight off the `Task` objects.
"""

import datetime
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import idea, run_cli
from itemctl.store import Store

from claude_tasks.board import board_stores, render_board
from claude_tasks.config import parse_config, read_config
from claude_tasks.env import user_config_path
from claude_tasks.errors import TasksError
from claude_tasks.ops import add_task, init_store
from claude_tasks.store import load_tasks
from claude_tasks.views import record_object

#: Never read; `parse_config` only names it in error messages.
CONFIG_SOURCE = Path("<test>/config.toml")


class _ScriptBlocks(HTMLParser):
    """Every `<script type="application/json" id="...">` block's raw text, by its id -- stopping
    at the first literal `</script>` the way a browser's own CDATA parsing would."""

    def __init__(self) -> None:
        super().__init__()
        self._capture: str | None = None
        self.blocks: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Start capturing when a JSON `<script id=...>` block opens."""
        if tag != "script":
            return
        attr_map = dict(attrs)
        block_id = attr_map.get("id")
        if attr_map.get("type") == "application/json" and block_id:
            self._capture = block_id
            self.blocks[block_id] = ""

    def handle_data(self, data: str) -> None:
        """Accumulate raw text while inside a captured block."""
        if self._capture is not None:
            self.blocks[self._capture] += data

    def handle_endtag(self, tag: str) -> None:
        """Stop capturing at the block's closing `</script>`."""
        if tag == "script":
            self._capture = None


def _script_blocks(html_text: str) -> dict[str, str]:
    """Every JSON `<script id=...>` block of `html_text`, by its id."""
    parser = _ScriptBlocks()
    parser.feed(html_text)
    return parser.blocks


def _extract_records(html_text: str) -> list[dict[str, object]]:
    """The board's embedded `records` JSON array, parsed out of the rendered page's own markup."""
    blocks = _script_blocks(html_text)
    assert "records" in blocks, 'no <script id="records"> block found in the rendered page'
    return json.loads(blocks["records"])


def _set_board_config(paths: list[Path]) -> None:
    """Write claude-tasks' configuration with a `[board]` table listing `paths`, the way a person
    would; nothing in claude-tasks writes this file."""
    quoted = ", ".join(f'"{p}"' for p in paths)
    config = user_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"[board]\nstores = [{quoted}]\n", encoding="utf-8")


# --- config parsing ----------------------------------------------------------------------------


def test_absent_config_is_an_empty_configuration(isolated_env: Path) -> None:
    """No configuration file: the board shows the home store alone."""
    assert not user_config_path().exists()
    assert read_config().board_stores == ()


def test_parse_config_rejects_a_store_setting(isolated_env: Path) -> None:
    """A store's own settings live in its store.toml; `prefix` here is an unknown key."""
    with pytest.raises(TasksError, match="unknown key"):
        parse_config('prefix = "CT"\n', CONFIG_SOURCE)


def test_parse_config_rejects_a_non_table_board(isolated_env: Path) -> None:
    """`board` must be a table; a bare string is refused, naming `board`."""
    with pytest.raises(TasksError, match="board"):
        parse_config('board = "x"\n', CONFIG_SOURCE)


def test_parse_config_rejects_an_unknown_key_in_board(isolated_env: Path) -> None:
    """`board`'s only key is `stores`; an extra key is refused, naming `board`."""
    with pytest.raises(TasksError, match="board"):
        parse_config("[board]\nstores = []\nextra = 1\n", CONFIG_SOURCE)


def test_parse_config_rejects_stores_not_a_list(isolated_env: Path) -> None:
    """`board.stores` must be a list; a bare string is refused, naming `stores`."""
    with pytest.raises(TasksError, match="stores"):
        parse_config('[board]\nstores = "x"\n', CONFIG_SOURCE)


def test_parse_config_rejects_an_empty_string_store(isolated_env: Path) -> None:
    """An empty string in `stores` is refused, naming `stores`."""
    with pytest.raises(TasksError, match="stores"):
        parse_config('[board]\nstores = [""]\n', CONFIG_SOURCE)


def test_parse_config_rejects_a_duplicate_store(isolated_env: Path) -> None:
    """The same store listed twice is refused, naming `stores`."""
    with pytest.raises(TasksError, match="stores"):
        parse_config(
            '[board]\nstores = ["/tmp/a/.claude-tasks", "/tmp/a/.claude-tasks"]\n',
            CONFIG_SOURCE,
        )


def test_parse_config_rejects_listing_the_home_store(isolated_env: Path) -> None:
    """The home store is always included; listing it again is refused, naming it."""
    home_path = str((isolated_env / "home-store").resolve())
    with pytest.raises(TasksError, match="home"):
        parse_config(f'[board]\nstores = ["{home_path}"]\n', CONFIG_SOURCE)


def test_parse_config_accepts_a_valid_board_table(isolated_env: Path) -> None:
    """A valid `[board]` table parses into `board_stores`, each path expanded."""
    cfg = parse_config('[board]\nstores = ["~/Scripts/.claude-tasks"]\n', CONFIG_SOURCE)
    assert cfg.board_stores == (Path("~/Scripts/.claude-tasks").expanduser(),)


# --- board_stores: opening the configured stores ------------------------------------------------


def test_board_stores_raises_on_a_path_that_is_not_a_store(home: Store, isolated_env: Path) -> None:
    """A listed path with no store.toml raises naming it."""
    missing = isolated_env / "not-a-store" / ".claude-tasks"
    missing.mkdir(parents=True)
    _set_board_config([missing])
    with pytest.raises(TasksError, match=re.escape(str(missing))):
        board_stores(home)


def test_board_stores_includes_a_second_configured_store(home: Store, isolated_env: Path) -> None:
    """A second listed store, once it really is one, is opened and returned after home."""
    second = init_store(isolated_env / "second-project" / ".claude-tasks", "SECOND", commit=True)
    _set_board_config([second.root])
    assert [s.root for s in board_stores(home)] == [home.root, second.root]


# --- rendering: the independent-oracle checks ---------------------------------------------------

GENERATED = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)


def test_render_board_embeds_exactly_record_object_plus_body(home: Store) -> None:
    """Every embedded record is `record_object(task)` plus `body`, verified against the raw HTML
    rather than against `board.board_payload`'s own output."""
    idea(home, "alpha", size="S", tags=["tooling"])
    idea(home, "beta", state="ready", decided_by="owner", reason="worth it")
    tasks = load_tasks(home)

    html_out = render_board(tasks, [home], GENERATED)

    extracted = sorted(_extract_records(html_out), key=lambda r: str(r["id"]))
    expected = sorted(
        ({**record_object(t), "body": t.body} for t in tasks), key=lambda r: str(r["id"])
    )
    assert extracted == expected


def test_a_body_with_a_script_close_cannot_break_out_of_the_records_block(home: Store) -> None:
    """A body carrying `</script><script>...` and `<!--` round-trips as data: the page still
    shows only the template's own three `</script>` closings (records, meta, the page's script),
    and the html.parser oracle -- which stops at the first literal one -- still recovers it whole.
    """
    nasty = "## Idea\n\nbefore </script><script>alert(1)</script> and <!-- x --> after\n"
    written = add_task(home, "danger", "open", {}, nasty)
    tasks = load_tasks(home)

    html_out = render_board(tasks, [home], GENERATED)

    assert html_out.count("</script>") == 3
    task = next(t for t in tasks if t.path == written.path)
    extracted = next(r for r in _extract_records(html_out) if r["id"] == task.id)
    assert extracted["body"] == nasty


# --- the CLI end to end -------------------------------------------------------------------------


def test_cli_board_writes_the_file_and_shows_a_second_stores_records(
    home: Store,
    isolated_env: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`board --out PATH --no-open` writes the page and prints the path; a record from a listed
    second store appears in it under that store's own prefix."""
    idea(home, "home idea")
    second = init_store(isolated_env / "second-project" / ".claude-tasks", "SECOND", commit=True)
    idea(second, "second idea")
    _set_board_config([second.root])

    out = tmp_path / "out" / "board.html"
    code, printed, err = run_cli(capsys, "board", "--out", str(out), "--no-open")
    assert code == 0, err
    assert printed.strip() == str(out)
    assert out.is_file()

    records = _extract_records(out.read_text(encoding="utf-8"))
    assert {r["store_prefix"] for r in records} == {"CT", "SECOND"}
    assert {r["title"] for r in records} == {"home idea", "second idea"}
