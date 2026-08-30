#!/usr/bin/env python3
"""oneiro — a nightly consolidation pass over an agent's memory store.

Agents accumulate memory during the day and never tidy it. Indexes drift from
the files they index, links rot, facts go stale, and two memories quietly say
different things. None of that surfaces during a session, because nothing ever
looks at the store as a whole.

oneiro looks, once a night, and writes up what it found.

It **never modifies the memory store.** A dream proposes; waking commits. Every
finding is a suggestion addressed to you, and applying one is your job.

Agent-agnostic by design: a store is read through a small adapter, so the same
analysis runs over Claude Code's one-file-per-fact layout and Codex's
raw/summary/handbook pipeline alike. Adding an agent means adding a reader, not
touching the analysis.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- model


@dataclass
class Memory:
    """One addressable unit of memory, however the agent happens to store it."""

    name: str
    path: Path
    body: str
    frontmatter: dict[str, str] = field(default_factory=dict)
    mtime: float = 0.0

    @property
    def age_days(self) -> float:
        return (time.time() - self.mtime) / 86400 if self.mtime else 0.0


@dataclass
class Store:
    """A memory store, normalised across agents."""

    kind: str
    root: Path
    memories: list[Memory]
    # Names the store's own index claims exist. Empty when the format has no index.
    index_names: list[str] = field(default_factory=list)
    index_path: Path | None = None


@dataclass
class Finding:
    kind: str
    detail: str
    # Lower sorts first: 0 is "this is silently wrong", 2 is "you might consider".
    severity: int = 1
    where: str = ""
    # Could a machine apply this without judgement? Only mechanical, additive,
    # trivially reversible repairs qualify — adding an index line whose text comes
    # from the file's own frontmatter, say. Anything requiring a decision about
    # what a memory *means* (is it stale? should these merge? was that link a typo
    # or a missing file?) is never autofixable, however obvious it looks.
    #
    # Nothing applies these yet: v1 only reports which ones it *could*. The
    # classification exists so that when applying is switched on, the scary set is
    # already carved out rather than being decided in the moment.
    autofix: bool = False


# ---------------------------------------------------------------- adapters

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
# `- [Title](file.md) — hook`
INDEX_LINK_RE = re.compile(r"^\s*[-*]\s*\[[^\]]*\]\(([^)]+\.md)\)", re.M)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split leading YAML-ish frontmatter. Deliberately shallow: we only need
    top-level scalars, and a real YAML dependency would buy nothing here."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if line[:1] in (" ", "\t", "#") or ":" not in line:
            continue  # nested key or comment; not needed
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("\"'")
    return fields, text[m.end() :]


def read_claude_code(root: Path) -> Store:
    """Claude Code: one .md per fact, plus a hand-maintained MEMORY.md index."""
    memories, index_names = [], []
    index_path = root / "MEMORY.md"
    for path in sorted(root.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        text = path.read_text(errors="replace")
        fm, body = parse_frontmatter(text)
        memories.append(
            Memory(
                name=fm.get("name") or path.stem,
                path=path,
                body=body,
                frontmatter=fm,
                mtime=path.stat().st_mtime,
            )
        )
    if index_path.exists():
        index_names = [
            Path(t).stem for t in INDEX_LINK_RE.findall(index_path.read_text(errors="replace"))
        ]
    return Store("claude-code", root, memories, index_names, index_path if index_path.exists() else None)


def read_codex(root: Path) -> Store:
    """Codex: raw_memories.md -> memory_summary.md -> MEMORY.md handbook.

    The pipeline is already the right shape; treat each document as one memory so
    staleness and contradiction checks still apply.
    """
    memories = []
    for name in ("raw_memories.md", "memory_summary.md", "MEMORY.md"):
        path = root / name
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        fm, body = parse_frontmatter(text)
        memories.append(
            Memory(name=path.stem, path=path, body=body, frontmatter=fm, mtime=path.stat().st_mtime)
        )
    return Store("codex", root, memories)


def read_portable(path: Path) -> Store:
    """The interchange format: one markdown file, `## name` per memory.

    For agents whose memory isn't on this disk — Gemini's account history,
    browser Claude, GPT. Rather than scraping or exporting, ask the agent itself
    to emit its memory in this shape (`oneiro --export-prompt`), paste the reply
    into a file, and analyse it like any other store. The agent does the reading;
    oneiro does the auditing.
    """
    text = path.read_text(errors="replace") if path.is_file() else ""
    memories, mtime = [], path.stat().st_mtime if path.is_file() else 0.0
    # Split on `## heading`, keeping the heading with its body.
    chunks = re.split(r"^##\s+(.+?)\s*$", text, flags=re.M)[1:]
    for name, body in zip(chunks[0::2], chunks[1::2]):
        slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
        memories.append(Memory(name=slug or name.strip(), path=path, body=body.strip(), mtime=mtime))
    return Store("portable", path.parent, memories)


ADAPTERS = {"claude-code": read_claude_code, "codex": read_codex, "portable": read_portable}

EXPORT_PROMPT = """\
Please export everything you durably remember about me into the format below.
This is going into a tool that audits memory across several assistants, so
fidelity matters more than polish.

