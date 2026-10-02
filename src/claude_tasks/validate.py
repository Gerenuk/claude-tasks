"""The rules about `legacy_ids` only claude-tasks knows, and `check_store` for the CLI's `check`.

itemctl validates everything `store.toml` declares — ids, slugs, states and their fields, placement
under `closed/`, stray files. What it cannot know is that an old id must still resolve to exactly
one record (`lookup.find_task` accepts one), and that itemctl allocates ids above live ids only,
so a retired id of this store's own prefix numbered above every live one would be issued again.
`store.legacy_findings` checks both and is wired into every store as an itemctl caller rule
(`store.open_store`), so a write that would create one of these problems is refused before it
reaches disk.
"""

from itemctl.store import Store


def check_store(store: Store) -> list[str]:
    """Every finding of a store on disk: itemctl's validation, overdue items, and the legacy-id
    rules, since `store.check` already runs `legacy_findings` as one of its caller rules."""
    return [f"{store.root / f.where}: {f.message}" for f in store.check()]
