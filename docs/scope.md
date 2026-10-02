# What belongs in claude-tasks

The canonical statement of scope. Other tools and skills **cite this file** rather than restating
it, so there is one place to change when the boundary moves.

## Kinds of record, one writer each

A store holds these kinds of record, declared together in its `store.toml` (`schema.store_toml`);
the friction kind only in a build that ships `friction_kind.py` (the public build does not):

| kind | where in the store | written by | read by |
|---|---|---|---|
| `idea` | the store root | `claude-tasks add` / `set` | a person choosing what to do next |
| `friction` | `friction/` | `claude-ctl friction` | the friction curation pass |
| `handover` | `handover/<slug>/item.md`, the session's files beside it in `files/` | `claude-tasks handover` | a session launcher, `/pause` pickup |

claude-tasks owns the *shape* of every kind — each kind's block and the store-wide rules every
writer and `check` runs (`friction_kind.entry_rule` among them, where shipped). Each kind's *flows* belong to its
writer. `add`, `list`, `show` and `set` are the idea commands and touch ideas only: `set` or `show`
on a friction or handover id is refused, naming the command that owns it.

## An Idea

An **Idea** is anything worth doing that someone chose to defer: scientific and engineering work,
code, frameworks, tooling, and machine or Claude-Code setup.

A handover is not an Idea, though both live in one store. An Idea is a deferred *decision*, read
by a person choosing what to do next; a handover is a *session* artefact — cwd, branch, WIP commit,
who holds it (an itemctl lease) — read by a session launcher, and dead within hours.

## The boundary against FRICTION is idea versus defect, not subject matter

Something that *went wrong* with how the work runs is friction: `claude-ctl friction` files it in
`~/.claude/FRICTION/`, or — for a defect in one project's own code — in that project's store.

Something that would *improve* how the work runs is an Idea, whatever it is about, because it needs
what FRICTION has none of: ranked routes, an effort size, a park with a trigger, and a close.

A tooling idea is not friction because it is about tooling, and a physics defect is not an Idea
because it is about physics. The question is only ever *went wrong* versus *would improve*.

## Where an idea came from: the `source` field

`source` answers "what produced this idea", with the most specific external anchor there is. One of:

- `url:<URL>` — something outside: a page, a repo, a paper.
- `file:<path>` — a document of his: a braindump, a report, a log.
- `session:<name>` — another session's work. Use this when the filing session is recording
  *someone else's* observation, and name that session.
- `self` — no external artefact: it arose in this session's own work, whether the session noticed it
  or the owner said it in conversation. `self` is the honest answer whenever the other three would be a
  guess.

The test between the last two is who had the thought, not who typed the command: a session filing
its own observation writes `self`, a session filing a peer's writes `session:<name>`.

## Which store

Two roots, and the root answers "which project":

- **A project's ideas** live in a store inside that project's repo, at
  `<main checkout>/.claude-tasks/` — from a linked worktree too, so the records are the project's
  rather than a branch's and outlive the worktree; each write is its own commit of only the
  store's paths, on the branch the main checkout has checked out — which must be the
  repository's default branch (`origin/HEAD`'s target, else local `main`/`master`), or the
  write is refused. A write from a linked worktree prints one stderr line saying it went to the
  main checkout's store, with the commit and branch.
- **The home store** `~/claude-tasks` holds only what is tied to no single project: cross-cutting
  work, the machine, Claude-Code setup.

**A project whose own CLAUDE.md names a register for a domain keeps that domain's ideas there**,
and its `.claude-tasks/` store holds the rest. QM-Theory is the live case: its physics leads go to
its own register, its engineering ideas to its store. The project's CLAUDE.md is what says so, and
it decides — read it before filing into a project you do not know.

Same records, same guard, same validation, one command set, a per-store id sequence shared by the
store's three kinds. Inside a repo the git toplevel gives the store a write goes to, and `--store
PATH` names one explicitly. Every store is also linked in the machine-wide store index
(`~/.local/state/claude-tasks/stores/<PREFIX>`, `store_index`), which is what a machine-wide read
walks; the index answers "which stores exist" and never picks the store a write goes to.
