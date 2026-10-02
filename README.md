# claude-tasks

A CLI that files, lists and starts deferred work, kept as markdown in git in an
[itemctl](https://github.com/Gerenuk/ItemCtl) store, which owns the files, their validation and the
commits. A store holds kinds of record, each with one writer: Idea records about scientific or
engineering work (`add`, `set`) and handovers (`claude-tasks handover`, the `/pause` skill's
record); a build that ships `friction_kind.py` also declares friction entries, written only by
`claude-ctl friction`. There are two kinds of store: the home store
`~/claude-tasks` (id prefix `CT`) for what belongs to no particular project, and a
`.claude-tasks/` inside a project's own repo for that project's records — in its main checkout,
also when you work in a linked worktree (a write from one prints a stderr line saying so). Inside a repo the git toplevel gives you its store, or
you name one with `--store`; each store keeps one id sequence, shared by its kinds. Every store is
also recorded in the index `~/.local/state/claude-tasks/stores/<PREFIX>` (a symlink to its root),
which only machine-wide reads use (`handover list --all`, finding an id's store by its prefix);
it never picks where a write goes.

## Install

```
uv tool install --with-executables-from itemctl git+https://github.com/Gerenuk/claude-tasks
```

puts `claude-tasks` and `itemctl` on `PATH`. The Claude Code plugin in this repository
(`.claude-plugin/`) adds the `/task` skill (`skills/task/`) and two hooks (`hooks/hooks.json`): at
SessionStart `claude-tasks hook session-start`, and before `Write`/`Edit` itemctl's record edit
guard `itemctl hook pre-tool-use`. Without the binaries the session start names the install
command instead. In a checkout,
`uv run pytest -q` runs the tests.

## Commands

| command | what it does |
|---|---|
| `claude-tasks` | the front door: what is waiting, ready first then open, oldest first |
| `claude-tasks add` | file a new record |
| `claude-tasks list` | dump every record of the view (`--json` for NDJSON, one object per record) |
| `claude-tasks show <ID>` | print a record, body included (`--json` for one object: the `list --json` record plus `body`) |
| `claude-tasks set <ID> [<ID> ...] ...` | change the fields, state or history note of one or more records; several ids are validated together and written in one commit |
| `claude-tasks check` | validate a store: itemctl's checks plus the legacy-id rules |
| `claude-tasks board` | render every record into one HTML page and open it |
| `claude-tasks upgrade` | move a store to the current schema (`--dry-run` shows the diff) |
| `claude-tasks handover ...` | the handover kind: `new`, `list`, `path`, `set`, `claim`, `release`, `done`, `commit` |
| `claude-tasks hook session-start` | answer a Claude Code SessionStart payload on stdin |

`docs/json-contract.md` is the durable description of `list --json` and `show --json`, the
machine-readable surfaces; `docs/scope.md` says what belongs in a store at all, and where — project store versus
home store.

## Board

`claude-tasks board` renders every record of the board's stores onto one page: four columns
(ready, open, parked, a closed column you can reveal), search across id/title/tags/body, chips to
toggle stores and sizes, a tag filter, and a click-through detail view with the record's full
frontmatter, its file path and its body.

```
claude-tasks board [--out PATH] [--no-open]
```

- `--out PATH` writes the page there instead of the default,
  `$XDG_CACHE_HOME/claude-tasks/board.html` (or `~/.cache/claude-tasks/board.html` when
  `XDG_CACHE_HOME` is unset).
- `--no-open` skips opening the page (it otherwise opens with `xdg-open`).

The board's stores are the home store plus whatever claude-tasks' own configuration,
`$XDG_CONFIG_HOME/claude-tasks/config.toml` (default `~/.config/claude-tasks/config.toml`), lists
under `[board]`:

```toml
[board]
stores = ["~/code/project-a/.claude-tasks", "~/code/project-b/.claude-tasks"]
```

The page is a single offline HTML file — no network, no server, works straight from `file://` —
built fresh each time you run the command. It is not live: a page left open in a browser goes
stale as records change, so rerun `claude-tasks board` to see the current state.
