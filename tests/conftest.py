"""Test isolation: every test runs against temp stores.

`isolated_env` points HOME, the XDG base directories and the claude-tasks environment overrides
into the test's tmp dir, so no test reaches a real store, the state directory or the user's
configuration; it also clears the session variables a Claude-run pytest inherits and fixes git's
identity. Where pytest-hermetic is installed (pyproject addopts), it additionally refuses any write
outside its temp root, in-process and in spawned Python children.
"""

import datetime
import re
import subprocess
from pathlib import Path

import pytest
from itemctl import record
from itemctl.model import thaw
from itemctl.store import Store

from claude_tasks.cli import main
from claude_tasks.ops import Written, add_task, init_store
from claude_tasks.schema import IDEA


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME, XDG and claude-tasks at temp locations and give git a fixed identity; return
    the temp base."""
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    for name, sub in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_DATA_HOME", ".local/share"),
    ):
        monkeypatch.setenv(name, str(tmp_path / "user-home" / sub))
    monkeypatch.setenv("CLAUDE_TASKS_HOME", str(tmp_path / "home-store"))
    monkeypatch.setenv("CLAUDE_TASKS_INDEX", str(tmp_path / "store-index"))
    monkeypatch.chdir(tmp_path)
    for name in ("CLAUDE_TASKS_DIR", "CLAUDE_TASKS_STATE", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@example.com"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@example.com"),
    ):
        monkeypatch.setenv(name, value)
    return tmp_path


@pytest.fixture
def home(isolated_env: Path) -> Store:
    """An initialized home store (prefix CT) in its own git repo."""
    return init_store(isolated_env / "home-store", "CT", commit=True)


def git(cwd: Path, *args: str) -> str:
    """Run git in cwd, returning stdout; fail the test on a git error."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def make_repo(path: Path) -> Path:
    """A git repository with one initial commit at path."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README").write_text("repo\n")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "init")
    return path


def commit_count(repo: Path) -> int:
    """The number of commits on HEAD."""
    return int(git(repo, "rev-list", "--count", "HEAD").strip())


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """Run the CLI in-process; return (exit status, stdout, stderr)."""
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def body_file(text: str = "A description.") -> str:
    """Write `text` where `add --body-file` can read it, returning the path.

    `add` takes its body from a file or from stdin and nowhere else, so a test that wants a body
    needs one on disk. It lands in the test's cwd, which `isolated_env` has pointed at tmp_path.
    """
    path = Path("body.md")
    path.write_text(text, encoding="utf-8")
    return str(path)


def idea(store: Store, title: str, state: str = "open", **fields: object) -> Written:
    """File an Idea through ops.add_task with a one-line description."""
    return add_task(store, title, state, fields, "A description.")


def read_record(path: Path) -> tuple[dict, str]:
    """A record file's frontmatter (plain dicts and lists) and body."""
    fields, body = record.parse(path.read_text(encoding="utf-8"), str(path))
    fm = thaw(fields)
    assert isinstance(fm, dict)
    return fm, body


def sections(body: str) -> dict[str, str]:
    """Each `## ` heading's text mapped to the raw content under it."""
    parts = re.split(r"^## (.+)$", body, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def today() -> str:
    """Today's local date, as a `--note` line spells it."""
    return datetime.datetime.now().astimezone().date().isoformat()


def patch_fm(path: Path, **fields: object) -> None:
    """Rewrite a record's frontmatter directly with `fields` merged in (None removes).

    Keys the schema does not manage are kept, the way the writer keeps them: a helper that
    dropped them would hide exactly the pass-through regression the contract forbids.
    """
    fm, body = read_record(path)
    fm.update(fields)
    path.write_text(record.render(IDEA, {k: v for k, v in fm.items() if v is not None}, body))
