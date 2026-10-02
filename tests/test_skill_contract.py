"""Every `claude-tasks …` invocation in skills/**/*.md and hooks/hooks.json parses against the
real CLI: the subcommand (and, for `hook`/`run`, the sub-subcommand) is one `build_parser()`
knows, and every `--flag` token after it is that (sub)parser's own option string.

A markdown extraction walks inline code spans and fenced-code-block lines; a JSON extraction
walks hooks.json's `command` strings. Both strip nested shell substitutions (`$(...)`, `` `...` ``)
via `_strip_shell_substitutions` before tokenizing, then funnel through `_scan_tokens` — both are
unit-tested directly against synthetic markdown below. This repo's skills/ and hooks/hooks.json
are scanned here; test_live_contract.py scans this machine's installed skills and settings with
the same extractors.
"""

import argparse
import functools
import json
import re
from pathlib import Path

import pytest

from claude_tasks.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A code-span token that stands in for a real name in doc prose, never a subcommand or flag.
PLACEHOLDER_TOKENS = frozenset({"ID", "PATH", "...", "…"})

#: Shell tokens that end one command: whatever follows belongs to another program. Without this a
#: documented pipeline (`claude-tasks list --json | jq -r '...'`) has the downstream program's own
#: flags attributed to the preceding claude-tasks subcommand and reported as unknown.
COMMAND_SEPARATORS: frozenset[str] = frozenset({"|", "|&", "||", "&&", "&", ";", ">", ">>", "<"})

#: Tokens that may stand between the start of a command and the program name — a prompt marker or
#: a launcher. Anything else before a claude-tasks-shaped token makes it an argument, not the
#: program: see `_is_invocation`.
COMMAND_PREFIXES: frozenset[str] = frozenset({"$", "sudo", "time", "env", "exec", "xargs"})

FENCE_RE = re.compile(r"^\s*```")
SPAN_RE = re.compile(r"`([^`\n]+)`")


def _is_placeholder(token: str) -> bool:
    """Whether `token` is a doc placeholder (`<...>`, `ID`, `PATH`, `...`, `…`)."""
    if token in PLACEHOLDER_TOKENS:
        return True
    return token.startswith("<") and token.endswith(">")


def _is_claude_tasks(token: str) -> bool:
    """Whether `token` names the claude-tasks binary, bare or as a path ending in `/claude-tasks`."""
    return token == "claude-tasks" or token.rsplit("/", 1)[-1] == "claude-tasks"


def _is_invocation(tokens: list[str], i: int) -> bool:
    """Whether `tokens[i]` names the binary *in command position*.

    The spelling alone cannot tell the program from a path that ends the same way: the home store
    is `~/claude-tasks` and the binary is `~/.local/bin/claude-tasks`. A program name stands first,
    after a command separator, or after a launcher — so `git -C ~/claude-tasks commit -m ...` names
    a directory, and its `commit` is git's subcommand rather than an unknown one of ours.
    """
    if not _is_claude_tasks(tokens[i]):
        return False
    if i == 0:
        return True
    previous = tokens[i - 1]
    return previous in COMMAND_SEPARATORS or previous in COMMAND_PREFIXES


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    """`parser`'s own nested subcommands action (e.g. `run`'s `new`/`path`), else None."""
    group = parser._subparsers  # noqa: SLF001 - argparse exposes no public walk of subparsers
    if group is None:
        return None
    for action in group._group_actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _choice_positional(parser: argparse.ArgumentParser) -> argparse.Action | None:
    """A positional action with fixed `choices` (e.g. `hook`'s `event`), else None."""
    for action in parser._get_positional_actions():  # noqa: SLF001
        if action.choices is not None:
            return action
    return None


def _option_strings(parser: argparse.ArgumentParser) -> frozenset[str]:
    """Every option string `parser` accepts, including inherited `parents=` and `-h`/`--help`."""
    return frozenset(parser._option_string_actions.keys())  # noqa: SLF001


