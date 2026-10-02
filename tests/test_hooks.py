"""The SessionStart line: what it counts, which stores it reads, and how it fails."""

import datetime
import io
import json
from pathlib import Path

import pytest
from conftest import idea, make_repo, patch_fm, run_cli, today
from itemctl.store import Store

from claude_tasks.errors import TasksError
from claude_tasks.hooks import context_response, run_context_hook, session_start_line
from claude_tasks.ops import Written, init_store

ME = "0f2d9c9e-4a6b-4d7e-9a1b-2c3d4e5f6a7b"


def event(name: str, cwd: Path, session_id: str = ME) -> str:
    """A hook payload."""
    return json.dumps({"hook_event_name": name, "cwd": str(cwd), "session_id": session_id})


def ready(store: Store, title: str) -> None:
    """A decided, actionable idea."""
    idea(store, title, state="ready", decided_by="owner", reason="worth it")


def aged(written: Written, days: int) -> None:
    """Backdate a filed record's `created` by `days`."""
    created = datetime.date.fromisoformat(today()) - datetime.timedelta(days=days)
    patch_fm(written.path, created=datetime.datetime.combine(created, datetime.time()).astimezone())


def test_line_counts_open_and_parked_with_their_oldest_age(home: Store, isolated_env: Path) -> None:
    """Open and parked ideas are counted with their oldest age, so a stale backlog is announced."""
    aged(idea(home, "open old"), 40)
    aged(idea(home, "open new"), 3)
    aged(idea(home, "parked one", state="parked", trigger="later"), 90)
    assert session_start_line(isolated_env) == "ideas: 2 open (oldest 40d) · 1 parked (oldest 90d)"
    ready(home, "decided")
    assert session_start_line(isolated_env) == (
        "ideas: 1 ready · 2 open (oldest 40d) · 1 parked (oldest 90d)"
    )


def test_line_is_silent_when_every_idea_is_closed(
    home: Store, isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only closed ideas: nothing waits, so the hook says nothing."""
    written = idea(home, "done one")
    set_argv = ("set", written.task_id, "state=closed", "outcome=built", "reason=did it")
    assert run_cli(capsys, *set_argv)[0] == 0
    assert session_start_line(isolated_env) is None


def test_undated_record_fails_loud(home: Store, isolated_env: Path) -> None:
    """A `created` that is no timestamp is named, not aged as zero."""
    patch_fm(idea(home, "broken").path, created="someday")
    with pytest.raises(TasksError, match="someday"):
        session_start_line(isolated_env)


def test_line_counts_ready_ideas_of_both_roots(home: Store, isolated_env: Path) -> None:
    """Inside a repo with a store the line counts that store and the home store together."""
    ready(home, "home one")
    repo = make_repo(isolated_env / "proj")
    project = init_store(repo / ".claude-tasks", "PR", commit=True)
    ready(project, "project one")
    ready(project, "project two")
    assert session_start_line(repo) == "ideas: 3 ready"
    assert session_start_line(isolated_env) == "ideas: 1 ready"


def test_line_outside_any_store_says_nothing(isolated_env: Path) -> None:
    """With no home store on disk the hook reads nothing and creates nothing."""
    assert session_start_line(isolated_env) is None
    assert not (isolated_env / "home-store").exists()


def test_session_start_response_carries_the_line(home: Store, isolated_env: Path) -> None:
    """A SessionStart payload answers with a systemMessage, or {} when there is nothing to say."""
    assert context_response(event("SessionStart", isolated_env), "session-start") == {}
    ready(home, "one")
    response = context_response(event("SessionStart", isolated_env), "session-start")
    assert response == {"systemMessage": "ideas: 1 ready"}


def test_session_start_response_never_carries_additional_context(
    home: Store, isolated_env: Path
) -> None:
    """The line goes to the user, not the model: no additionalContext, ever."""
    ready(home, "one")
    response = context_response(event("SessionStart", isolated_env), "session-start")
    assert "additionalContext" not in response
    assert "hookSpecificOutput" not in response


def test_mismatched_event_fails_open(
    home: Store, isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A payload for another event answers a systemMessage marked hookFailed, never raising."""
    response = run_context_hook("session-start", event("UserPromptSubmit", isolated_env))
    assert "systemMessage" in response and "answers SessionStart" in str(response["systemMessage"])
    assert response["hookFailed"] is True
    assert "hook failed" in capsys.readouterr().err


def test_user_prompt_submit_is_no_longer_a_hook(
    home: Store, isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The deleted subcommand is refused by the parser rather than quietly answering nothing."""
    with pytest.raises(SystemExit):
        run_cli(capsys, "hook", "user-prompt-submit")


def test_hook_cli_prints_json(
    home: Store,
    isolated_env: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claude-tasks hook session-start` prints `{}` when no idea is ready."""
    monkeypatch.setattr("sys.stdin", io.StringIO(event("SessionStart", isolated_env)))
    assert run_cli(capsys, "hook", "session-start")[:2] == (0, "{}\n")
