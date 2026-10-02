"""The pyright gate: editor LSP silence is not a check.

`pyright` only resolves `claude_tasks`'s first-party imports when its cwd is the repo root (it
then finds this repo's `.venv`); from any other cwd every first-party import comes back
`reportMissingImports`, which silently disables the whole attribute-error class the editor's LSP
is meant to catch (see FRICTION/2026-09-18-pyright-resolves-no-claude-tasks-import-so-a.md). This
test runs pyright the way that works — cwd = repo root — and turns a diagnostic in that
silent-breakage class into a test failure instead of editor silence. It checks `src` only: a
broken attribute or name in a test file already fails that test when it runs, and leaving `tests`
out halves pyright's run. Run with no `.venv` synced, it comes back as `reportMissingImports` on
every first-party import, which is itself the signal that the gate is not resolving this repo and
needs `uv sync` (or `uv run pytest`, which syncs first) before it can be trusted.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The rule classes an unresolved import silently disables: a deleted attribute, an undefined
# name, a bad call — plus the missing-import symptom itself, which doubles as the tripwire that
# pyright is actually resolving this repo rather than staying silently blind to it.
SILENT_BREAKAGE_RULES = frozenset(
    {
        "reportMissingImports",
        "reportAttributeAccessIssue",
        "reportUndefinedVariable",
        "reportCallIssue",
    }
)


def test_pyright_gate_catches_silent_breakage() -> None:
    """`pyright` from the repo root reports none of the silent-breakage-class diagnostics."""
    if shutil.which("pyright") is None:
        pytest.skip("pyright is not on PATH")
    proc = subprocess.run(
        ["pyright", "--outputjson", "src"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode in (0, 1), (
        f"pyright exited {proc.returncode} instead of reporting diagnostics:\n{proc.stderr}"
    )
    report: dict[str, Any] = json.loads(proc.stdout)
    diagnostics = report["generalDiagnostics"]
    hits = [d for d in diagnostics if d.get("rule") in SILENT_BREAKAGE_RULES]
    lines = [
        "{file}:{line}:{rule}: {message}".format(
            file=Path(d["file"]).relative_to(REPO_ROOT),
            line=d["range"]["start"]["line"] + 1,
            rule=d["rule"],
            message=d["message"].splitlines()[0],
        )
        for d in hits
    ]
    assert not hits, "pyright found silent-breakage-class diagnostics:\n" + "\n".join(lines)
