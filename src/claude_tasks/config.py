"""claude-tasks' own configuration, and a new store's prefix.

The configuration is `$XDG_CONFIG_HOME/claude-tasks/config.toml` (`env.user_config_path`); its
only table is `[board]`, whose `stores` lists the stores the board shows beside the home store.
An absent file is an empty configuration; an unknown key or a malformed value raises naming the
file and the key. A store's own settings live in its `store.toml` (`schema.store_toml`).
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from claude_tasks.env import home_store_root, user_config_path
from claude_tasks.errors import TasksError
from claude_tasks.schema import PREFIX_RE, check_prefix

_TOP_KEYS = frozenset({"board"})


@dataclass(frozen=True)
class UserConfig:
    """A parsed, validated claude-tasks configuration."""

    board_stores: tuple[Path, ...] = ()


def _board_stores(data: dict[str, object], source: Path) -> tuple[Path, ...]:
    """The `[board]` table's `stores`, expanded and de-duplicated; `()` when `board` is absent.

    `board` must be a table whose only key is `stores`, a list of non-empty strings; a listed
    path resolving to the home store, or to another listed path, raises naming `source`.
    """
    if "board" not in data:
        return ()
    board = data["board"]
    if not isinstance(board, dict) or set(board) != {"stores"}:
        raise TasksError(
            f"{source}: board is {board!r}; expected a table whose only key is `stores`"
        )
    stores = board["stores"]
    if not isinstance(stores, list) or not all(isinstance(s, str) and s for s in stores):
        raise TasksError(
            f"{source}: board.stores is {stores!r}; expected a list of non-empty strings"
        )
    paths = tuple(Path(s).expanduser() for s in stores)
    home = home_store_root().expanduser().resolve()
    for raw, path in zip(stores, paths, strict=True):
        if path.resolve() == home:
            raise TasksError(
                f"{source}: board.stores lists the home store ({raw!r}); it is always included"
            )
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise TasksError(f"{source}: board.stores has a duplicate entry: {stores!r}")
    return paths


def parse_config(text: str, source: Path) -> UserConfig:
    """Parse and validate configuration text, raising naming `source` and the offending key."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TasksError(f"{source}: invalid TOML: {exc}") from exc
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise TasksError(f"{source}: unknown key(s) {unknown}; expected only {sorted(_TOP_KEYS)}")
    return UserConfig(board_stores=_board_stores(data, source))


def read_config() -> UserConfig:
    """The configuration at `env.user_config_path()`; empty when the file does not exist."""
    path = user_config_path()
    if not path.is_file():
        return UserConfig()
    return parse_config(path.read_text(encoding="utf-8"), path)


def derive_prefix(repo_dir: Path) -> str:
    """A new store's prefix: its repo directory name, uppercased, non-alphanumerics stripped.

    `~/Projects/QM-Theory` → `QMTHEORY`, `~/Projects/New QM` → `NEWQM`. The whole name rather than
    an abbreviation, because three-letter abbreviations collide across the repos and no registry
    exists to notice. Raise when nothing legal is left, rather than inventing a prefix.
    """
    name = repo_dir.name
    candidate = re.sub(r"[^A-Z0-9]", "", name.upper())
    try:
        return check_prefix(candidate)
    except TasksError as exc:
        raise TasksError(
            f"cannot derive a store prefix from directory name {name!r}: it leaves {candidate!r}, "
            f"which is not {PREFIX_RE.pattern!r}"
        ) from exc
