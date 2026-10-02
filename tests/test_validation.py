"""The record schema claude-tasks declares in store.toml, and the legacy-id rules it adds.

itemctl's own suite covers the generic rules (ids, slugs, parse errors, stray files, empty values,
placement mechanics). These tests check what claude-tasks decides: that the four states demand
what `docs/json-contract.md` says, that only-in-one-state fields are refused elsewhere, that
`outcome` is an enum, that any other key passes through, and the legacy-id findings. Each fixture
file is written directly, bypassing the CLI, so one violation can be injected; where a test
iterates a vocabulary it takes it from `schema.IDEA` and asserts only that `check` reacts.
"""

import datetime
from pathlib import Path

import pytest
from conftest import make_repo, read_record
from itemctl import record
from itemctl.store import Store

from claude_tasks.errors import TasksError
from claude_tasks.ops import init_store
from claude_tasks.schema import IDEA, PLACEHOLDER_REASON, check_prefix, store_toml
from claude_tasks.validate import check_store

BODY = "## Idea\n\nA short description.\n\n## History\n"
CREATED = datetime.datetime(2026, 1, 1).astimezone()
STATES = [state.name for state in IDEA.states]
OUTCOMES = next(f for f in IDEA.fields if f.name == "outcome").choices


def make_fm(**overrides: object) -> dict[str, object]:
    """A complete, minimally valid open Idea frontmatter; an override of None removes the field."""
    fm: dict[str, object] = {
        "id": "CT-0001",
        "state": "open",
        "created": CREATED,
        "title": "A title",
        "type": "Idea",
    }
    fm.update(overrides)
    return {k: v for k, v in fm.items() if v is not None}


DEMANDED = {"decided_by": "owner", "reason": "worth it", "trigger": "later", "outcome": "built"}


def state_fm(state: str, **overrides: object) -> dict[str, object]:
    """A minimally valid record in `state`: what that state demands, and nothing else."""
    declared = IDEA.state(state)
    fm = make_fm(state=state)
    for fld in (*declared.requires, *declared.requires_any[:1]):
        fm.setdefault(fld, DEMANDED.get(fld))
    fm.update(overrides)
    return {k: v for k, v in fm.items() if v is not None}


def rel_for(state: str, slug: str = "foo") -> str:
    """Where a record in `state` belongs."""
    return f"closed/{slug}.md" if IDEA.state(state).terminal else f"{slug}.md"


def write_task(store: Store, rel: str, fm: dict[str, object]) -> Path:
    """Render a record at store/rel directly, bypassing validation."""
    path = store.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.render(IDEA, fm, BODY))
    return path


def errors(store: Store) -> list[str]:
    """Every finding of the store on disk."""
    return check_store(store)


def test_store_toml_is_the_schema_the_cli_derives_from(home: Store) -> None:
    """A new store's store.toml parses into exactly the kind `schema.IDEA` holds."""
    assert (home.root / "store.toml").read_text() == store_toml("CT")
    assert home.schema.kind("idea") == IDEA
    assert home.schema.id_width == 4 and home.schema.git_commit


@pytest.mark.parametrize("bad_prefix", ["Ct", "C T", "-CT", "", "1CT", "CT-"])
def test_prefix_legality_deny(bad_prefix: str) -> None:
    """A prefix outside `[A-Z][A-Z0-9]*` is refused before a store.toml is written."""
    with pytest.raises(TasksError, match="prefix"):
        store_toml(bad_prefix)


def test_prefix_may_carry_digits() -> None:
    """A prefix derived from a repo name such as `Project2` is legal."""
    assert check_prefix("PROJECT2") == "PROJECT2"


@pytest.mark.parametrize("state", STATES)
def test_every_state_validates_where_it_belongs(home: Store, state: str) -> None:
    """A minimally valid record in each state passes, in the directory its state puts it."""
    write_task(home, rel_for(state), state_fm(state))
    assert errors(home) == []


@pytest.mark.parametrize("state", STATES)
def test_every_required_field_of_every_state_is_demanded(home: Store, state: str) -> None:
    """Dropping any one field a state requires is reported by name."""
    for fld in IDEA.state(state).requires:
        write_task(home, rel_for(state), state_fm(state, **{fld: None}))
        assert any("requires" in e and fld in e for e in errors(home)), (state, fld)


def test_parked_needs_a_trigger_or_a_blocker(home: Store) -> None:
    """parked says why it waits: a trigger, or an id or fact it is blocked on."""
    write_task(home, "foo.md", make_fm(state="parked"))
    assert any("one of" in e and "trigger" in e for e in errors(home))
    write_task(home, "foo.md", make_fm(state="parked", blocked_by=["the new laptop"]))
    assert errors(home) == []


