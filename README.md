# oneiro

A nightly consolidation pass over an agent's memory store.

Agents accumulate memory during the day and never tidy it. Indexes drift from
the files they index, links rot, facts go stale, and two memories quietly say
different things. None of it surfaces during a session, because nothing ever
looks at the store as a whole.

`oneiro` looks, once a night, and writes up what it found.

**It never modifies the memory store.** A dream proposes; waking commits. Every
finding is addressed to you, and applying one is your job.

## Run it

```sh
./oneiro.py claude:-home-solteris          # a Claude Code project's memory
./oneiro.py codex                          # ~/.codex/memories
./oneiro.py claude-code:/path/to/memory    # any directory
./oneiro.py portable:export.md             # an agent that isn't on this disk
./oneiro.py --report stdout                # don't write anything at all
```

By default the report is written to `<store>/dream-reports/YYYY-MM-DD.md` and
also dropped on the desktop as a [waynote](https://github.com/mryll/waynote)
sticky, so it's waiting when you wake up. The sticky is best-effort: if waynote
isn't installed it's skipped silently, because the canonical file is already
written and there's nothing to lose.

## What it looks for

| finding | severity | why it matters |
|---|---|---|
| `index-drift` | wrong | a memory missing from the index is **never recalled**, and nothing anywhere reports it |
| `broken-link` | rotting | `[[wikilinks]]` pointing at memories that don't exist |
| `stale` | consider | untouched for N days — still true? |
| `overlap` | consider | two memories with high vocabulary overlap; merge candidates |

Index drift is the one worth building the tool for. Every other failure has some
ambient signal — you notice vague answers, you can see an old date in `ls`. A
missing index line has none. The memory simply never fires, and you never learn
it exists.

## Agents

A store is read through a small adapter, so the analysis is the same everywhere.
Adding an agent means adding a reader, not touching the analysis.

- **`claude-code`** — one `.md` per fact plus a hand-maintained `MEMORY.md` index
- **`codex`** — `raw_memories.md` → `memory_summary.md` → `MEMORY.md` handbook
- **`portable`** — the interchange format, for agents whose memory isn't on this
  disk

Adapters read explicit paths and never glob a vendor directory: `~/.gemini/`
keeps `oauth_creds.json` next to its knowledge dirs, and a tool that wandered
would put credentials in a report.

### Agents that aren't on your disk

Gemini's account history, browser Claude, GPT. Rather than scraping or wrestling
with an export API, ask the agent to emit its own memory:

```sh
./oneiro.py --export-prompt        # paste this into the agent
# save its reply, then:
./oneiro.py portable:gemini-export.md
```

The agent does the reading; oneiro does the auditing.

## Applying findings

Nothing is applied. Findings marked _(safe to apply)_ are mechanical, additive
and trivially reversible — adding an index line whose text comes from the file's
own `description:`. Anything requiring a judgement about what a memory *means*
is never marked safe, however obvious it looks: an index line pointing at a
missing file could be a rename rather than a deletion, and quietly dropping it
would bury the memory for good.

The classification exists so that when applying is switched on, the scary set is
already carved out rather than decided in the moment.

## Not implemented yet

**Mining raw transcripts.** Curated memory and raw conversation history are two
different jobs — you can't check index drift on a chat log. Cursor keeps 68
chats in SQLite; Gemini keeps history server-side. Consolidating that raw
experience into durable memory is the larger half of the idea, and it needs a
model rather than a regex. Codex's `raw_memories.md → memory_summary.md`
pipeline is exactly this shape, waiting for something to drive it.

**Contradiction detection.** Requires reading for meaning. Plugs in alongside the
deterministic checks: same `Store` in, same `Finding` list out.

A rule that holds when it lands: **semantics may propose; only deterministic
rules may classify as safe.** A model can say "these two memories conflict"; it
must never say "so I'll fix it." Otherwise the apply-gate belongs to the
component most capable of being confidently wrong, firing at 4am unattended.
`autofix` stays keyed on finding *kind*, forever.

Semantic findings should also be tuned for precision over recall and quote both
memories verbatim. Thirty maybes a night and you stop reading the report — and a
report you don't read is worse than none, because you'll believe your memory is
audited.

## Reading the report

The top line states a **consequence**, not a count — "2 memories are invisible to
recall" rather than "2 index-drift". A long report gets skimmed, so the line that
survives skimming has to be the one that matters. It escalates: structural
problems outrank rot, rot outranks housekeeping.

Findings seen on a previous run are remembered, so anything new is marked
**NEW**. That's the best triage signal there is — on a busy store, "3 new since
last night" beats any severity ranking. State lives in
`$XDG_STATE_HOME/oneiro/`; `--no-state` disables it and leaves no trace.

## Schedule

See `packaging/` for a systemd user timer. Stdlib only, no dependencies.
