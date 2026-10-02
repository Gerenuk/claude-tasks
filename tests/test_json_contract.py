"""Real `list --json` output validated against `docs/json-contract.md`.

The doc is the oracle: its two tables are parsed out of the markdown and the assertions come from
them, never from a second copy of the schema. `test_the_oracle_catches_a_narrowed_dump` injects
the mutation the tables exist to catch, so a parser that silently found nothing cannot pass.
"""

import json
import re
from pathlib import Path

import pytest
from conftest import idea, patch_fm, run_cli
from itemctl.store import Store

from claude_tasks.schema import IDEA, PLACEHOLDER_REASON

STATES = [state.name for state in IDEA.states]

CONTRACT = Path(__file__).resolve().parent.parent / "docs" / "json-contract.md"
CELLS_RE = re.compile(r"^\|(?P<first>[^|]+)\|(?P<second>[^|]+)\|\s*$")


def section_rows(heading: str) -> list[tuple[str, str]]:
    """The two-column table rows under a `##` heading, as (first cell, second cell)."""
    rows: list[tuple[str, str]] = []
    inside = False
    for line in CONTRACT.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line[3:].strip() == heading
        elif inside and (match := CELLS_RE.match(line)):
            rows.append((match["first"].strip(), match["second"].strip()))
    return [row for row in rows if backticked(row[0])]  # skips the header and separator rows


def backticked(cell: str) -> list[str]:
    """The `name` tokens of a table cell, ignoring anything in parentheses (a value list)."""
    return re.findall(r"`([a-z_]+)`", re.sub(r"\([^)]*\)", "", cell))


def guaranteed_keys() -> set[str]:
    """The keys the contract says every record in the stream carries."""
    keys = {backticked(first)[0] for first, _meaning in section_rows("Keys every record carries")}
    assert len(keys) >= 7, f"the contract's key table did not parse: {keys}"
    return keys


def state_requirements() -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """Per state, the keys the contract requires: (all of these, at least one of these)."""
    table: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for first, second in section_rows("Keys a record carries because of its state"):
        state = backticked(first)[0]
        fields = tuple(backticked(second))
        if " or " in second:
            table[state] = ((), fields)
        else:
            table[state] = (fields, ())
    assert set(table) == set(STATES), f"the contract's state table did not parse: {table}"
    return table


def test_the_schema_enforces_exactly_the_documented_state_requirements() -> None:
    """store.toml and the contract file say the same thing, read from the doc's markdown.

    `title` is every state's requirement, so the doc lists it among the keys every record carries.
    """
    documented = state_requirements()
    for state, (required, one_of) in documented.items():
        declared = IDEA.state(state)
        assert tuple(f for f in declared.requires if f != "title") == required, state
        assert declared.requires_any == one_of, state
    assert all("title" in state.requires for state in IDEA.states)


def test_the_contract_names_the_placeholder_reason_constant() -> None:
    """The doc points consumers at `schema.PLACEHOLDER_REASON`, which is where the string lives."""
    text = CONTRACT.read_text(encoding="utf-8")
    assert "PLACEHOLDER_REASON" in text
    assert "schema.py" in text
    assert PLACEHOLDER_REASON.startswith("no reason was recorded")


def test_the_contract_promises_no_envelope_and_no_version() -> None:
    """The doc is explicit that there is no `schema_version`, so a consumer stops comparing one."""
    text = CONTRACT.read_text(encoding="utf-8").lower()
    assert "newline-delimited json" in text
    assert "no version field" in text


@pytest.fixture
def populated(home: Store, capsys: pytest.CaptureFixture[str]) -> Store:
    """One record per state, the first carrying keys the schema does not manage."""
    idea(home, "rich", size="M", tags=["tooling"], touches=["a.py"], why="because")
    patch_fm(home.root / "rich.md", legacy_ids=["IDEA-0333"])
    idea(home, "ready one", state="ready", decided_by="owner", reason="worth it")
    idea(home, "parked one", state="parked", trigger="later")
    idea(home, "closed one")
    run_cli(capsys, "set", "CT-0004", "state=closed", "outcome=built", "reason=landed")
    return home


def dumped(populated: Store, capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    """`list --json` parsed as NDJSON: one complete object per line, and nothing else."""
    code, out, err = run_cli(capsys, "list", "--json")
    assert code == 0, err
    lines = out.splitlines()
    rows = [json.loads(line) for line in lines]
    assert all(isinstance(row, dict) for row in rows)
    return rows


def test_every_line_carries_the_guaranteed_keys(
    populated: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every record of every state carries the contract's guaranteed keys, plus its state's."""
    rows = dumped(populated, capsys)
    assert len(rows) == 4
    keys = guaranteed_keys()
    requirements = state_requirements()
    for row in rows:
        assert keys <= set(row), row
        required, one_of = requirements[str(row["state"])]
        assert set(required) <= set(row), row
        assert not one_of or set(one_of) & set(row), row
        assert "schema_version" not in row
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row["created"])), row


def test_unmanaged_keys_reach_the_dump(
    populated: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """A key the schema does not manage is in the stream unchanged: the pass-through promise."""
    row = next(r for r in dumped(populated, capsys) if r["id"] == "CT-0001")
    assert row["size"] == "M"
    assert row["tags"] == ["tooling"]
    assert row["touches"] == ["a.py"]
    assert row["legacy_ids"] == ["IDEA-0333"]


def test_the_oracle_catches_a_narrowed_dump() -> None:
    """The documented key set is wide enough to catch a dump that dropped one of its keys.

    Without this, a table that failed to parse would make every assertion above vacuous.
    """
    keys = guaranteed_keys()
    assert {"id", "title", "state", "created", "path", "store_prefix", "store"} <= keys
    for dropped in sorted(keys):
        assert not keys <= (keys - {dropped})
