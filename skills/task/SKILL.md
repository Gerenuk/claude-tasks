---
name: task
description: Every operation on the owner's Idea records — file one, change its state or fields, look one up, survey them (a list, NDJSON, or one searchable HTML board of every record), validate a store. Use when work needs to be deferred rather than done now and something must remember it, when a record already filed needs its state or fields changed, or when you need to know what is already recorded before raising something as new. Records are markdown in git, in one of two stores (a project's `.claude-tasks/`, or the home store `~/claude-tasks`); the `claude-tasks` CLI is their only writer, so a record is never hand-written or hand-edited above the body.
---

# Task

Idea records are markdown files in git, managed by the `claude-tasks` CLI. This skill is that
CLI's whole agent-facing surface: nothing else needs to know its flags.

**What belongs here and what does not** — an Idea is work worth doing that is not being done now.
A defect in how the work *runs* is friction, not an idea: it goes wherever this setup tracks
defects (where claude-ctl is installed, `claude-ctl friction file`), never into an idea.

**This skill writes ideas only.** A store also holds handovers (under `handover/`, written by
`claude-tasks handover`, the `/pause` skill's record) and, where claude-ctl is installed,
friction entries (under `friction/`, written only by `claude-ctl friction`). `add`, `set`, `show` and `list`
see ideas alone, and refuse another kind's id, naming the command that owns it.
`${CLAUDE_SKILL_DIR}/docs/scope.md` is the canonical boundary; read it before answering "does
this belong here?" and never restate it from memory.

## The record

Five fields are always present, and the CLI writes all five — you supply only the title:
`id`, `type`, `title`, `state`, `created` (a timestamp; `list --json` shows its date).

| state | lives at | needs, beyond the five |
|---|---|---|
| `open` | store root | — |
| `ready` | store root | `decided_by`, `reason` |
| `parked` | store root | `trigger` **or** `blocked_by` |
| `closed` | `closed/` | `outcome` (`built`, `dropped` or `superseded`), `reason` |

`trigger` and `blocked_by` exist only while parked, `outcome` only while closed; a `set` that
leaves the state drops them, so a reopened record never keeps the outcome of the closure it left.
`id`, `type`, `created` and `legacy_ids` never change.

**Every other field is yours.** `--field K=V` carries any key through unchanged and unvalidated —
`tags`, `size`, `touches`, `source`, `source_runs`, `autopilot`, anything a caller needs — into the
record, into the dump, and back out. The CLI neither invents these nor polices them.

**The body is free-form markdown.** Nothing checks its headings. Write whatever the record's reader
will need.

## Which store

Two roots. With no flag a read sees this repository's store
(`<main checkout>/.claude-tasks/`) **and** the home store, so a mixed list tells them apart by id
prefix; a write goes to this repository's store, or to the home store outside any repository.
**From a linked worktree the store is still the main checkout's**: the record lands and is
committed there, on the main checkout's branch, touching nothing else of it — it is not missing
from your worktree (the write prints one stderr line saying where it went), so never copy,
cherry-pick or re-file it. A write is refused unless the main
checkout is on the repository's default branch (the error says what it found); the fix is to
switch it back, or to file in the home store with `--home` — never to switch it yourself when
a peer may be working there.
`--home` pins the home store, `--store PATH` pins an existing store anywhere. A project *name*
resolves to nothing — name the path.

## File one

```
claude-tasks add --title "<title>" --body-file <path> \
    [--state open|ready|parked] [--field K=V ...] [--legacy-id <old-id> ...] \
    [--home | --store <path>]
```

It prints the new id and commits the record. The body comes from `--body-file <path>`, or from
stdin with `--body-file -`; it is required, and those are its only two spellings. The filename is
the title's slug — a title with no word in it, or one whose slug another record already has, is
refused rather than filed under a guessed name.
`--state` defaults to `open`; filing straight into `ready` or `parked` means supplying that
state's fields in the same call, or the write is refused:

```
claude-tasks add --title "Split the parser" --body-file - --state ready \
    --field decided_by=owner --field reason="it blocks the rewrite" <<'EOF'
Why this is worth doing.
EOF
```

A migration carries a record's old id with `--legacy-id <old-id>`, once per id: `add` writes them
as the record's `legacy_ids`, the only time that field is written, and `show <old-id>` finds the
record from then on. An old id another record of the store already answers to, as its id or a
legacy id, is refused.

## Change one

```
claude-tasks set <ID> [<ID> ...] field=value [field=value ...] [--note "<text>"]
```

State changes are ordinary fields, given together with what the new state requires:

```
claude-tasks set CT-0042 state=closed outcome=built reason="landed in 714f302"
claude-tasks set CT-0042 state=parked trigger="when the cutover lands"
claude-tasks set CT-0042 state=open --note "failed: the chosen route needs a rewrite first"
```

`--note` appends a dated line to the record's `## History` (creating the section when the body
has none) — what the fields cannot say, such as why it came back. `FIELD=null` removes a field;
`FIELD+=a,b` appends to a list field. A `set` commits its own record; given several ids
(`set CT-0042 CT-0043 state=closed outcome=built reason="..."`) it applies the same changes to each,
validates all before writing any, and makes **one** commit — one bad id writes nothing. Close a
batch this way, not with one `set` per record.

**Never invent a `reason`.** Record what he actually said. A blank prompts a question later; an
invention is read by every future run as a fact he stated.

## Look one up

```
claude-tasks show <ID>
```

Prints the record, body included. It takes an id, or an id the record used to carry (its
`legacy_ids`), and says on stderr which record holds an old one now. `show <ID> --json` prints one
JSON object instead: the record as `list --json` dumps it, plus its `body` text.

## Survey many

```
claude-tasks list --json
```

One JSON object per line: that record's frontmatter verbatim plus `path`, `store_prefix` and
`store`. There are no filter flags and no envelope — select with `jq` or `duckdb`:

```
claude-tasks list --home --json | jq -r 'select(.state=="ready") | "\(.id) \(.title)"'
```

`${CLAUDE_SKILL_DIR}/docs/json-contract.md` is what consumers may rely on. Plain `list` without
`--json` is for a human reading the terminal and is not a contract.

## Validate

```
claude-tasks check
```

Every rule a scan can catch: duplicate ids, slugs and legacy ids, an id that does not match its
store, a state missing what it requires, a record in the wrong directory for its state, stray
files. Run it after a migration or a bulk edit. It prints `ok` when clean.

## See the whole board

```
claude-tasks board [--out PATH] [--no-open]
```

Renders every record of the board's stores into one offline HTML page — columns by state, search,
store/tag/size filters, a click-through detail view — and opens it. Always pass `--no-open` here
and hand him the printed path; only he opens the page. The board's stores are the home store plus
whatever `~/.config/claude-tasks/config.toml`'s `[board]` table lists — set up once, not through
this CLI.

## What you must never do

- **Never hand-write a record file**, and **never edit a record's frontmatter** with `Write` or
  `Edit` — where itemctl's `PreToolUse` guard (`itemctl hook pre-tool-use`) is wired, it refuses
  both. Editing a record's **body** directly is fine
  and always allowed.
- **Never hand-edit a store's `store.toml`**: it is the record schema, and `claude-tasks` writes
  it when it creates a store. Every write commits itself; there is no way to leave a record
  uncommitted, because an uncommitted record is state the next compaction loses.
