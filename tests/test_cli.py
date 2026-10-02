"""CLI behaviour: add, list, show, set (fields, states, notes), check, and the two roots."""

import io
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from conftest import (
    body_file,
    commit_count,
    git,
    idea,
    make_repo,
    patch_fm,
    read_record,
    run_cli,
    sections,
    today,
)
from itemctl.store import Store

from claude_tasks.cli import main
from claude_tasks.config import derive_prefix
from claude_tasks.errors import TasksError
from claude_tasks.ops import init_store
from claude_tasks.views import FRONT_DOOR_MAX

# --- add -------------------------------------------------------------------------------------


def test_add_assigns_sequential_ids_and_commits(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """add prints `<ID>  <path>`, commits once per record, and numbers ids in sequence."""
    before = commit_count(home.root)
    code, out, _err = run_cli(
        capsys, "add", "--title", "First idea", "--body-file", body_file("Why.")
    )
    assert code == 0
    assert out == f"CT-0001  {home.root / 'first-idea.md'}\n"
    code, out, _err = run_cli(
        capsys, "add", "--title", "Second idea", "--body-file", body_file("Why.")
    )
    assert out.startswith("CT-0002  ")
    assert commit_count(home.root) == before + 2
    fm, body = read_record(home.root / "first-idea.md")
    assert fm["state"] == "open" and fm["type"] == "Idea"
    assert list(sections(body)) == ["Idea", "History"]


def test_add_id_is_one_above_the_highest_ever_issued(home: Store) -> None:
    """With CT-0001 and CT-0005 present the next id is CT-0006."""
    idea(home, "one")
    path = idea(home, "two").path
    path.write_text(path.read_text().replace("id: CT-0002", "id: CT-0005"))
    assert idea(home, "three").task_id == "CT-0006"


def test_check_reports_a_legacy_id_an_add_would_reissue(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """itemctl numbers from live ids only, so a retired own-prefix id above them is a finding.

    Both records are filed before either is patched: the rule now gates every write too (not
    only `check`), so introducing the first violation before "two" exists would refuse that add.
    """
    one = idea(home, "one").path
    two = idea(home, "two").path
    patch_fm(one, legacy_ids=["CT-0009"])
    code, out, _err = run_cli(capsys, "check")
    assert code == 1 and "CT-0009" in out and "issue it again" in out
    patch_fm(two, legacy_ids=["CT-0001"])
    code, out, _err = run_cli(capsys, "check")
    assert "live id" in out


def test_add_refused_by_legacy_id_collision_writes_nothing(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """The legacy-id rule gates every write, not only `check`: an add whose freshly issued id
    would become another record's stated legacy id is refused before anything reaches disk."""
    one = idea(home, "one").path
    patch_fm(one, legacy_ids=["CT-0002"])
    before_md = {p.relative_to(home.root): p.read_bytes() for p in home.root.rglob("*.md")}
    before_commits = commit_count(home.root)
    before_status = git(home.root, "status", "--porcelain")
    code, _out, err = run_cli(capsys, "add", "--title", "two", "--body-file", body_file("Why."))
    assert code == 1 and "CT-0002" in err and "live id" in err
    after_md = {p.relative_to(home.root): p.read_bytes() for p in home.root.rglob("*.md")}
    assert after_md == before_md
    assert commit_count(home.root) == before_commits
    assert git(home.root, "status", "--porcelain") == before_status


@pytest.mark.parametrize("old_ids", [["I-1"], ["I-1", "IDEA-0333"]])
def test_add_legacy_id_writes_the_list_and_show_finds_the_record(
    home: Store, capsys: pytest.CaptureFixture[str], old_ids: list[str]
) -> None:
    """Each `--legacy-id` lands in `legacy_ids`, in the order given, and resolves through `show`."""
    flags = [arg for old in old_ids for arg in ("--legacy-id", old)]
    code, out, _err = run_cli(capsys, "add", "--title", "t", "--body-file", body_file(), *flags)
    assert code == 0 and out.startswith("CT-0001  ")
    assert read_record(home.root / "t.md")[0]["legacy_ids"] == old_ids
    for old in old_ids:
        code, out, err = run_cli(capsys, "show", old)
        assert code == 0 and out == (home.root / "t.md").read_text()
        assert err.strip() == f"claude-tasks: {old} is now CT-0001 (t)"


def test_add_without_legacy_id_writes_no_legacy_ids(home: Store) -> None:
    assert "legacy_ids" not in read_record(idea(home, "t").path)[0]


def test_legacy_ids_stay_immutable_after_add(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """`set` refuses the field on a record that carries one, by `=` and by `+=`."""
    run_cli(capsys, "add", "--title", "t", "--body-file", body_file(), "--legacy-id", "I-1")
    before = (home.root / "t.md").read_text()
    for token in ("legacy_ids=I-2", "legacy_ids+=I-2", "legacy_ids=null"):
        code, _out, err = run_cli(capsys, "set", "CT-0001", token)
        assert code == 1 and "immutable" in err
    assert (home.root / "t.md").read_text() == before


def test_add_field_legacy_ids_refused_naming_the_flag(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, err = run_cli(
        capsys, "add", "--title", "t", "--body-file", body_file(), "--field", "legacy_ids=I-1"
    )
    assert code == 1 and "--legacy-id" in err
    assert not (home.root / "t.md").exists()


@pytest.mark.parametrize(
    ("old_ids", "said"),
    [
        ([""], "non-empty"),
        (["   "], "non-empty"),
        ([" I-1"], "surrounding space"),
        (["I-1\nI-2"], "one non-empty line"),
        (["I-1", "I-1"], "given twice"),
    ],
)
def test_add_legacy_id_validation_deny(
    home: Store, capsys: pytest.CaptureFixture[str], old_ids: list[str], said: str
) -> None:
    """An empty, padded, multi-line or repeated legacy id is refused and nothing is written."""
    before = commit_count(home.root)
    flags = [arg for old in old_ids for arg in ("--legacy-id", old)]
    code, _out, err = run_cli(capsys, "add", "--title", "t", "--body-file", body_file(), *flags)
    assert code == 1 and "--legacy-id" in err and said in err
    assert not (home.root / "t.md").exists() and commit_count(home.root) == before


@pytest.mark.parametrize(
    ("old", "said"),
    [
        ("I-1", "duplicates"),  # another record's legacy id
        ("CT-0001", "live id"),  # another record's id
        ("CT-0009", "issue it again"),  # an own-prefix id the next add would issue
    ],
)
def test_add_legacy_id_clash_refused_naming_the_holder(
    home: Store, capsys: pytest.CaptureFixture[str], old: str, said: str
) -> None:
    """A legacy id must resolve to one record: one that another record already answers to, or
    that a later add would issue, fails the store's legacy-id rule and nothing reaches disk."""
    run_cli(capsys, "add", "--title", "one", "--body-file", body_file(), "--legacy-id", "I-1")
    before = commit_count(home.root)
    code, _out, err = run_cli(
        capsys, "add", "--title", "two", "--body-file", body_file(), "--legacy-id", old
    )
    assert code == 1 and old in err and said in err
    if old != "CT-0009":
        assert "one.md" in err
    assert not (home.root / "two.md").exists() and commit_count(home.root) == before
    assert git(home.root, "status", "--porcelain") == ""


def test_add_refuses_duplicate_slug_live_and_closed(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """A slug already used live or under closed/ is refused."""
    idea(home, "same title")
    code, _out, err = run_cli(capsys, "add", "--title", "same title", "--body-file", body_file("x"))
    assert code == 1 and "is taken" in err
    run_cli(capsys, "set", "CT-0001", "state=closed", "outcome=dropped", "reason=dup")
    code, _out, err = run_cli(capsys, "add", "--title", "same title", "--body-file", body_file("x"))
    assert code == 1 and "is taken" in err


def test_add_requires_a_body(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """--body-file is required: a record with no body is not a record."""
    with pytest.raises(SystemExit):
        run_cli(capsys, "add", "--title", "t")


def test_add_body_comes_from_a_file_or_stdin(
    home: Store, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--body-file reads the named file, and `-` reads stdin -- the only two spellings."""
    draft = tmp_path / "draft.md"
    draft.write_text("From a file.\n")
    assert run_cli(capsys, "add", "--title", "from file", "--body-file", str(draft))[0] == 0
    assert "From a file." in (home.root / "from-file.md").read_text()
    monkeypatch.setattr("sys.stdin", io.StringIO("From dash.\n"))
    assert run_cli(capsys, "add", "--title", "from dash", "--body-file", "-")[0] == 0
    assert "From dash." in (home.root / "from-dash.md").read_text()


def test_add_missing_body_file_fails_loud(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """A --body-file that does not exist names the path rather than filing an empty record."""
    code, _out, err = run_cli(capsys, "add", "--title", "t", "--body-file", "nowhere.md")
    assert code == 1 and "nowhere.md" in err


# --- set -------------------------------------------------------------------------------------


def test_set_updates_field_and_commits(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """set writes the field and commits, whether the schema manages the field or not."""
    idea(home, "t")
    before = commit_count(home.root)
    code, out, _err = run_cli(capsys, "set", "CT-0001", "size=M", "decided_by=owner")
    assert code == 0 and out.startswith("CT-0001  ")
    fm, _ = read_record(home.root / "t.md")
    assert fm["size"] == "M" and fm["decided_by"] == "owner"
    assert commit_count(home.root) == before + 1


def test_set_takes_assignments_after_an_option_between_them_and_the_id(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """`set ID --store PATH FIELD=VALUE` applies the assignments, as with the option first."""
    idea(home, "t")
    code, _out, err = run_cli(capsys, "set", "CT-0001", "--store", str(home.root), "size=M")
    assert code == 0, err
    fm, _ = read_record(home.root / "t.md")
    assert fm["size"] == "M"


def test_set_still_refuses_an_unknown_option_after_the_id(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only bare words left over join set's tokens; an unknown option is refused."""
    idea(home, "t")
    with pytest.raises(SystemExit) as exc:
        run_cli(capsys, "set", "CT-0001", "--store", str(home.root), "--bogus", "size=M")
    assert exc.value.code == 2


def test_set_refused_leaves_file_byte_identical_and_no_commit(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """An invalid set changes nothing on disk and makes no commit."""
    path = idea(home, "t").path
    before_text = path.read_text()
    before = commit_count(home.root)
    code, _out, err = run_cli(capsys, "set", "CT-0001", "state=closed")
    assert code == 1 and "outcome" in err
    assert path.read_text() == before_text
    assert commit_count(home.root) == before


def test_set_many_ids_is_one_commit_with_every_record_changed(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """set ID ID ID changes every record and makes exactly one commit, naming each id."""
    paths = [idea(home, title).path for title in ("a", "b", "c")]
    before = commit_count(home.root)
    code, out, err = run_cli(
        capsys, "set", "CT-0001", "CT-0002", "CT-0003", "size=M", "--note", "batch"
    )
    assert code == 0, err
    assert [line.split()[0] for line in out.splitlines()] == ["CT-0001", "CT-0002", "CT-0003"]
    for path in paths:
        fm, body = read_record(path)
        assert fm["size"] == "M" and "batch" in body
    assert commit_count(home.root) == before + 1
    assert "CT-0001 CT-0002 CT-0003" in git(home.root, "log", "-1", "--format=%s")
    assert git(home.root, "status", "--porcelain").strip() == ""


def test_set_many_closing_moves_every_record_in_one_commit(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """A state change that moves the files (to closed/) still commits once."""
    idea(home, "a")
    idea(home, "b")
    before = commit_count(home.root)
    code, _out, err = run_cli(
        capsys, "set", "CT-0001", "CT-0002", "state=closed", "outcome=built", "reason=done"
    )
    assert code == 0, err
    assert not (home.root / "a.md").exists() and not (home.root / "b.md").exists()
    assert commit_count(home.root) == before + 1
    assert git(home.root, "status", "--porcelain").strip() == ""


def test_set_many_with_one_bad_id_writes_nothing(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown id, or a change the validator refuses, leaves every record and git alone."""
    paths = [idea(home, "a").path, idea(home, "b", state="parked", trigger="later").path]
    texts = [path.read_text() for path in paths]
    before = commit_count(home.root)
    code, _out, err = run_cli(capsys, "set", "CT-0001", "CT-0009", "size=M")
    assert code == 1 and "CT-0009" in err
    # both lack the closed state's outcome, so the whole validated result is refused
    code, _out, err = run_cli(capsys, "set", "CT-0001", "CT-0002", "state=closed")
    assert code == 1 and "outcome" in err
    assert [path.read_text() for path in paths] == texts
    assert commit_count(home.root) == before


def test_set_a_single_id_is_unchanged(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """One id: the one `<ID>  <path>` line and the one commit message `set` always made."""
    path = idea(home, "t").path
    code, out, _err = run_cli(capsys, "set", "CT-0001", "size=M")
    assert code == 0 and out == f"CT-0001  {path}\n"
    assert git(home.root, "log", "-1", "--format=%s").strip() == "idea: set CT-0001 t"


def test_set_repeating_an_id_applies_it_once(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same id twice is one record changed once, so a note is not appended twice."""
    path = idea(home, "t").path
    code, out, _err = run_cli(capsys, "set", "CT-0001", "CT-0001", "--note", "once")
    assert code == 0 and len(out.splitlines()) == 1
    assert path.read_text().count("once") == 1


@pytest.mark.parametrize("fld", ["id", "type", "created", "legacy_ids"])
def test_set_immutable_field_deny(
    home: Store, capsys: pytest.CaptureFixture[str], fld: str
) -> None:
    """id, type, created and legacy_ids are immutable."""
    idea(home, "t")
    code, _out, err = run_cli(capsys, "set", "CT-0001", f"{fld}=x")
    assert code == 1 and "immutable" in err


def test_set_list_append_and_null(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """`+=` grows a list field (refusing duplicates and non-lists), `=null` removes it.

    `+=` is the only way to build a list on a field the schema does not manage: a comma list is
    a list only where a managed kind says so (`blocked_by`), because elsewhere the CLI cannot
    know whether `a,b` is two entries or one string.
    """
    idea(home, "t")
    assert run_cli(capsys, "set", "CT-0001", "touches+=a,b")[0] == 0
    assert run_cli(capsys, "set", "CT-0001", "touches+=c")[0] == 0
    fm, _ = read_record(home.root / "t.md")
    assert fm["touches"] == ["a", "b", "c"]
    code, _out, err = run_cli(capsys, "set", "CT-0001", "touches+=a")
    assert code == 1 and "already present" in err
    code, _out, err = run_cli(capsys, "set", "CT-0001", "title+=x")
    assert code == 1 and "expected a list of strings" in err
    assert run_cli(capsys, "set", "CT-0001", "touches=null")[0] == 0
    fm, _ = read_record(home.root / "t.md")
    assert "touches" not in fm


def test_set_walks_the_states_in_one_shape(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """open → ready → parked → closed, each through `set`, the file moving when it closes."""
    idea(home, "t")
    idea(home, "other")
    steps = [
        (["state=ready", "decided_by=owner", "reason=worth it"], "ready"),
        (["state=parked", "trigger=after the release"], "parked"),
        (["state=parked", "blocked_by=CT-0002", "trigger=null"], "parked"),
        (["state=closed", "outcome=built", "reason=landed", "closed_by=abc123"], "closed"),
    ]
    for assignments, state in steps:
        code, _out, err = run_cli(capsys, "set", "CT-0001", *assignments)
        assert code == 0, err
        fm, _ = read_record(home.root / ("closed" if state == "closed" else "") / "t.md")
        assert fm["state"] == state
    fm, _ = read_record(home.root / "closed" / "t.md")
    assert fm["outcome"] == "built" and fm["closed_by"] == "abc123" and fm["reason"] == "landed"
    assert "trigger" not in fm and "blocked_by" not in fm
    assert not (home.root / "t.md").exists()


@pytest.mark.parametrize(
    ("assignments", "wanted"),
    [
        (["state=ready"], ["decided_by", "reason"]),
        (["state=closed", "reason=x"], ["outcome"]),
        (["state=parked"], ["trigger", "blocked_by"]),
    ],
)
def test_set_enforces_the_new_states_required_fields_in_the_same_call(
    home: Store, capsys: pytest.CaptureFixture[str], assignments: list[str], wanted: list[str]
) -> None:
    """A state change without that state's fields is refused, naming them; nothing is written."""
    path = idea(home, "t").path
    before = path.read_text()
    code, _out, err = run_cli(capsys, "set", "CT-0001", *assignments)
    assert code == 1
    assert all(field in err for field in wanted)
    assert path.read_text() == before


def test_set_refusals_each_name_every_problem_of_the_closing_sequence(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Closing an idea by guesswork -- state=done, state=closed, outcome=done, then right -- is
    refused three times, and each refusal already names the state, every field closed requires and
    the values `outcome` allows, marking what is missing or invalid in that call."""
    path = idea(home, "t").path
    before = path.read_text()
    allowed = "one of built, dropped, superseded"

    code, _out, err = run_cli(capsys, "set", "CT-0001", "state=done")
    assert code == 1
    assert "no state 'done'" in err and "open, ready, parked, closed" in err
    assert f"outcome: missing; {allowed}" in err and "reason: missing" in err

    code, _out, err = run_cli(capsys, "set", "CT-0001", "state=closed")
    assert code == 1
    assert f"outcome: missing; {allowed}" in err and "reason: missing" in err

    code, _out, err = run_cli(capsys, "set", "CT-0001", "state=closed", "outcome=done")
    assert code == 1
    assert f"outcome: 'done' is invalid; {allowed}" in err and "reason: missing" in err
    assert path.read_text() == before

    code, _out, err = run_cli(
        capsys, "set", "CT-0001", "state=closed", "outcome=built", "reason=landed"
    )
    assert code == 0, err


def test_set_refusal_marks_only_the_fields_still_wrong(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """A field given in the call is shown as given, so the refusal says what is left to fix."""
    idea(home, "t")
    code, _out, err = run_cli(capsys, "set", "CT-0001", "state=closed", "reason=landed")
    assert code == 1
    assert "outcome: missing" in err and "reason: given" in err


def test_set_many_refusal_names_the_record_with_the_problem(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused `set ID ID` writes nothing and attributes the missing fields to their ids."""
    idea(home, "a")
    idea(home, "b")
    before = commit_count(home.root)
    code, _out, err = run_cli(capsys, "set", "CT-0001", "CT-0002", "state=closed", "reason=x")
    assert code == 1
    assert "CT-0001: state closed needs:" in err and "CT-0002: state closed needs:" in err
    assert err.count("outcome: missing; one of built, dropped, superseded") == 2
    assert commit_count(home.root) == before


def test_set_reopens_a_closed_record_with_a_note(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """`set ID state=open --note` is what reopening is now: back to the root, the note in History."""
    idea(home, "t")
    assert (
        run_cli(capsys, "set", "CT-0001", "state=closed", "outcome=built", "reason=landed")[0] == 0
    )
    code, out, err = run_cli(capsys, "set", "CT-0001", "state=open", "--note", "not done after all")
    assert code == 0, err
    assert out == f"CT-0001  {home.root / 't.md'}\n"
    fm, body = read_record(home.root / "t.md")
    assert fm["state"] == "open"
    assert not {"outcome", "closed_by"} & set(fm)
    assert "not done after all" in sections(body)["History"]


def test_set_drops_the_old_states_exclusive_fields(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Leaving parked takes `trigger` with it."""
    idea(home, "t", state="parked", trigger="after the release")
    code, _out, err = run_cli(capsys, "set", "CT-0001", "state=open")
    assert code == 0, err
    fm, _ = read_record(home.root / "t.md")
    assert "trigger" not in fm


def test_note_appends_a_dated_line_and_writes_no_field(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """--note appends to ## History only; two notes become two lines of one list."""
    idea(home, "t")
    before_fm, _ = read_record(home.root / "t.md")
    assert run_cli(capsys, "set", "CT-0001", "--note", "first thought")[0] == 0
    assert run_cli(capsys, "set", "CT-0001", "--note", "second thought")[0] == 0
    fm, body = read_record(home.root / "t.md")
    assert fm == before_fm
    history = sections(body)["History"].strip().splitlines()
    assert history == [f"- {today()} — first thought", f"- {today()} — second thought"]


def test_set_without_assignment_or_note_is_refused(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """A set that would change nothing says so instead of making an empty commit."""
    idea(home, "t")
    code, _out, err = run_cli(capsys, "set", "CT-0001")
    assert code == 1 and "changes nothing" in err


# --- commit ----------------------------------------------------------------------------------


# --- list ------------------------------------------------------------------------------------


def test_list_dumps_every_record_in_every_state(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """list is a dump: every record of the store, whatever its state, and no flag to narrow it.

    Selection is the consumer's, through `--json` and the query language of its choice, so a
    closed or parked record is in the stream exactly like an open one.
    """
    idea(home, "alpha")
    idea(home, "beta")
    idea(home, "gamma", state="ready", decided_by="owner", reason="worth it")
    idea(home, "delta", state="parked", trigger="after the release")
    run_cli(capsys, "set", "CT-0002", "state=closed", "outcome=dropped", "reason=no")
    code, out, err = run_cli(capsys, "list")
    assert code == 0, err
    assert [line.split()[0] for line in out.splitlines()] == [
        "CT-0001",
        "CT-0002",
        "CT-0003",
        "CT-0004",
    ]
    for flag in ("--state", "--closed", "--project", "--unexplained", "--count-by", "--format"):
        with pytest.raises(SystemExit):
            run_cli(capsys, "list", flag, "open")


def test_list_json_is_ndjson_of_the_records_own_keys(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--json` is one JSON object per line: the frontmatter verbatim plus where it came from."""
    idea(home, "t", size="S", tags=["tooling"])
    idea(home, "u")
    code, out, err = run_cli(capsys, "list", "--json")
    assert code == 0, err
    lines = out.splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    fm, _ = read_record(home.root / "t.md")
    assert set(row) == set(fm) | {"store_prefix", "store", "path"}
    assert row["size"] == "S" and row["tags"] == ["tooling"]
    assert row["store_prefix"] == "CT" and row["store"] == str(home.root)
    assert row["path"] == str(home.root / "t.md")


def test_list_fails_loud_on_broken_file(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """A task file without frontmatter makes list fail naming it."""
    idea(home, "t")
    (home.root / "broken.md").write_text("no fence\n")
    code, out, err = run_cli(capsys, "list")
    assert code == 1 and "broken.md" in err and out == ""


def test_no_arguments_is_the_front_door(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """No subcommand shows what is waiting and how to start one -- not the raw dump."""
    idea(home, "undecided one")
    idea(home, "decided one", state="ready", decided_by="owner", reason="worth it")
    idea(home, "sleeping", state="parked", trigger="after the release")
    code, out, _err = run_cli(capsys)
    assert code == 0
    assert "1 ready - 1 open" in out
    assert "decided one" in out and "undecided one" in out
    assert "sleeping" not in out, "a parked record waits on its trigger, not on him"
    assert "claude-tasks show <ID>" in out


def test_front_door_caps_a_long_queue(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """Past FRONT_DOOR_MAX it summarises the rest: a front door has to read at a glance."""
    for n in range(FRONT_DOOR_MAX + 3):
        idea(home, f"idea number {n}")
    code, out, _err = run_cli(capsys)
    assert code == 0
    assert out.count("\n  CT-") == FRONT_DOOR_MAX
    assert "... and 3 more" in out


def test_front_door_says_so_when_nothing_waits(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty queue is stated, not shown as a bare count with no rows under it."""
    idea(home, "done")
    assert (
        run_cli(capsys, "set", "CT-0001", "state=closed", "outcome=built", "reason=landed")[0] == 0
    )
    code, out, _err = run_cli(capsys)
    assert code == 0 and "Nothing is waiting" in out


def test_list_exits_quietly_when_the_reader_stops_early(
    home: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`list --json | head` gets the SIGPIPE status, not a BrokenPipeError traceback.

    A real pipe whose read end is already closed is the only faithful stand-in: the handler
    redirects `sys.stdout.fileno()`, so a StringIO would pass for the wrong reason.
    """
    idea(home, "t")
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    with open(write_fd, "w", buffering=1) as dead_pipe:
        monkeypatch.setattr(sys, "stdout", dead_pipe)
        code = main(["list", "--json"])
    assert code == 128 + signal.SIGPIPE


# --- show and check --------------------------------------------------------------------------


def test_show_by_legacy_id(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """show resolves an id the migration retired, and says on stderr which record holds it now."""
    patch_fm(idea(home, "t").path, legacy_ids=["IDEA-0333"])
    raw = (home.root / "t.md").read_text()
    code, out, err = run_cli(capsys, "show", "IDEA-0333")
    assert code == 0 and out == raw
    assert err.strip() == "claude-tasks: IDEA-0333 is now CT-0001 (t)"


def test_show_json_is_the_list_record_plus_body(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """`show --json` parses as one object: `list --json`'s record for that id, plus `body`."""
    task = idea(home, "t")
    listed = json.loads(run_cli(capsys, "list", "--json")[1].splitlines()[0])
    code, out, err = run_cli(capsys, "show", "--json", task.task_id)
    shown = json.loads(out)
    assert code == 0 and err == "" and len(out.splitlines()) == 1
    body = shown.pop("body")
    assert "A description." in body and "body" not in listed
    assert shown == listed


def test_show_json_unknown_id_fails_as_without_json(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown id under `--json` has the plain `show` exit code and stderr, and no stdout."""
    plain = run_cli(capsys, "show", "CT-0099")
    code, out, err = run_cli(capsys, "show", "--json", "CT-0099")
    assert (code, out, err) == plain and code == 1 and out == ""


def test_show_does_not_resolve_a_path(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """A reference is an id, not a path: a record's own file no longer names it.

    Lookup by path went with the attachment directory it was built for. A record is reached by
    id, and `list --json` carries `path` for anyone who wants the file.
    """
    path = idea(home, "t").path
    code, _out, err = run_cli(capsys, "show", str(path))
    assert code == 1 and "by id or a legacy id" in err


def test_show_unknown_id_lists_where_it_looked(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown reference raises naming the stores searched."""
    code, _out, err = run_cli(capsys, "show", "CT-0099")
    assert code == 1 and "CT-0099" in err and str(home.root) in err


def test_check_reports_every_error(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """check prints all errors and a count, or ok."""
    assert run_cli(capsys, "check")[1] == "ok\n"
    (home.root / "a.md").write_text("no fence\n")
    (home.root / "b.md").write_text("no fence either\n")
    code, out, err = run_cli(capsys, "check")
    assert code == 1 and len(out.splitlines()) == 2 and "2 error(s) found" in err


def test_every_write_commits_itself(home: Store, capsys: pytest.CaptureFixture[str]) -> None:
    """A write always commits: there is no way to leave a record uncommitted through the CLI.

    Leaving out a `--no-commit` option means a record on disk is a record in git.
    """
    before = commit_count(home.root)
    assert run_cli(capsys, "add", "--title", "t", "--body-file", body_file("x"))[0] == 0
    assert run_cli(capsys, "set", "CT-0001", "size=M")[0] == 0
    assert commit_count(home.root) == before + 2
    assert git(home.root, "status", "--porcelain").strip() == ""


# --- the two roots ---------------------------------------------------------------------------


def test_add_creates_this_repos_store_with_a_derived_prefix(
    isolated_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a repo with no store, add creates `<toplevel>/.claude-tasks` named after the repo."""
    repo = make_repo(isolated_env / "QM-Theory")
    monkeypatch.chdir(repo)
    code, out, err = run_cli(capsys, "add", "--title", "first", "--body-file", body_file("x"))
    assert code == 0, err
    store_root = repo / ".claude-tasks"
    assert out == f"QMTHEORY-0001  {store_root / 'first.md'}\n"
    assert (store_root / "store.toml").is_file()
    assert (store_root / ".gitignore").read_text() == ".lock\nhandover/**/files/\n"
    assert git(repo, "status", "--porcelain", "--", ".claude-tasks").strip() == ""
    assert not (store_root / "runs").exists()


def test_add_from_a_linked_worktree_commits_only_the_store_in_the_main_checkout(
    isolated_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """From a worktree, add writes the main checkout's store: one commit to create it and one per
    record, each of store paths only, with the main checkout's index and files and the worktree's
    branch untouched."""
    repo = make_repo(isolated_env / "Proj")
    (repo / "staged.txt").write_text("peer staged\n")
    git(repo, "add", "staged.txt")
    (repo / "README").write_text("peer unstaged edit\n")
    (repo / "untracked.txt").write_text("peer scratch\n")
    status_before = git(repo, "status", "--porcelain")
    worktree = isolated_env / "Proj-wt" / "feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    wt_head = git(worktree, "rev-parse", "HEAD")
    main_before = commit_count(repo)
    monkeypatch.chdir(worktree)

    for title in ("first", "second"):
        code, _out, err = run_cli(capsys, "add", "--title", title, "--body-file", body_file("x"))
        assert code == 0, err

    store_root = repo / ".claude-tasks"
    assert (store_root / "second.md").is_file()
    assert not (worktree / ".claude-tasks").exists()
    assert git(worktree, "rev-parse", "HEAD") == wt_head
    assert git(repo, "status", "--porcelain") == status_before
    assert git(repo, "diff", "--cached", "--name-only").split() == ["staged.txt"]
    assert commit_count(repo) == main_before + 3
    touched = git(repo, "log", "--format=", "--name-only", "HEAD~3..HEAD").split()
    assert touched and all(path.startswith(".claude-tasks/") for path in touched), touched


def test_a_write_from_a_linked_worktree_says_where_it_went_on_stderr(
    isolated_env: Path,
    home: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add and set from a worktree end with one stderr line naming the main checkout's store, the
    commit just made there and its branch."""
    repo = make_repo(isolated_env / "Proj")
    worktree = isolated_env / "Proj-wt" / "feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    monkeypatch.chdir(worktree)

    code, out, err = run_cli(capsys, "add", "--title", "first", "--body-file", body_file("x"))
    assert code == 0, err
    sha = git(repo, "rev-parse", "--short", "HEAD").strip()
    assert err == (
        f"claude-tasks: wrote {repo / '.claude-tasks'} (the repository's one store, in its "
        f"main checkout; commit {sha} on main)\n"
    )
    task_id = out.split()[0]

    code, _out, err = run_cli(capsys, "set", task_id, "size=M")
    assert code == 0, err
    sha = git(repo, "rev-parse", "--short", "HEAD").strip()
    assert err.endswith(f"main checkout; commit {sha} on main)\n")
    assert err.count("\n") == 1


def test_a_write_from_the_main_checkout_prints_no_worktree_notice(
    isolated_env: Path,
    home: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From the main checkout, even with a linked worktree present, a write prints nothing on
    stderr."""
    repo = make_repo(isolated_env / "Proj")
    git(repo, "worktree", "add", "-q", "-b", "feature", str(isolated_env / "Proj-wt" / "feature"))
    monkeypatch.chdir(repo)

    code, out, err = run_cli(capsys, "add", "--title", "first", "--body-file", body_file("x"))
    assert code == 0
    assert err == ""
    code, _out, err = run_cli(capsys, "set", out.split()[0], "size=M")
    assert code == 0
    assert err == ""


def test_add_outside_a_repo_creates_the_home_store(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Outside a repo, add files into the home store, which it creates with its CT prefix."""
    code, out, err = run_cli(capsys, "add", "--title", "loose idea", "--body-file", body_file("x"))
    assert code == 0, err
    root = isolated_env / "home-store"
    assert out == f"CT-0001  {root / 'loose-idea.md'}\n"
    assert (root / "store.toml").is_file()


@pytest.mark.parametrize(
    ("name", "prefix"), [("QM-Theory", "QMTHEORY"), ("New QM", "NEWQM"), ("ClaudeCtl", "CLAUDECTL")]
)
def test_derived_prefixes(name: str, prefix: str) -> None:
    """A new store's prefix is its repo directory name, uppercased and stripped."""
    assert derive_prefix(Path("/srv/Projects") / name) == prefix


def test_derive_prefix_refuses_a_name_with_nothing_left(tmp_path: Path) -> None:
    """A directory name that strips to nothing raises instead of inventing a prefix."""
    with pytest.raises(TasksError, match="derive"):
        derive_prefix(tmp_path / "---")
    with pytest.raises(TasksError, match="derive"):
        derive_prefix(tmp_path / "9lives")


def test_list_mixes_both_roots_and_the_flags_pin_one(
    isolated_env: Path,
    home: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside a repo with a store, list shows it and the home store; --home/--store pin one."""
    idea(home, "home idea")
    repo = make_repo(isolated_env / "proj")
    project = init_store(repo / ".claude-tasks", "PR", commit=True)
    idea(project, "project idea")
    monkeypatch.chdir(repo)

    def ids(*argv: str) -> list[str]:
        """The ids list prints."""
        code, out, err = run_cli(capsys, "list", *argv)
        assert code == 0, err
        return [line.split()[0] for line in out.splitlines()]

    assert ids() == ["PR-0001", "CT-0001"]
    assert ids("--home") == ["CT-0001"]
    assert ids("--store", str(project.root)) == ["PR-0001"]
    assert run_cli(capsys, "show", "CT-0001")[0] == 0
    assert run_cli(capsys, "add", "--title", "second", "--body-file", body_file("x"))[1].startswith(
        "PR-0002"
    )


def test_store_flag_must_name_an_existing_store(
    home: Store, isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mistyped --store fails instead of seeding a second store beside the real one."""
    missing = isolated_env / "typo" / ".claude-tasks"
    code, _out, err = run_cli(capsys, "list", "--store", str(missing))
    assert code == 1 and "must already exist" in err
    assert not missing.exists()
    plain = isolated_env / "plain"
    plain.mkdir()
    code, _out, err = run_cli(capsys, "list", "--store", str(plain))
    assert code == 1 and "not a claude-tasks store" in err


def test_store_and_home_together_are_refused(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """--store and --home name different stores, so they cannot both be given."""
    with pytest.raises(SystemExit):
        run_cli(capsys, "list", "--home", "--store", str(home.root))


def test_reads_outside_a_repo_never_create_a_store(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no home store yet, list names the missing store.toml and writes nothing."""
    code, _out, err = run_cli(capsys, "list")
    assert code == 1 and "no store.toml" in err and "not a claude-tasks store" in err
    assert not (isolated_env / "home-store").exists()


def test_a_repo_without_a_store_reads_the_home_store_alone(
    isolated_env: Path,
    home: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside a repo that has no store, list reads the home store and creates nothing."""
    idea(home, "home idea")
    repo = make_repo(isolated_env / "bare")
    monkeypatch.chdir(repo)
    code, out, err = run_cli(capsys, "list")
    assert code == 0, err
    assert [line.split()[0] for line in out.splitlines()] == ["CT-0001"]
    assert not (repo / ".claude-tasks").exists()


def test_set_from_a_repo_works_while_the_home_store_does_not_exist(
    isolated_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo store is read and written with no home store yet, and none is created."""
    repo = make_repo(isolated_env / "proj")
    monkeypatch.chdir(repo)
    assert run_cli(capsys, "add", "--title", "a", "--body-file", body_file("x"))[0] == 0
    assert run_cli(capsys, "add", "--title", "b", "--body-file", body_file("x"))[0] == 0
    code, out, err = run_cli(capsys, "set", "PROJ-0001", "PROJ-0002", "size=M")
    assert code == 0, err
    assert [line.split()[0] for line in out.splitlines()] == ["PROJ-0001", "PROJ-0002"]
    code, out, err = run_cli(capsys, "list")
    assert code == 0, err
    assert len(out.splitlines()) == 2
    assert not (isolated_env / "home-store").exists()


def test_targeting_the_missing_home_store_still_fails(
    isolated_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--home names the home store, so its absence stays an error even from inside a repo."""
    repo = make_repo(isolated_env / "proj")
    monkeypatch.chdir(repo)
    assert run_cli(capsys, "add", "--title", "a", "--body-file", body_file("x"))[0] == 0
    code, _out, err = run_cli(capsys, "list", "--home")
    assert code == 1 and "no store.toml" in err
    code, _out, err = run_cli(capsys, "set", "--home", "CT-0001", "size=M")
    assert code == 1 and "no store.toml" in err


def test_set_keeps_every_key_the_record_already_carried(
    home: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record's own keys survive a `set` untouched — the contract's pass-through promise.

    This is the check that stops a writer or a migration from being forced into a lossy rewrite:
    a validator stricter than `docs/json-contract.md` would make `tags` unwritable, and the way
    past it is to destroy data.
    """
    written = idea(home, "t")
    patch_fm(written.path, tags=["machine-security", "tooling"], legacy_id="IDEA-0007")
    before_fm, _body = read_record(written.path)
    code, _out, err = run_cli(
        capsys,
        "set",
        "CT-0001",
        "state=ready",
        "decided_by=owner",
        "reason=worth it",
        "--note",
        "decided",
    )
    assert code == 0, err
    after_fm, body = read_record(written.path)
    assert after_fm["tags"] == ["machine-security", "tooling"]
    assert after_fm["legacy_id"] == "IDEA-0007"
    assert set(after_fm) == set(before_fm) | {"decided_by", "reason"}
    assert "decided" in sections(body)["History"]