@pytest.mark.parametrize(
    ("fld", "value"), [("trigger", "later"), ("blocked_by", ["CT-0002"]), ("outcome", "built")]
)
def test_state_exclusive_fields_deny(home: Store, fld: str, value: object) -> None:
    """A field bound to another state is refused on an open record."""
    write_task(home, "foo.md", make_fm(**{fld: value}))
    assert any(fld in e and "belongs to state" in e for e in errors(home))


def test_decision_history_persists_allow(home: Store) -> None:
    """decided_by and reason stay legal after the state moves on."""
    write_task(home, "foo.md", make_fm(state="parked", decided_by="owner", reason="r", trigger="t"))
    assert errors(home) == []


def test_placeholder_reason_satisfies_the_field(home: Store) -> None:
    """The one documented placeholder is a `reason` like any other: the CLI judges no reason."""
    write_task(
        home, "foo.md", make_fm(state="ready", decided_by="owner", reason=PLACEHOLDER_REASON)
    )
    assert errors(home) == []


def test_an_unknown_outcome_is_an_error(home: Store) -> None:
    """`outcome` arrives from `set ID outcome=…`, so its value is checked against the enum."""
    write_task(home, "closed/foo.md", make_fm(state="closed", outcome="banana", reason="why"))
    assert any("outcome" in e and "banana" in e for e in errors(home))


def test_every_known_outcome_is_accepted(home: Store) -> None:
    """The allow case beside the deny case: each outcome passes."""
    for n, outcome in enumerate(OUTCOMES, start=1):
        fm = make_fm(
            id=f"CT-{n:04d}", title=f"rec {n}", state="closed", outcome=outcome, reason="w"
        )
        write_task(home, f"closed/rec{n}.md", fm)
    assert errors(home) == []


def test_a_date_string_created_is_an_error(home: Store) -> None:
    """`created` is a timestamp; the pre-itemctl `YYYY-MM-DD` string is what the migration fixes."""
    write_task(home, "foo.md", make_fm(created="2026-01-01"))
    assert any("created" in e for e in errors(home))


def test_project_store_validates_on_its_own_prefix(isolated_env: Path, home: Store) -> None:
    """A project store checks its own records against its own prefix."""
    project = init_store(make_repo(isolated_env / "proj") / ".claude-tasks", "PR", commit=True)
    write_task(project, "dep.md", make_fm(id="PR-0001"))
    assert errors(project) == []
    write_task(project, "bad.md", make_fm(id="CT-0002", title="bad"))
    assert any("CT-0002" in e for e in errors(project))


# --- legacy ids --------------------------------------------------------------------------------


def test_duplicate_legacy_id_deny(home: Store) -> None:
    """Two records carrying one legacy id is an error."""
    write_task(home, "foo.md", make_fm(legacy_ids=["IDEA-0333"]))
    write_task(home, "bar.md", make_fm(id="CT-0002", title="bar", legacy_ids=["IDEA-0333"]))
    assert any("legacy id" in e and "duplicates" in e for e in errors(home))


def test_legacy_id_that_is_a_live_id_deny(home: Store) -> None:
    """An old reference must resolve to exactly one record."""
    write_task(home, "foo.md", make_fm())
    write_task(home, "bar.md", make_fm(id="CT-0002", title="bar", legacy_ids=["CT-0001"]))
    assert any("live id" in e and "CT-0001" in e for e in errors(home))


def test_legacy_id_above_every_live_id_deny_below_allow(home: Store) -> None:
    """An own-prefix legacy id above every live id would be issued again; one below is fine."""
    path = write_task(home, "foo.md", make_fm(id="CT-0005", legacy_ids=["CT-0003", "OLD-0099"]))
    assert errors(home) == []
    write_task(home, "foo.md", make_fm(id="CT-0005", legacy_ids=["CT-0006"]))
    assert any("issue it again" in e for e in errors(home))
    assert read_record(path)[0]["legacy_ids"] == ["CT-0006"]


# --- pass-through keys ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fld", "value"),
    [
        ("tags", ["machine-security", "tooling"]),
        ("tags", "a-single-string"),
        ("size", "XL"),
        ("priority", 0),
        ("autopilot", True),
        ("closed_by", "abc"),
        ("source_runs", ["nothing-on-disk"]),
        ("decided", "2026-09-01"),
        ("legacy_id", "IDEA-0007"),
        ("whatever_a_writer_invents", "x"),
    ],
)
def test_unmanaged_keys_are_never_a_validation_error(home: Store, fld: str, value: object) -> None:
    """A key store.toml does not declare is carried, not judged — whatever value it holds."""
    write_task(home, "foo.md", make_fm(**{fld: value}))
    assert errors(home) == []
    assert read_record(home.root / "foo.md")[0][fld] == value
