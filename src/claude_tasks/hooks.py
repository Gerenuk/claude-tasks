"""`hook session-start`: one line telling the user what is waiting and how long the backlog has aged.

It reads the two roots directly — the store at the session's git toplevel, if that repo has one,
and the home store — with no `project`-field matching and no name→path lookup, because a repo
store's records carry no `project` at all. A root that holds no store is simply not read.

The line is `ideas: 3 ready · 50 open (oldest 120d) · 10 parked (oldest 60d)`, a state left out
when it holds nothing, and nothing at all when every idea is closed. Nothing else resurfaces a
parked idea -- its trigger is prose no code evaluates -- so the ages are how an ageing backlog
reaches the user instead of deteriorating silently. An age counts from `created`, the same date
the front door orders by.
It answers as a `systemMessage` — for the user, not the model, since the count is nothing the
model needs to reason about — and never as `additionalContext`. Firing only on a fresh start
(not `resume` or `compact`) is the job of the hook's own SessionStart matcher in
`~/.claude/settings.json`, the one place that filter lives; this module answers whatever
SessionStart payload it is given.

The same answer carries, as `additionalContext` for the model, the unclaimed open handovers of the
store a write from the session's cwd goes to (`handover.pickup_context`), so a session started
in a paused project is told how to continue it. A failure reading them is reported in that
context instead, and never hides the ideas line.

The frontmatter guard is itemctl's (`itemctl hook pre-tool-use`).
Any failure here answers a `systemMessage` with `"hookFailed": true` and exits 0.
"""

import datetime
import json
import sys
from pathlib import Path

from itemctl.model import ItemError

from claude_tasks.errors import TasksError
from claude_tasks.handover import pickup_context
from claude_tasks.store import Task, existing_stores, load_tasks

#: The states the line counts, in order; each `True` one also shows its oldest record's age.
ANNOUNCED_STATES: tuple[tuple[str, bool], ...] = (
    ("ready", False),
    ("open", True),
    ("parked", True),
)

#: Every hook event Claude Code 2.1.267 names; a payload naming anything else is reported.
HOOK_EVENTS = frozenset({
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch", "Notification",
    "UserPromptSubmit", "UserPromptExpansion", "SessionStart", "SessionEnd", "Stop",
    "StopFailure", "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
    "PreModelSwitch", "PostModelSwitch", "PermissionRequest", "PermissionDenied", "Setup",
    "TeammateIdle", "TaskCreated", "TaskCompleted", "Elicitation", "ElicitationResult",
    "ConfigChange", "WorktreeCreate", "WorktreeRemove", "InstructionsLoaded", "CwdChanged",
    "FileChanged", "DirectoryAdded", "MessageDisplay",
})  # fmt: skip

#: The event each `hook` subcommand this module answers is for.
SUBCOMMAND_EVENTS: dict[str, str] = {"session-start": "SessionStart"}


def _age_days(task: Task, today: datetime.date) -> int:
    """Days since the record was filed; raise naming the record when `created` is no timestamp."""
    created = task.fm.get("created")
    if not isinstance(created, datetime.datetime):
        raise TasksError(f"{task.path}: created {created!r} is not a timestamp")
    return (today - created.date()).days


def session_start_line(cwd: Path) -> str | None:
    """The waiting-ideas line for the stores a session at `cwd` reads, or None when none waits."""
    tasks = [task for store in existing_stores(cwd) for task in load_tasks(store)]
    now = datetime.datetime.now().astimezone().date()
    parts: list[str] = []
    for state, show_age in ANNOUNCED_STATES:
        ages = [_age_days(task, now) for task in tasks if task.state == state]
        if ages:
            parts.append(f"{len(ages)} {state}" + (f" (oldest {max(ages)}d)" if show_age else ""))
    return "ideas: " + " · ".join(parts) if parts else None


def payload_str(payload: dict[str, object], name: str) -> str:
    """The non-empty string field `name` of a hook payload; raise naming the field otherwise."""
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise TasksError(f"hook payload field {name!r} must be a non-empty string, got {value!r}")
    return value


def parse_payload(raw: str, subcommand: str) -> dict[str, object]:
    """The hook payload as a mapping whose hook_event_name matches the subcommand."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TasksError(f"hook payload is not JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise TasksError(f"hook payload must be a JSON object, got {type(payload).__name__}")
    if subcommand not in SUBCOMMAND_EVENTS:
        raise TasksError(
            f"hook {subcommand!r} is unknown; expected one of {list(SUBCOMMAND_EVENTS)}"
        )
    event = payload_str(payload, "hook_event_name")
    if event not in HOOK_EVENTS:
        raise TasksError(f"hook_event_name {event!r} is not a Claude Code hook event")
    if event != SUBCOMMAND_EVENTS[subcommand]:
        raise TasksError(
            f"`claude-tasks hook {subcommand}` answers {SUBCOMMAND_EVENTS[subcommand]}, "
            f"got a {event} payload"
        )
    return payload


def handover_context(cwd: Path) -> str | None:
    """The handover pickup context for `cwd`, or a line naming why it could not be read."""
    try:
        return pickup_context(cwd)
    except (TasksError, ItemError) as exc:
        return f"claude-tasks handover pickup failed: {exc}"


def context_response(raw: str, subcommand: str) -> dict[str, object]:
    """The JSON answer to a SessionStart payload: the ideas line as a systemMessage and the
    handover pickup as additionalContext, each only when there is something to say."""
    payload = parse_payload(raw, subcommand)
    event = SUBCOMMAND_EVENTS[subcommand]
    if event != "SessionStart":
        raise TasksError(f"context_response does not answer {event}")
    cwd = Path(payload_str(payload, "cwd"))
    pickup = handover_context(cwd)
    line = session_start_line(cwd)
    response: dict[str, object] = {} if line is None else {"systemMessage": line}
    if pickup is not None:
        response["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": pickup}
    return response


def fail_open(what: str, exc: BaseException) -> dict[str, object]:
    """The fail-open answer: a systemMessage naming the failure, also warned on stderr, and
    `"hookFailed": true`, the top-level key the hook telemetry counts as a failed launch."""
    detail = str(exc) if isinstance(exc, TasksError) else f"{type(exc).__name__}: {exc}"
    message = f"claude-tasks {what} hook failed: {detail}"
    print(message, file=sys.stderr)
    return {"systemMessage": message, "hookFailed": True}


def run_context_hook(subcommand: str, raw: str) -> dict[str, object]:
    """Answer one context hook, failing open."""
    try:
        return context_response(raw, subcommand)
    except Exception as exc:  # noqa: BLE001 - the hook contract: fail open, but loudly
        return fail_open(subcommand, exc)