def _candidate_flags(token: str) -> list[str]:
    """`token` as the flag(s) it names once brackets, pipes and a trailing `=…` are stripped."""
    flags = []
    for part in token.strip("[]").split("|"):
        part = part.split("=", 1)[0].strip("[]")
        if part.startswith("-") and part not in ("-", "--"):
            flags.append(part)
    return flags


@functools.cache
def _top_parser() -> argparse.ArgumentParser:
    """The real CLI's parser, built once: the scan only reads it, and a build (~20 ms) per code
    span dominated the scan's runtime."""
    return build_parser()


@functools.cache
def _top_choices() -> dict[str, argparse.ArgumentParser]:
    """The real CLI's top-level subcommand name -> subparser mapping."""
    action = _subparsers_action(_top_parser())
    assert action is not None, "claude-tasks build_parser() has no top-level subcommands"
    return dict(action.choices)


def _strip_shell_substitutions(text: str) -> str:
    """`text` with every `$(...)` (nesting-aware) and backtick-quoted `` `...` `` shell
    substitution removed. A substitution's contents are a nested shell invocation, never part of
    claude-tasks' own argv, so its flags (e.g. the `--show-current` in `$(git branch
    --show-current)`) must not be mistaken for one."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "`":
            end = text.find("`", i + 1)
            i = end + 1 if end != -1 else n
            continue
        if text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth > 0:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _scan_tokens(tokens: list[str], location: str) -> list[str]:
    """Every contract violation among `tokens` (one code span or code line), as
    `"{location}: ..."` strings naming the subcommand and the offending flag or subcommand."""
    top_choices = _top_choices()
    top_flags = _option_strings(_top_parser())
    violations = []
    i = 0
    while i < len(tokens):
        if not _is_invocation(tokens, i):
            i += 1
            continue
        i += 1
        while i < len(tokens) and tokens[i].startswith("-"):
            for flag in _candidate_flags(tokens[i]):
                if flag not in top_flags:
                    violations.append(f"{location}: claude-tasks: unknown flag {flag!r}")
            i += 1
        if i >= len(tokens):
            continue
        subcmd = tokens[i]
        if _is_placeholder(subcmd):
            i += 1
            continue
        if subcmd not in top_choices:
            violations.append(f"{location}: unknown subcommand {subcmd!r}")
            i += 1
            continue
        i += 1
        parser = top_choices[subcmd]
        label = subcmd
        nested = _subparsers_action(parser)
        if nested is not None:
            if i < len(tokens) and not _is_placeholder(tokens[i]) and not tokens[i].startswith("-"):
                subsub = tokens[i]
                if subsub not in nested.choices:
                    violations.append(f"{location}: {subcmd}: unknown subcommand {subsub!r}")
                else:
                    parser = nested.choices[subsub]
                    label = f"{subcmd} {subsub}"
                    i += 1
        else:
            positional = _choice_positional(parser)
            if (
                positional is not None
                and i < len(tokens)
                and not _is_placeholder(tokens[i])
                and not tokens[i].startswith("-")
            ):
                value = tokens[i]
                if value not in positional.choices:
                    violations.append(f"{location}: {subcmd}: unknown subcommand {value!r}")
                i += 1
        allowed = _option_strings(parser)
        while (
            i < len(tokens)
            and not _is_invocation(tokens, i)
            and tokens[i] not in COMMAND_SEPARATORS
        ):
            for flag in _candidate_flags(tokens[i]):
                if flag not in allowed:
                    violations.append(f"{location}: {label}: unknown flag {flag!r}")
            i += 1
    return violations


def _markdown_violations(text: str, label: str) -> list[str]:
    """Every contract violation among `text`'s inline code spans and fenced-code-block lines."""
    violations = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        loc = f"{label}:{lineno}"
        if in_fence:
            violations += _scan_tokens(_strip_shell_substitutions(line).split(), loc)
        else:
            for span in SPAN_RE.findall(line):
                violations += _scan_tokens(_strip_shell_substitutions(span).split(), loc)
    return violations