Rules:
- One `##` section per distinct fact, preference, or piece of project context.
- The heading is a short kebab-case name (e.g. `## gpu-vram-headroom`).
- First line under it: a one-sentence description of what the memory is.
- Then the memory itself, in as much detail as you actually hold. Include when
  you learned it if you know.
- Use [[other-memory-name]] to reference another section.
- Do not invent, merge, or tidy. If you are unsure whether something is durable
  memory or just this conversation, include it and say which.
- If you hold nothing durable, say exactly that rather than reconstructing.

Format:

## short-kebab-name
One-sentence description.

The memory, in full.

## another-name
One-sentence description.

The memory, in full.
"""


def project_key(path: Path) -> str:
    """Claude Code's project key for a directory: the path with separators and
    dots flattened to hyphens (`/home/u/.claude` -> `-home-u--claude`)."""
    return re.sub(r"[/.]", "-", str(path))


def default_store() -> str:
    """Audit the memory belonging to the directory you're standing in.

    Claude Code keys memory by working directory, so this is the store that a
    session started here would actually load — which makes the no-argument case
    mean something on any machine, rather than on one developer's.
    """
    return f"claude:{project_key(Path.cwd())}"


def resolve_store(spec: str) -> tuple[str, Path]:
    """`claude:<project-key>`, `codex`, or `<adapter>:<path>`."""
    home = Path.home()
    if spec == "codex":
        return "codex", home / ".codex/memories"
    if spec.startswith("claude:"):
        return "claude-code", home / ".claude/projects" / spec.split(":", 1)[1] / "memory"
    if ":" in spec:
        kind, _, path = spec.partition(":")
        if kind in ADAPTERS:
            return kind, Path(path).expanduser()
    raise SystemExit(f"oneiro: don't know how to read store {spec!r}")


def store_target_exists(kind: str, root: Path) -> bool:
    """`portable` addresses a file; the rest address a directory."""
    return root.is_file() if kind == "portable" else root.is_dir()


# ---------------------------------------------------------------- analysis
# Everything below is deterministic and free. An LLM pass (contradictions,
# merge wording) plugs in alongside these, taking the same Store and returning
# the same Findings.

STOPWORDS = set(
    "the a an and or of to in for on is are was were be been it its this that with "
    "as at by from not no do does did so if then than when what which who whom".split()
)


def significant_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9_-]{3,}", text.lower()) if w not in STOPWORDS}


def check_index_drift(store: Store) -> list[Finding]:
    """The index and the directory disagreeing is the one failure with no
    ambient signal: a memory missing from the index is never recalled, and
    nothing anywhere reports it."""
    if store.index_path is None:
        return []
    on_disk = {m.path.stem for m in store.memories}
    indexed = set(store.index_names)
    out = []
    for name in sorted(on_disk - indexed):
        # Additive and mechanical: the index line is derivable from the file's own
        # `description:`. Safe to apply automatically once applying exists.
        out.append(Finding("index-drift", f"{name}.md exists but is not in the index — it will never be recalled", 0, name, autofix=True))
    for name in sorted(indexed - on_disk):
        # Deliberately NOT autofixable. The tidy repair is deleting the index line,
        # but the file may have been renamed or moved rather than dropped, and
        # silently removing the only pointer to it would bury it for good.
        out.append(Finding("index-drift", f"the index points at {name}.md, which does not exist", 0, name))
    return out


def check_broken_links(store: Store) -> list[Finding]:
    known = {m.path.stem for m in store.memories} | {m.name for m in store.memories}
    out = []
    for mem in store.memories:
        for target in sorted(set(WIKILINK_RE.findall(mem.body))):
            if target not in known:
                out.append(Finding("broken-link", f"links to [[{target}]], which does not exist", 1, mem.name))
    return out


def check_stale(store: Store, days: int) -> list[Finding]:
    # A portable export is one file, so every memory in it shares one mtime — the
    # export date, not when anything was learned. Reporting that per-memory would
    # be noise dressed up as insight, so say the one true thing instead.
    if store.kind == "portable":
        age = store.memories[0].age_days if store.memories else 0.0
        if age >= days:
            return [Finding("stale-export", f"this export is {age:.0f} days old — re-export to refresh", 2)]
        return []
    out = []
    for mem in store.memories:
        if mem.age_days >= days:
            out.append(
                Finding("stale", f"untouched for {mem.age_days:.0f} days — still true?", 2, mem.name)
            )
    return out


def check_overlap(store: Store, threshold: float) -> list[Finding]:
    """Merge candidates by vocabulary overlap. A blunt heuristic on purpose: it
    flags pairs worth a human glance, it does not decide anything."""
    out = []
    vocab = [(m, significant_words(m.body)) for m in store.memories]
    for i, (a, wa) in enumerate(vocab):
        for b, wb in vocab[i + 1 :]:
            if not wa or not wb:
                continue
            overlap = len(wa & wb) / min(len(wa), len(wb))
            if overlap >= threshold:
                out.append(
                    Finding(
                        "overlap",
                        f"{overlap:.0%} vocabulary overlap with {b.name} — possible merge",
                        2,
                        a.name,
                    )
                )
    return out


def fingerprint(f: Finding) -> str:
    """Identity of a finding across runs, so last night's report can be diffed
    against tonight's. Deliberately excludes wording that drifts with time (a
    staleness detail counts days), keeping the identity stable while the text
    changes."""
    import hashlib

    detail = re.sub(r"\d+", "#", f.detail)
    return hashlib.sha256(f"{f.kind}\0{f.where}\0{detail}".encode()).hexdigest()[:16]


def state_path(store: Store) -> Path:
    import hashlib

    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state")
    key = hashlib.sha256(str(store.root).encode()).hexdigest()[:12]
    return Path(base) / "oneiro" / f"{store.kind}-{key}.json"


def load_seen(store: Store) -> set[str]:
    """Fingerprints reported on a previous night."""
    path = state_path(store)
    if not path.is_file():
        return set()
    try:
        return set(json.loads(path.read_text()).get("seen", []))
    except (json.JSONDecodeError, OSError):
        return set()  # a corrupt state file must not stop the pass


def save_seen(store: Store, findings: list[Finding]) -> None:
    path = state_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "store": str(store.root),
        "kind": store.kind,
        "last_run": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "seen": sorted(fingerprint(f) for f in findings),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def analyse(store: Store, stale_days: int, overlap: float) -> list[Finding]:
    findings = (
        check_index_drift(store)
        + check_broken_links(store)
        + check_stale(store, stale_days)
        + check_overlap(store, overlap)
    )
    return sorted(findings, key=lambda f: (f.severity, f.kind, f.where))


# ---------------------------------------------------------------- reporting

SEVERITY_LABEL = {0: "wrong", 1: "rotting", 2: "consider"}


def headline(findings: list[Finding], new: set[str]) -> str:
    """One line stating consequence, not counts.

    A long report gets skimmed, so the top line has to survive skimming. Say what
    happens if it is ignored — the way a permission prompt says "this deletes
    your disk" rather than printing the command and hoping.
    """
    unrecalled = [f for f in findings if f.kind == "index-drift" and f.autofix]
    dangling = [f for f in findings if f.kind == "index-drift" and not f.autofix]
    if unrecalled:
        n = len(unrecalled)
        return (f"**{n} {'memory is' if n == 1 else 'memories are'} invisible to recall** — "
                f"on disk but missing from the index, so {'it' if n == 1 else 'they'} will never load.")
    if dangling:
        n = len(dangling)
        return f"**{n} index {'entry points' if n == 1 else 'entries point'} at {'a file' if n == 1 else 'files'} that no longer exist.**"
    broken = [f for f in findings if f.kind == "broken-link"]
    if broken:
        return f"**{len(broken)} broken {'link' if len(broken) == 1 else 'links'}** between memories."
    if new:
        return f"**{len(new)} new since the last pass**, nothing structurally wrong."
    return "**Nothing urgent** — housekeeping only."


def render(store: Store, findings: list[Finding], took: float,
           seen: set[str] | None = None) -> str:
    today = time.strftime("%Y-%m-%d")
    seen = seen or set()
    new = {fingerprint(f) for f in findings} - seen
    lines = [f"# dream {today}", ""]
    if not findings:
        n = len(store.memories)
        lines += [f"{n} {'memory' if n == 1 else 'memories'} in `{store.kind}`. "
                  "Nothing to report — the store is tidy.", ""]
        return "\n".join(lines)

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    summary = " · ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    count = len(store.memories)
    lines += [
        headline(findings, new),
        "",
        f"{count} {'memory' if count == 1 else 'memories'} in `{store.kind}` · {summary}"
        + (f" · {len(new)} new since last pass" if new and seen else ""),
        "",
    ]

    for sev in (0, 1, 2):
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        lines.append(f"## {SEVERITY_LABEL[sev]}")
        lines.append("")
        for f in group:
            where = f"`{f.where}` — " if f.where else ""
            mark = " _(safe to apply)_" if f.autofix else ""
            # Only mark novelty once there's a baseline; on a first run everything
            # is technically new, which would make the flag meaningless.
            flag = " **NEW**" if seen and fingerprint(f) in new else ""
            lines.append(f"- {where}{f.detail}{mark}{flag}")
        lines.append("")

    safe = sum(1 for f in findings if f.autofix)
    if safe:
        verb = "is" if safe == 1 else "are"
        lines.append(
            f"{safe} of these {verb} mechanical and could be applied without judgement. "
            "oneiro does not apply anything."
        )
        lines.append("")
    lines.append(f"_read-only pass over {store.root}, {took*1000:.0f}ms — nothing was modified_")
    lines.append("")
    return "\n".join(lines)


def new_ulid() -> str:
    import secrets

    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    ts, rnd, out, tail = int(time.time() * 1000), secrets.randbits(80), "", ""
    for _ in range(10):
        ts, r = divmod(ts, 32)
        out = alphabet[r] + out
    for _ in range(16):
        rnd, r = divmod(rnd, 32)
        tail = alphabet[r] + tail
    return out + tail


def write_sticky(report: str, notes_dir: Path) -> Path | None:
    """Best-effort: drop the report on the desktop as a waynote sticky.

    Silently does nothing when waynote isn't installed — the canonical file has
    already been written, so there is nothing to lose by skipping.
    """
    if not notes_dir.is_dir():
        return None
    note_id = new_ulid()
    path = notes_dir / f"{note_id}-dream-{time.strftime('%Y-%m-%d')}.md"
    frontmatter = (
        "---\n"
        f"id: {note_id}\n"
        "color: purple\n"
        "pinned: false\n"
        "locked: false\n"
        "layer: front\n"
        "tags: [dream]\n"
        "---\n"
    )
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(frontmatter + report)
    os.replace(tmp, path)
    return path


def write_file(report: str, store: Store) -> Path:
    out_dir = store.root / "dream-reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{time.strftime('%Y-%m-%d')}.md"
    path.write_text(report)
    return path


# ---------------------------------------------------------------- cli


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("store", nargs="?", default=None,
                    help="claude:<project-key>, codex, or <adapter>:<path> "
                         "(default: the Claude Code memory for the current directory)")
    ap.add_argument("--report", choices=["sticky", "file", "both", "stdout"], default="sticky",
                    help="where the report goes. 'sticky' also writes the canonical file, "
                         "and skips the sticky silently when waynote is absent (default: sticky)")
    ap.add_argument("--notes-dir", type=Path,
                    default=Path.home() / ".local/share/waynote/notes",
                    help="waynote notes directory, for --report sticky/both")
    ap.add_argument("--stale-days", type=int, default=45,
                    help="flag memories untouched for this long (default: 45)")
    ap.add_argument("--overlap", type=float, default=0.55,
                    help="vocabulary overlap that marks a merge candidate, 0-1 (default: 0.55)")
    ap.add_argument("--no-state", action="store_true",
                    help="don't read or write the seen-findings state, so nothing "
                         "is marked NEW and the run leaves no trace")
    ap.add_argument("--export-prompt", action="store_true",
                    help="print a prompt to hand to an agent whose memory isn't on "
                         "this disk (Gemini, browser Claude, GPT); save its reply and "
                         "read it back with `portable:<file>`")
    args = ap.parse_args()

    if args.export_prompt:
        print(EXPORT_PROMPT, end="")
        return 0

    kind, root = resolve_store(args.store or default_store())
    if not store_target_exists(kind, root):
        print(f"oneiro: no store at {root}", file=sys.stderr)
        return 1

    started = time.time()
    store = ADAPTERS[kind](root)
    findings = analyse(store, args.stale_days, args.overlap)
    seen = set() if args.no_state else load_seen(store)
    report = render(store, findings, time.time() - started, seen)

    if args.report == "stdout":
        print(report, end="")
        return 0

    if not args.no_state:
        save_seen(store, findings)

    written = [str(write_file(report, store))]
    if args.report in ("sticky", "both"):
        sticky = write_sticky(report, args.notes_dir)
        if sticky:
            written.append(str(sticky))
    print(f"oneiro: {len(findings)} finding(s); wrote {', '.join(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
