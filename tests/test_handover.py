"""`claude-tasks handover`: filing a handover into the store a write from its cwd goes to, the
claim lease, closing, the pathspec body commit, `list --json` over the store index, and the
pickup context the session-start hook adds."""

import json
from pathlib import Path

import pytest
from conftest import commit_count, git, make_repo, read_record, run_cli
from itemctl import hooks as item_hooks

from claude_tasks import store_index
from claude_tasks.hooks import context_response
from claude_tasks.schema import HANDOVER_KIND, store_toml_v1
from claude_tasks.store import open_store

Capsys = pytest.CaptureFixture[str]

ME = "session-me"
PEER = "session-peer"


@pytest.fixture
def repo(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo on `main` with no store yet; the cwd is inside it."""
    path = make_repo(isolated_env / "proj")
    monkeypatch.chdir(path)
    return path


def new(capsys: Capsys, title: str = "Port the parser", *extra: str) -> tuple[str, Path, Path]:
    """File a handover through the CLI; return its id, item.md and files directory."""
    argv = ("handover", "new", "--title", title, "--model", "opus", "--size", "M", *extra)
    code, out, err = run_cli(capsys, *argv)
    assert code == 0, err
    handover_id, item, files = out.splitlines()
    return handover_id, Path(item), Path(files)


def listed(capsys: Capsys, *extra: str) -> dict:
    """`handover list --json` as parsed JSON."""
    code, out, err = run_cli(capsys, "handover", "list", "--json", *extra)
    assert code == 0, err
    return json.loads(out)


def claim(capsys: Capsys, handover_id: str, session: str, *extra: str) -> tuple[int, str]:
    """Run `handover claim`; return its status and stderr."""
    code, _out, err = run_cli(
        capsys, "handover", "claim", handover_id, "--session", session, *extra
    )
    return code, err


# --- new --------------------------------------------------------------------------------------


def test_new_in_a_repo_without_a_store_creates_and_indexes_it(repo: Path, capsys: Capsys) -> None:
    """The first handover creates `.claude-tasks/`, records it in the index and commits the record
    from the template, with the branch and session; `files/agents/` exists but is not committed
    (allow)."""
    handover_id, item, files = new(capsys, "Port the parser", "--session", ME)

    root = repo / ".claude-tasks"
    assert handover_id == "PROJ-0001"
    assert item == root / "handover" / "port-the-parser" / "item.md"
    assert files == item.parent / "files"
    assert (files / "agents").is_dir()
    assert [s.root for s in store_index.stores().stores] == [root.resolve()]
    fm, body = read_record(item)
    assert fm["state"] == "open"
    assert (fm["model"], fm["size"], fm["cwd"]) == ("opus", "M", str(repo))
    assert (fm["branch"], fm["paused_session"]) == ("main", ME)
    assert body.startswith("# Handover — Port the parser\n")
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "ls-files", str(root / "handover")).splitlines() == [
        ".claude-tasks/handover/port-the-parser/item.md"
    ]


def test_new_with_the_main_checkout_off_its_default_branch_is_refused(
    repo: Path, capsys: Capsys
) -> None:
    """On a feature branch `new` exits 1 naming the default branch, and creates no store (deny)."""
    git(repo, "checkout", "-q", "-b", "feat/x")
    before = commit_count(repo)

    code, _out, err = run_cli(
        capsys, "handover", "new", "--title", "t", "--model", "opus", "--size", "S"
    )

    assert code == 1
    assert "'main'" in err
    assert "--home" not in err
    assert not (repo / ".claude-tasks").exists()
    assert commit_count(repo) == before


def test_new_upgrades_a_v1_store_first(repo: Path, capsys: Capsys) -> None:
    """A store still carrying the idea-only schema is upgraded in-process, then the handover is
    filed (allow)."""
    root = repo / ".claude-tasks"
    root.mkdir()
    (root / "store.toml").write_text(store_toml_v1("PROJ"))
    (root / ".gitignore").write_text(".lock\n")
    git(repo, "add", ".claude-tasks")
    git(repo, "commit", "-q", "-m", "v1 store")

    handover_id, item, _files = new(capsys)

    assert handover_id == "PROJ-0001"
    assert item.is_file()
    assert HANDOVER_KIND in {k.name for k in open_store(root).schema.kinds}
    assert "handover/**/files/" in (root / ".gitignore").read_text().splitlines()
    assert git(repo, "status", "--porcelain") == ""


def test_new_outside_a_repo_writes_the_home_store(isolated_env: Path, capsys: Capsys) -> None:
    """Outside any repository the handover goes to the home store."""
    elsewhere = isolated_env / "loose"
    elsewhere.mkdir()

    handover_id, item, _files = new(capsys, "Loose work", "--cwd", str(elsewhere))

    assert handover_id == "CT-0001"
    assert item.is_relative_to(isolated_env / "home-store")


def test_a_taken_slug_gets_the_time_appended(repo: Path, capsys: Capsys) -> None:
    """A second handover with the same title is filed as `<title> (HH:MM)`."""
    new(capsys, "Same work")
    second_id, item, _files = new(capsys, "Same work")

    fm, _body = read_record(item)
    assert second_id == "PROJ-0002"
    assert fm["title"].startswith("Same work (")
    assert item.parent.name.startswith("same-work-")


# --- claim, release, done ---------------------------------------------------------------------


def test_claim_against_another_live_lease_is_refused(repo: Path, capsys: Capsys) -> None:
    """A second session cannot claim while the first one's lease is live (deny)."""
    handover_id, item, _files = new(capsys)
    assert claim(capsys, handover_id, PEER)[0] == 0

    code, err = claim(capsys, handover_id, ME)

    assert code == 1
    assert PEER in err
    assert read_record(item)[0]["claimed_by"] == PEER


def test_claim_by_the_same_holder_renews(repo: Path, capsys: Capsys) -> None:
    """The holder claiming again keeps its lease and `claimed_at` (allow)."""
    handover_id, item, _files = new(capsys)
    assert claim(capsys, handover_id, ME)[0] == 0
    first = read_record(item)[0]

    assert claim(capsys, handover_id, ME)[0] == 0

    again = read_record(item)[0]
    assert again["claimed_by"] == ME
    assert again["claimed_at"] == first["claimed_at"]


def test_claim_force_takes_over_a_live_lease(repo: Path, capsys: Capsys) -> None:
    """`--force` releases the other session's lease and claims it (allow)."""
    handover_id, item, _files = new(capsys)
    assert claim(capsys, handover_id, PEER)[0] == 0

    assert claim(capsys, handover_id, ME, "--force")[0] == 0

    assert read_record(item)[0]["claimed_by"] == ME


def test_release_by_a_non_holder_is_refused(repo: Path, capsys: Capsys) -> None:
    """Only the holder releases (deny for another session, allow for the holder)."""
    handover_id, item, _files = new(capsys)
    assert claim(capsys, handover_id, PEER)[0] == 0

    code, _out, err = run_cli(capsys, "handover", "release", handover_id, "--session", ME)
    assert code == 1
    assert PEER in err
    assert read_record(item)[0]["claimed_by"] == PEER

    code, _out, err = run_cli(capsys, "handover", "release", handover_id, "--session", PEER)
    assert code == 0, err
    assert "claimed_by" not in read_record(item)[0]


def test_done_closes_once_and_refuses_twice(repo: Path, capsys: Capsys) -> None:
    """`done` moves the record (and its files) under `closed/`, dropping the lease; a second
    `done` is refused (deny)."""
    handover_id, _item, files = new(capsys)
    (files / "wip.patch").write_text("diff\n")
    assert claim(capsys, handover_id, ME)[0] == 0

    code, out, err = run_cli(capsys, "handover", "done", handover_id, "--closed-by", "abc123")
    assert code == 0, err
    closed = Path(out.strip())
    assert closed.parent.parent.name == "closed"
    fm, _body = read_record(closed)
    assert (fm["state"], fm["closed_by"]) == ("done", "abc123")
    assert "claimed_by" not in fm
    assert (closed.parent / "files" / "wip.patch").read_text() == "diff\n"

    code, _out, err = run_cli(capsys, "handover", "done", handover_id, "--closed-by", "abc123")
    assert code == 1
    assert "already done" in err


# --- set, path, commit ------------------------------------------------------------------------


def test_set_changes_a_settable_field_and_refuses_others(repo: Path, capsys: Capsys) -> None:
    """`set` writes `wip_commit` (allow) and refuses `title` and a model the kind lacks (deny)."""
    handover_id, item, _files = new(capsys)

    assert run_cli(capsys, "handover", "set", handover_id, "wip_commit=deadbeef")[0] == 0
    assert read_record(item)[0]["wip_commit"] == "deadbeef"
    assert run_cli(capsys, "handover", "set", handover_id, "title=x")[0] == 1
    assert run_cli(capsys, "handover", "set", handover_id, "model=gpt")[0] == 1


def test_path_resolves_by_id_from_outside_the_repo(
    repo: Path, isolated_env: Path, capsys: Capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An id is found through the index from another cwd; `--files` gives the files directory."""
    handover_id, item, files = new(capsys)
    monkeypatch.chdir(isolated_env)

    assert run_cli(capsys, "handover", "path", handover_id)[1].strip() == str(item)
    assert run_cli(capsys, "handover", "path", handover_id, "--files")[1].strip() == str(files)


def test_commit_commits_the_edited_item_only(repo: Path, capsys: Capsys) -> None:
    """`commit` commits the body edit and leaves other changes of the checkout alone; with nothing
    edited it commits nothing."""
    handover_id, item, _files = new(capsys)
    item.write_text(item.read_text() + "\nMore state.\n")
    (repo / "README").write_text("changed\n")
    before = commit_count(repo)

    assert run_cli(capsys, "handover", "commit", handover_id)[0] == 0

    assert commit_count(repo) == before + 1
    assert git(repo, "status", "--porcelain").strip() == "M README"
    code, _out, err = run_cli(capsys, "handover", "commit", handover_id)
    assert code == 0
    assert "nothing to commit" in err
    assert commit_count(repo) == before + 1


# --- list --------------------------------------------------------------------------------------


def test_list_json_shape(repo: Path, capsys: Capsys) -> None:
    """`list --json` is `{handovers, notes}`; each handover carries its frontmatter, the lease
    fields once claimed, `path`, `files_dir`, `store` and `store_prefix`."""
    handover_id, item, files = new(capsys, "Port the parser", "--session", ME)
    assert claim(capsys, handover_id, ME)[0] == 0

    report = listed(capsys)

    assert report["notes"] == []
    (entry,) = report["handovers"]
    assert entry["id"] == handover_id
    assert (entry["path"], entry["files_dir"]) == (str(item), str(files))
    assert entry["store"] == str((repo / ".claude-tasks").resolve())
    assert entry["store_prefix"] == "PROJ"
    assert entry["claimed_by"] == ME
    required = {"title", "created", "state", "model", "size", "cwd", "claimed_at", "lease_until"}
    assert required <= entry.keys()


def test_list_all_reads_the_index_and_reports_dangling_links(
    repo: Path, isolated_env: Path, capsys: Capsys
) -> None:
    """`--all` lists every indexed store's open handovers and notes each dangling link; `done`
    ones only with `--done`."""
    project_id, _item, _files = new(capsys, "In the project")
    loose = isolated_env / "loose"
    loose.mkdir()
    home_id, _item, _files = new(capsys, "In home", "--cwd", str(loose))
    (isolated_env / "store-index" / "GONE").symlink_to(isolated_env / "gone")
    assert run_cli(capsys, "handover", "done", home_id, "--closed-by", "x")[0] == 0

    report = listed(capsys, "--all")

    assert [h["id"] for h in report["handovers"]] == [project_id]
    assert len(report["notes"]) == 1
    assert "GONE" in report["notes"][0]
    assert "dangling" in report["notes"][0]
    assert {h["id"] for h in listed(capsys, "--all", "--done")["handovers"]} == {
        project_id,
        home_id,
    }


# --- the record guard and the pickup hook -------------------------------------------------------


def test_itemctl_guard_allows_files_and_denies_frontmatter_edits(
    repo: Path, capsys: Capsys
) -> None:
    """A Write creating `files/agents/x.md` passes the itemctl guard (allow); an Edit of the
    frontmatter of `item.md` is denied, naming the writer (deny); a body Edit passes."""
    _id, item, files = new(capsys)
    write = {"file_path": str(files / "agents" / "x.md"), "content": "notes\n"}
    assert item_hooks.pre_tool_use({"tool_name": "Write", "tool_input": write}) is None

    edit = {"file_path": str(item), "old_string": "size: M", "new_string": "size: L"}
    reason = item_hooks.pre_tool_use({"tool_name": "Edit", "tool_input": edit})
    assert reason is not None
    assert "claude-tasks" in reason

    body = {"file_path": str(item), "old_string": "## Goal", "new_string": "## Goal\nShip it."}
    assert item_hooks.pre_tool_use({"tool_name": "Edit", "tool_input": body}) is None


def payload(cwd: Path) -> str:
    """A SessionStart payload for a session at `cwd`."""
    return json.dumps({"hook_event_name": "SessionStart", "cwd": str(cwd), "session_id": ME})


def test_session_start_names_unclaimed_open_handovers(repo: Path, capsys: Capsys) -> None:
    """The hook's additionalContext lists open, unclaimed handovers with the commands to take one;
    a claimed one is left out, and with none left there is no context."""
    first, _item, _files = new(capsys, "First work")
    second, _item, _files = new(capsys, "Second work")
    assert claim(capsys, second, PEER)[0] == 0

    answer = context_response(payload(repo), "session-start")

    context = answer["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    assert f"{first} · opus/M" in context
    assert second not in context
    assert "claude-tasks handover claim <id>" in context
    assert run_cli(capsys, "handover", "done", first, "--closed-by", "x")[0] == 0
    assert "hookSpecificOutput" not in context_response(payload(repo), "session-start")