def _command_strings(node: object) -> list[str]:
    """Every `type: command` hook's command string under `node`, in document order.

    One walk for both registries that call this CLI: the repo's `hooks/hooks.json` manifest and
    the live `~/.claude/settings.json`, which share a shape.
    """
    found: list[str] = []
    if isinstance(node, dict):
        command = node.get("command")
        if node.get("type") == "command" and isinstance(command, str):
            found.append(command)
        for value in node.values():
            found.extend(_command_strings(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_command_strings(item))
    return found


def _hooks_json_violations(text: str, label: str) -> list[str]:
    """Every contract violation among a hook registry's `command` strings."""
    violations = []
    lines = text.splitlines()

    def line_of(needle: str) -> int:
        """The 1-based line on which `needle` first appears in the raw registry text.

        A command containing quotes is escaped in the JSON source, so the decoded string is not
        in the raw text verbatim; the encoded spelling is what to look for then.
        """
        for candidate in (needle, json.dumps(needle)[1:-1]):
            for lineno, line in enumerate(lines, start=1):
                if candidate in line:
                    return lineno
        raise AssertionError(f"{label}: command {needle!r} not found in its own raw text")

    for command in _command_strings(json.loads(text)):
        tokens = _strip_shell_substitutions(command).split()
        violations.extend(_scan_tokens(tokens, f"{label}:{line_of(command)}"))
    return violations


def test_skill_invocations_match_the_real_cli() -> None:
    """Every claude-tasks call in this repo's skills/ docs and hooks/hooks.json parses.

    A glob that silently returns nothing is exactly how a scan stops checking without anyone
    noticing, so a `skills/` directory that exists but holds no .md is a failure."""
    violations: list[str] = []
    skills_dir = REPO_ROOT / "skills"
    skill_docs = sorted(skills_dir.glob("**/*.md")) if skills_dir.is_dir() else []
    assert not (skills_dir.is_dir() and not skill_docs), (
        f"{skills_dir} exists but holds no .md: the scan would pass having checked nothing"
    )
    for path in skill_docs:
        label = str(path.relative_to(REPO_ROOT))
        violations += _markdown_violations(path.read_text(encoding="utf-8"), label)
    hooks_path = REPO_ROOT / "hooks" / "hooks.json"
    violations += _hooks_json_violations(
        hooks_path.read_text(encoding="utf-8"), str(hooks_path.relative_to(REPO_ROOT))
    )
    assert violations == []


def test_extractor_reports_a_bogus_flag() -> None:
    """A flag the target subparser does not define is reported."""
    violations = _markdown_violations("See `claude-tasks list --bogus-flag`.", "synthetic.md")
    assert any("--bogus-flag" in v for v in violations)


def test_extractor_reports_a_bogus_subcommand() -> None:
    """A top-level word that is not a real subcommand is reported."""
    violations = _markdown_violations("Run `claude-tasks frobnicate ID`.", "synthetic.md")
    assert any("frobnicate" in v for v in violations)


def test_extractor_reports_a_bogus_hook_subsubcommand() -> None:
    """A `hook` event outside its fixed choices is reported."""
    violations = _markdown_violations(
        "Wire it: `claude-tasks hook not-a-real-event`.", "synthetic.md"
    )
    assert any("not-a-real-event" in v for v in violations)


def test_extractor_passes_a_correct_invocation() -> None:
    """A real subcommand with only its own flags reports nothing."""
    assert (
        _markdown_violations("List them: `claude-tasks list --home --json`.", "synthetic.md") == []
    )


def test_extractor_ignores_a_nested_shell_substitution() -> None:
    """A `$(...)` substitution's own flags (e.g. the `--show-current` inside `$(git branch
    --show-current)`) are stripped before tokenizing, never mistaken for a claude-tasks flag."""
    violations = _markdown_violations(
        'Run `claude-tasks add --title "$(basename $(pwd))" '
        '--field branch="$(git branch --show-current)" --body-file F`.',
        "synthetic.md",
    )
    assert violations == []


def test_extractor_still_catches_a_bogus_flag_beside_a_substitution() -> None:
    """Stripping `$(...)` substitutions must not hide a real bogus flag elsewhere on the line."""
    violations = _markdown_violations(
        'Run `claude-tasks add --title "$(pwd)" --bogus-flag --body-file F`.',
        "synthetic.md",
    )
    assert any("--bogus-flag" in v for v in violations)


def test_extractor_ignores_flags_of_a_downstream_pipeline_stage() -> None:
    """A pipe ends the claude-tasks command: `jq -r` and `duckdb -c` are not `list`'s flags."""
    violations = _markdown_violations(
        "Query it: `claude-tasks list --home --json | jq -r 'select(.state==\"ready\")'`.",
        "synthetic.md",
    )
    assert violations == []


def test_extractor_still_catches_a_bogus_flag_before_a_pipe() -> None:
    """Stopping at the pipe must not hide a bogus flag on the claude-tasks side of it."""
    violations = _markdown_violations(
        "Run `claude-tasks list --bogus-flag | jq -r .`.", "synthetic.md"
    )
    assert any("--bogus-flag" in v for v in violations)


def test_extractor_ignores_the_store_path_as_a_program_name() -> None:
    """`~/claude-tasks` is the home store, not the binary: git's `commit` is not our subcommand."""
    violations = _markdown_violations(
        'Persist it: `git -C ~/claude-tasks commit -m "..." -- runs/r.md`.', "synthetic.md"
    )
    assert violations == []


def test_extractor_still_catches_an_invocation_after_a_separator() -> None:
    """Command position includes the token after a separator, so a real call there is still read."""
    violations = _markdown_violations(
        "Run `cd /tmp && claude-tasks bogus-subcommand`.", "synthetic.md"
    )
    assert any("bogus-subcommand" in v for v in violations)


#: The API's limits on a skill's frontmatter: `name` is at most 64 lowercase letters, digits and
#: hyphens and names no reserved word; `description` is non-empty and at most 1024 characters.
SKILL_NAME_RE = re.compile(r"[a-z0-9-]{1,64}")
SKILL_NAME_RESERVED = ("anthropic", "claude")
SKILL_DESCRIPTION_MAX = 1024


def _frontmatter(text: str) -> dict[str, str]:
    """`text`'s leading `---`-fenced frontmatter as single-line `key: value` pairs."""
    lines = text.splitlines()
    assert lines and lines[0] == "---", "SKILL.md does not open with a `---` frontmatter fence"
    end = lines.index("---", 1)
    return dict(line.split(": ", 1) for line in lines[1:end] if ": " in line)


def _metadata_violations(text: str) -> list[str]:
    """Every way `text`'s frontmatter breaks the API's `name`/`description` limits."""
    meta = _frontmatter(text)
    name = meta.get("name", "")
    description = meta.get("description", "")
    violations = []
    if not SKILL_NAME_RE.fullmatch(name):
        violations.append(f"name {name!r} is not 1-64 lowercase letters, digits and hyphens")
    violations += [f"name {name!r} contains {w!r}" for w in SKILL_NAME_RESERVED if w in name]
    if not description.strip():
        violations.append("description is empty")
    if len(description) > SKILL_DESCRIPTION_MAX:
        violations.append(f"description is {len(description)} chars, over {SKILL_DESCRIPTION_MAX}")
    return violations


_SKILL_FILES = sorted((REPO_ROOT / "skills").glob("*/SKILL.md"))


@pytest.mark.parametrize("path", _SKILL_FILES, ids=[p.parent.name for p in _SKILL_FILES])
def test_skill_metadata_fits_the_api_limits(path: Path) -> None:
    """Each of this repo's SKILL.md files has a valid `name` and a `description` of 1-1024 chars."""
    assert _metadata_violations(path.read_text(encoding="utf-8")) == []


def test_metadata_check_rejects_a_long_description_and_a_bad_name() -> None:
    """A 1025-char description and an uppercase name are both reported."""
    text = f"---\nname: Task\ndescription: {'x' * 1025}\n---\n"
    assert len(_metadata_violations(text)) == 2


def test_metadata_check_passes_a_description_at_the_limit() -> None:
    """A 1024-char description under a valid name reports nothing."""
    text = f"---\nname: task\ndescription: {'x' * 1024}\n---\n"
    assert _metadata_violations(text) == []
