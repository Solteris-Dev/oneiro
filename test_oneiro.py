#!/usr/bin/env python3
"""Tests for oneiro. Stdlib unittest; run with `./test_oneiro.py`.

The analysis is deliberately made of pure functions over a normalised Store, so
almost everything here runs without touching a real memory store. The few that
do use a temporary directory.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import oneiro as o


def mem(name, body="", mtime=None, **fm):
    return o.Memory(
        name=name,
        path=Path(f"/nowhere/{name}.md"),
        body=body,
        frontmatter=fm,
        mtime=mtime if mtime is not None else time.time(),
    )


def store(memories, index_names=None, kind="claude-code"):
    return o.Store(
        kind=kind,
        root=Path("/nowhere"),
        memories=memories,
        index_names=index_names or [],
        index_path=Path("/nowhere/MEMORY.md") if index_names is not None else None,
    )


class Frontmatter(unittest.TestCase):
    def test_splits_fields_and_body(self):
        fm, body = o.parse_frontmatter("---\nname: a\ndescription: \"x\"\n---\nbody here")
        self.assertEqual(fm["name"], "a")
        self.assertEqual(fm["description"], "x")
        self.assertEqual(body, "body here")

    def test_absent_frontmatter_is_all_body(self):
        fm, body = o.parse_frontmatter("just text")
        self.assertEqual(fm, {})
        self.assertEqual(body, "just text")

    def test_nested_keys_are_skipped_not_crashed(self):
        fm, _ = o.parse_frontmatter("---\nname: a\nmetadata:\n  type: user\n---\nb")
        self.assertEqual(fm["name"], "a")
        self.assertNotIn("type", fm)


class IndexDrift(unittest.TestCase):
    def test_unindexed_file_is_reported_and_autofixable(self):
        s = store([mem("alpha")], index_names=[])
        (f,) = o.check_index_drift(s)
        self.assertEqual(f.kind, "index-drift")
        self.assertTrue(f.autofix, "adding an index line is mechanical")
        self.assertEqual(f.severity, 0)

    def test_dangling_index_entry_is_never_autofixable(self):
        # The tidy repair is deleting the line, but the file may have been renamed;
        # dropping its only pointer would bury it. Must stay a human decision.
        s = store([], index_names=["ghost"])
        (f,) = o.check_index_drift(s)
        self.assertFalse(f.autofix)

    def test_no_index_means_no_drift_findings(self):
        # Codex-shaped stores have no index to drift from.
        self.assertEqual(o.check_index_drift(store([mem("a")])), [])

    def test_matching_index_and_disk_is_silent(self):
        s = store([mem("alpha")], index_names=["alpha"])
        self.assertEqual(o.check_index_drift(s), [])


class BrokenLinks(unittest.TestCase):
    def test_dangling_wikilink_is_reported(self):
        s = store([mem("a", "see [[nowhere]]")])
        (f,) = o.check_broken_links(s)
        self.assertEqual(f.kind, "broken-link")
        self.assertIn("nowhere", f.detail)

    def test_link_to_existing_memory_is_silent(self):
        s = store([mem("a", "see [[b]]"), mem("b")])
        self.assertEqual(o.check_broken_links(s), [])

    def test_repeated_link_reported_once(self):
        s = store([mem("a", "[[x]] and [[x]] again")])
        self.assertEqual(len(o.check_broken_links(s)), 1)


class Staleness(unittest.TestCase):
    def test_old_memory_is_flagged(self):
        old = time.time() - 100 * 86400
        (f,) = o.check_stale(store([mem("a", mtime=old)]), days=45)
        self.assertEqual(f.kind, "stale")

    def test_recent_memory_is_silent(self):
        self.assertEqual(o.check_stale(store([mem("a")]), days=45), [])

    def test_portable_store_collapses_to_one_export_age(self):
        # Every memory in an export shares the file's mtime, so per-memory
        # staleness would be noise dressed as insight.
        old = time.time() - 100 * 86400
        s = store([mem("a", mtime=old), mem("b", mtime=old)], kind="portable")
        findings = o.check_stale(s, days=45)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "stale-export")

    def test_empty_portable_store_does_not_crash(self):
        self.assertEqual(o.check_stale(store([], kind="portable"), days=45), [])


class Overlap(unittest.TestCase):
    def test_near_identical_memories_are_merge_candidates(self):
        text = "hyprland waybar sticky notes purple config wayland compositor"
        s = store([mem("a", text), mem("b", text)])
        self.assertTrue(o.check_overlap(s, 0.55))

    def test_unrelated_memories_are_silent(self):
        s = store([mem("a", "hyprland waybar compositor purple"),
                   mem("b", "terraria server backups netcode")])
        self.assertEqual(o.check_overlap(s, 0.55), [])

    def test_stopwords_do_not_create_overlap(self):
        s = store([mem("a", "the and or of to in for on is are"),
                   mem("b", "the and or of to in for on is are")])
        self.assertEqual(o.check_overlap(s, 0.55), [])


class Fingerprints(unittest.TestCase):
    def test_stable_across_changing_numbers(self):
        # Staleness wording counts days, which changes nightly. The finding is
        # the same finding, so it must not read as new each run.
        a = o.Finding("stale", "untouched for 45 days — still true?", 2, "x")
        b = o.Finding("stale", "untouched for 46 days — still true?", 2, "x")
        self.assertEqual(o.fingerprint(a), o.fingerprint(b))

    def test_different_findings_differ(self):
        a = o.Finding("stale", "untouched", 2, "x")
        b = o.Finding("stale", "untouched", 2, "y")
        self.assertNotEqual(o.fingerprint(a), o.fingerprint(b))


class Headline(unittest.TestCase):
    def test_unrecalled_memories_outrank_everything(self):
        findings = [
            o.Finding("index-drift", "not indexed", 0, "a", autofix=True),
            o.Finding("broken-link", "dangling", 1, "b"),
        ]
        self.assertIn("invisible to recall", o.headline(findings, set()))

    def test_singular_and_plural_agree(self):
        one = [o.Finding("index-drift", "x", 0, "a", autofix=True)]
        two = one + [o.Finding("index-drift", "x", 0, "b", autofix=True)]
        self.assertIn("memory is", o.headline(one, set()))
        self.assertIn("memories are", o.headline(two, set()))

    def test_housekeeping_only_when_nothing_structural(self):
        findings = [o.Finding("stale", "old", 2, "a")]
        self.assertIn("Nothing urgent", o.headline(findings, set()))


class Render(unittest.TestCase):
    def test_tidy_store_says_so(self):
        self.assertIn("tidy", o.render(store([mem("a")]), [], 0.0))

    def test_new_flag_suppressed_without_a_baseline(self):
        # On a first run everything is new, which would make the flag meaningless.
        findings = [o.Finding("stale", "old", 2, "a")]
        self.assertNotIn("NEW", o.render(store([mem("a")]), findings, 0.0, seen=set()))

    def test_new_flag_appears_against_a_baseline(self):
        old = o.Finding("stale", "old", 2, "a")
        new = o.Finding("broken-link", "dangling", 1, "b")
        out = o.render(store([mem("a")]), [old, new], 0.0, seen={o.fingerprint(old)})
        self.assertIn("NEW", out)
        self.assertEqual(out.count("**NEW**"), 1)

    def test_report_never_claims_to_have_changed_anything(self):
        out = o.render(store([mem("a")]), [o.Finding("stale", "old", 2, "a")], 0.0)
        self.assertIn("nothing was modified", out)


class Adapters(unittest.TestCase):
    def test_claude_code_roundtrip_and_index(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "alpha.md").write_text("---\nname: alpha\n---\nbody")
            (root / "MEMORY.md").write_text("- [Alpha](alpha.md) — hook\n")
            s = o.read_claude_code(root)
            self.assertEqual([m.name for m in s.memories], ["alpha"])
            self.assertEqual(s.index_names, ["alpha"])

    def test_claude_code_excludes_the_index_from_memories(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "MEMORY.md").write_text("- [A](a.md) — x\n")
            self.assertEqual(o.read_claude_code(root).memories, [])

    def test_portable_splits_on_headings(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "export.md"
            path.write_text("## First Thing\ndesc\n\nbody\n\n## second-thing\nmore\n")
            s = o.read_portable(path)
            self.assertEqual([m.name for m in s.memories], ["first-thing", "second-thing"])

    def test_portable_missing_file_is_empty_not_fatal(self):
        self.assertEqual(o.read_portable(Path("/nowhere/nope.md")).memories, [])


class State(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_STATE_HOME"] = d
            s = store([mem("a")])
            f = o.Finding("stale", "old", 2, "a")
            o.save_seen(s, [f])
            self.assertEqual(o.load_seen(s), {o.fingerprint(f)})

    def test_absent_state_is_an_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_STATE_HOME"] = d
            self.assertEqual(o.load_seen(store([mem("a")])), set())

    def test_corrupt_state_does_not_stop_the_pass(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_STATE_HOME"] = d
            s = store([mem("a")])
            p = o.state_path(s)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{not json")
            self.assertEqual(o.load_seen(s), set())


class ReadOnly(unittest.TestCase):
    def test_a_full_pass_does_not_touch_the_store(self):
        """The load-bearing guarantee: analysing must never modify memory."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "alpha.md").write_text("---\nname: alpha\n---\nlinks [[nowhere]]")
            (root / "MEMORY.md").write_text("- [Ghost](ghost.md) — missing\n")
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in sorted(root.glob("*.md"))}

            s = o.read_claude_code(root)
            findings = o.analyse(s, stale_days=45, overlap=0.55)
            o.render(s, findings, 0.0)

            after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                     for p in sorted(root.glob("*.md"))}
            self.assertEqual(before, after)
            self.assertTrue(findings, "fixture should produce findings")


if __name__ == "__main__":
    unittest.main(verbosity=2)
