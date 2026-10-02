"""A store holds ideas and handovers (and, where the build ships it, friction entries:
test_friction_kind.py), and every idea command reads and writes ideas only: `set`/`show` on
another kind's id is refused naming the command that owns it, and `list` leaves the other kinds
out of the stream.
"""

import json
from pathlib import Path

import pytest
from conftest import idea, run_cli
from itemctl.model import Assign
from itemctl.store import Store

Capsys = pytest.CaptureFixture[str]


@pytest.fixture
def mixed(home: Store) -> Store:
    """The home store holding an idea and a handover: CT-0001 idea, CT-0002 handover."""
    idea(home, "an idea")
    home.add(
        "handover",
        "a handover",
        [
            Assign("state", "open"),
            Assign("model", "opus"),
            Assign("size", "S"),
            Assign("cwd", "/tmp"),
        ],
        "## Next\n\ncontinue\n",
    )
    return home


def test_the_fixture_holds_one_record_of_each_kind(mixed: Store) -> None:
    """What the refusals below are about is really there: two kinds, two ids."""
    assert sorted((i.id, i.kind) for i in mixed.snapshot().items) == [
        ("CT-0001", "idea"),
        ("CT-0002", "handover"),
    ]
    assert (mixed.root / "handover" / "a-handover" / "item.md").is_file()


def test_set_on_a_handover_id_is_refused_naming_its_command(mixed: Store, capsys: Capsys) -> None:
    """`set` on a handover is refused and names `claude-tasks handover` (deny)."""
    path = mixed.root / "handover" / "a-handover" / "item.md"
    before = path.read_bytes()

    code, out, err = run_cli(capsys, "set", "CT-0002", "size=M")

    assert code == 1
    assert out == ""
    assert "CT-0002 is a handover record, not an idea" in err
    assert "claude-tasks handover" in err
    assert path.read_bytes() == before


def test_show_on_another_kind_is_refused(mixed: Store, capsys: Capsys) -> None:
    """`show` refuses the same ids with the same owners (deny)."""
    code, out, err = run_cli(capsys, "show", "CT-0002")

    assert code == 1
    assert out == ""
    assert "claude-tasks handover" in err


def test_set_and_show_on_an_idea_id_work(mixed: Store, capsys: Capsys) -> None:
    """The same commands on the idea in that store go through (allow)."""
    code, out, err = run_cli(capsys, "set", "CT-0001", "title=renamed")
    assert code == 0, err
    assert out.startswith("CT-0001  ")

    code, out, err = run_cli(capsys, "show", "CT-0001")
    assert code == 0, err
    assert "title: renamed" in out


def test_the_library_set_refuses_another_kind(mixed: Store) -> None:
    """`ops.set_fields`, called directly, refuses under the lock too, so a library caller that
    skips the lookup cannot write another kind through the idea writer."""
    from claude_tasks.errors import TasksError
    from claude_tasks.ops import set_fields

    with pytest.raises(TasksError, match="claude-tasks handover"):
        set_fields(mixed, "CT-0002", ["title=x"], None)


def test_list_json_carries_ideas_only(mixed: Store, capsys: Capsys) -> None:
    """`list --json` streams the idea alone; the handover is not in it."""
    code, out, err = run_cli(capsys, "list", "--json")

    assert code == 0, err
    rows = [json.loads(line) for line in out.splitlines()]
    assert [row["id"] for row in rows] == ["CT-0001"]
    assert Path(rows[0]["path"]).name == "an-idea.md"


def test_the_front_door_and_check_see_the_whole_store(mixed: Store, capsys: Capsys) -> None:
    """The bare command lists the idea only; `check` validates every kind and finds it clean."""
    code, out, err = run_cli(capsys)
    assert code == 0, err
    assert "an idea" in out
    assert "a handover" not in out

    code, out, err = run_cli(capsys, "check")
    assert code == 0, err
    assert out.strip() == "ok"
