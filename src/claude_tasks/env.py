"""The locations claude-tasks reads from the environment, each with its default.

`CLAUDE_TASKS_HOME` and `CLAUDE_TASKS_INDEX` exist for tests; set but empty, they raise rather than
resolving to the current directory. The configuration file follows `XDG_CONFIG_HOME` and the store
index `XDG_STATE_HOME`, each falling back to its `~/.config` / `~/.local/state` default when unset
or empty, as the XDG base-directory spec says.
"""

import os
from pathlib import Path

from claude_tasks.errors import TasksError


def _env_path(name: str, default: Path) -> Path:
    """The absolute path in environment variable `name`, or `default` when it is unset."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw:
        raise TasksError(f"${name} is set but empty; expected an absolute path or unset")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise TasksError(f"${name} is {raw!r}; expected an absolute path")
    return path


def home_store_root() -> Path:
    """The home store directory: `$CLAUDE_TASKS_HOME`, default `~/claude-tasks`."""
    return _env_path("CLAUDE_TASKS_HOME", Path.home() / "claude-tasks")


def _xdg_base(name: str, default: Path) -> Path:
    """The XDG base directory in `name`, or `default` when it is unset or empty."""
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def store_index_dir() -> Path:
    """The store index directory: `$CLAUDE_TASKS_INDEX`, default
    `$XDG_STATE_HOME/claude-tasks/stores`."""
    state = _xdg_base("XDG_STATE_HOME", Path.home() / ".local" / "state")
    return _env_path("CLAUDE_TASKS_INDEX", state / "claude-tasks" / "stores")


def user_config_path() -> Path:
    """claude-tasks' configuration file: `$XDG_CONFIG_HOME/claude-tasks/config.toml`."""
    base = _xdg_base("XDG_CONFIG_HOME", Path.home() / ".config")
    return base / "claude-tasks" / "config.toml"
