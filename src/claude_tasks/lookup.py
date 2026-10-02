"""Finding an idea by its id or by an id it used to have; an id naming another kind (a handover,
a friction entry) is refused with the command that owns it (`ops.refuse_non_idea`)."""

from collections.abc import Sequence

from itemctl.store import Store

from claude_tasks.errors import TasksError
from claude_tasks.ops import refuse_non_idea
from claude_tasks.store import Task


def matches_ref(task: Task, ref: str) -> bool:
    """Whether `ref` names this record, by its current id or by one the migration retired."""
    return ref == task.id or ref in task.legacy_ids()


def legacy_notice(task: Task, ref: str) -> str | None:
    """The notice a lookup by an old id prints: which record holds it now; None for a current id."""
    if ref in task.legacy_ids():
        return f"claude-tasks: {ref} is now {task.id} ({task.title})"
    return None


def find_task(stores: Sequence[Store], ref: str) -> Task:
    """The one idea `ref` names in `stores`; raise when none or several do, or when the record it
    names is another kind.

    An unparsable sibling file does not stop the lookup: `check` is what reports it.
    """
    matches = [
        task
        for store in stores
        for item in store.snapshot().items
        if matches_ref(task := Task(store, item), ref)
    ]
    if len(matches) == 1:
        refuse_non_idea(matches[0].item)
        return matches[0]
    if len(matches) > 1:
        raise TasksError(f"{ref!r} is ambiguous: it names {[f'{t.id} {t.path}' for t in matches]}")
    raise TasksError(
        f"no record matches {ref!r} (by id or a legacy id) in "
        + ", ".join(str(store.root) for store in stores)
    )
