# claude-tasks JSON contract

`claude-tasks list --json` and `claude-tasks show ID --json` are the machine-readable read surfaces.
This file is what consumers rely on; nothing else in this repo describes the format.

## The format: newline-delimited JSON, one object per idea

No envelope, no wrapping array. Each line is a complete JSON object: **that record's frontmatter
verbatim** — except `created`, see below — plus `path`, `store_prefix` and `store` added by the
CLI. Key order is not part of the contract.

```
{"id":"CT-0240","state":"open","created":"2026-09-17","title":"Retire the ideas ledger","size":"S","tags":["tooling"],"store_prefix":"CT","store":"~/claude-tasks","path":"~/claude-tasks/retire-the-ideas-ledger.md"}
```

NDJSON because the consumers are `duckdb` and `jq`:

```sh
claude-tasks list --json | duckdb -c "
  select state, count(*) from read_json('/dev/stdin', union_by_name=true) group by 1"
```

`union_by_name=true` is what handles records carrying different key sets — a `closed` record has
`outcome` and `reason`, an `open` one has neither, and the union is wider than any single record.

## `show --json`: one record, with its body

`claude-tasks show ID --json` prints one JSON object on one line: exactly the object `list --json`
carries for that record, plus `body`, the record's markdown body as a string. Nothing else differs.
An unknown id fails as plain `show` does: exit 1, the message on stderr, nothing on stdout.

## There is no version field

There is no CLI-invented shape left to version: the keys are the record's own, and `claude-tasks
check` is what enforces them. A failing `check` is a louder signal than a number a consumer might
forget to compare.

A consumer that meets a `{"schema_version": …, "tasks": […]}` object on stdin is talking to an
installed `claude-tasks` older than this contract, and should say so by name rather than fail later
on a missing key.

## Keys every record carries

| key | meaning |
|---|---|
| `id` | `<PREFIX>-<zero-padded number>`, unique within its store |
| `title` | one line, the record's name |
| `state` | `open` · `ready` · `parked` · `closed` |
| `created` | `YYYY-MM-DD`: the local date of the record's `created` timestamp |
| `path` | absolute path to the record file; the only way to reach its body |
| `store_prefix` | the id prefix of the store this record came from |
| `store` | absolute path to that store's root |

`path`, `store_prefix` and `store` are added by the CLI and are not in the file's frontmatter. The
other four are validated on every write. In the file, `created` is a timezone-aware timestamp
(`2026-09-17T00:00:00+02:00`; records filed before the itemctl migration carry midnight of their
filing date); the dump carries its date only, so the key keeps the one shape consumers parse.

## Keys a record carries because of its state

| state | also required |
|---|---|
| `open` | — |
| `ready` | `decided_by`, `reason` |
| `parked` | `trigger` or `blocked_by` |
| `closed` | `outcome` (`built` · `dropped` · `superseded`), `reason` |

## Everything else is pass-through

Any other key in a record's frontmatter — `size`, `tags`, `touches`, `source`, `legacy_id`,
`autopilot`, whatever a writer put there — appears in the dump unchanged and unvalidated. A consumer
reads it with `.get()` semantics: present on some records, absent on others.

## The placeholder reason

`closed` and `ready` both require a `reason`, and the 2026-09-17 migration from the pre-lean stores
could not know why a record was closed months ago. It wrote one documented string, `schema.PLACEHOLDER_REASON`,
which is **the** value meaning "no reason was recorded".

To find records still awaiting a real reason:

```sql
select id, title, state from read_json('/dev/stdin', union_by_name=true)
where reason = (the value of schema.PLACEHOLDER_REASON) or autopilot
```

The string lives in `src/claude_tasks/schema.py` and nowhere else, so a writer that changes it
changes this query at the same time.

## Which records are in the stream

Every idea in the stores being read, in every state. `list` applies no filter of any kind —
selection is the consumer's, with the query language of its choice. A store also holds friction
entries and handovers; they are never in this stream, and their own commands (`claude-ctl
friction`, `claude-tasks handover`) are how they are read.

Both roots by default: run inside a repo that has a `.claude-tasks/` store and the stream holds that
store's records **and** the home store's, in one call, `store_prefix` telling them apart. `--store
PATH` or `--home` pins one root. Outside a repo, or inside one with no store, the home store alone.

## Plain `list`

Without `--json`, `list` prints one line per record for a human at a terminal. That rendering is not
a contract and may change; anything parsing it should use `--json`.

## `handover list --json`

The handover kind's read surface, which claude-ctl's continue-all reads. Unlike `list`, it is **one
JSON document**, `{"handovers": [...], "notes": [...]}`:

- `handovers` — each open handover (every one with `--done`), oldest `created` first: its
  frontmatter verbatim, timestamps as full ISO text (`created`, `block_resets_at`, `claimed_at`,
  `lease_until`, `closed_at`), plus `path` (its `item.md`), `files_dir` (the gitignored `files/`
  beside it, holding `agents/` and `wip.patch`), `store` and `store_prefix`. Always present: `id`,
  `title`, `created`, `state` (`open` · `done`), `model`, `size`, `cwd`; `closed_by` and `closed_at`
  exactly in `done`. A claim is the lease `claimed_by`/`claimed_at`/`lease_until`, not a state.
- `notes` — with `--all`, one line per index link that leads to no store and per indexed store that
  could not be read; those stores' handovers are missing from `handovers`. Empty without `--all`.

Without `--all` it reads the store a write from the cwd goes to; with it, every store the index
links to.
