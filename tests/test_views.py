"""The two writers `list` has: one NDJSON line per record, and one line for a human."""

import dataclasses
import datetime
import json

import pytest
from conftest import idea, patch_fm
from itemctl.store import Store

from claude_tasks.errors import TasksError
from claude_tasks.store import Task, load_tasks
from claude_tasks.views import list_line, ndjson_line


def test_load_tasks_is_id_ordered(home: Store) -> None:
    """Records come back in id-number order, whatever order the filesystem lists them."""
    for title in ("c", "a", "b"):
        idea(home, title)
    assert [t.id for t in load_tasks(home)] == ["CT-0001", "CT-0002", "CT-0003"]


def test_ndjson_line_is_the_frontmatter_plus_where_it_came_from(home: Store) -> None:
    """One JSON object per record: its own keys, then `store_prefix`, `store` and `path`."""
    written = idea(home, "a")
    task = load_tasks(home)[0]
    obj = json.loads(ndjson_line(task))
    assert obj["id"] == "CT-0001"
    assert obj["title"] == "a"
    assert obj["state"] == "open"
    assert obj["created"] == datetime.date.today().isoformat()
    assert obj["store_prefix"] == "CT"
    assert obj["store"] == str(home.root)
    assert obj["path"] == str(written.path)
    assert "\n" not in ndjson_line(task)


def test_ndjson_line_carries_unmanaged_keys_verbatim(home: Store) -> None:
    """`tags`, `size`, anything a writer put there: the dump is the record, not a CLI shape.

    This is the contract's pass-through promise (docs/json-contract.md), and it is why there is
    no `schema_version`: the keys are the record's own.
    """
    idea(home, "a", size="S", tags=["tooling", "machine-security"], autopilot=True)
    obj = json.loads(ndjson_line(load_tasks(home)[0]))
    assert obj["size"] == "S"
    assert obj["tags"] == ["tooling", "machine-security"]
    assert obj["autopilot"] is True


def test_ndjson_line_renders_a_yaml_date_object_as_its_iso_string(home: Store) -> None:
    """A pass-through field YAML read as a date still dumps, as the string a quoted one would."""
    written = idea(home, "a")
    patch_fm(written.path, decided=datetime.date(2026, 9, 1))
    obj = json.loads(ndjson_line(load_tasks(home)[0]))
    assert obj["decided"] == "2026-09-01"


def test_ndjson_line_refuses_a_value_it_cannot_encode(home: Store) -> None:
    """A frontmatter value JSON has no form for is named, not dropped from the line."""
    written = idea(home, "a")
    task = load_tasks(home)[0]
    odd = dataclasses.replace(task.item, fields={**task.fm, "odd": {1, 2}})
    with pytest.raises(TasksError, match="cannot dump"):
        ndjson_line(Task(home, odd))
    assert written.path.is_file()


def test_list_line_is_id_state_title(home: Store) -> None:
    """The human row names the record and nothing computed; `--json` is the contract."""
    idea(home, "a park this", state="parked", trigger="later")
    assert list_line(load_tasks(home)[0]).split() == ["CT-0001", "parked", "a", "park", "this"]
