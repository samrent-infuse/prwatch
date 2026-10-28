import os

# Before importing decide (through review): identity and policy are read at import time.
os.environ.update(PRWATCH_ME="me,me-alt", PRWATCH_REVIEWER="reviewer-bot",
                  PRWATCH_REQUIRED_CHECK="scan-verdict", PRWATCH_REPO="example-org/example-repo",
                  PRWATCH_SETTLE_MIN="30", PRWATCH_SKIP_TITLE="do not review",
                  PRWATCH_MAX_CHANGE_ROUNDS="2")

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import brain

LEAVES = {
    "gotchas/gotcha-a.md": ("gotcha", "2026-09-01", "current", "Boot DDL in `inkhub_platform/api/main.py` locks."),
    "reference/ref-b.md": ("reference", "2026-04-20", "current",
                           "Routes live in inkhub_platform/api/routes.py; the db listens on port 5432."),
    "decisions/decision-c.md": ("decision", "2026-08-01", "current", "Workers: inkhub_platform/worker/x.py."),
    "investigations/inv-d.md": ("investigation", "2026-10-01", "current", "Traced inkhub_platform/api/main.py."),
    "reference/old-ref.md": ("reference", "2026-01-01", "superseded", "inkhub_platform/api/main.py once."),
}


def leaf_text(type_, as_of, status, body, title="T"):
    return (f"---\ntype: {type_}\ntitle: \"{title}\"\nstatus: {status}\nas_of: {as_of}\n"
            f"source: \"built from source\"\ntags: [platform, api]\nlinks: []\n---\n\n# {title}\n\n{body}\n"
            + "More context that keeps the leaf a realistic size. " * 5 + "\n")


class VaultCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name) / "vault"
        self.inputs = Path(tmp.name) / "inputs"
        self.backups = Path(tmp.name) / "backups"
        self.inputs.mkdir()
        for rel, (t, a, s, body) in LEAVES.items():
            p = self.vault / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(leaf_text(t, a, s, body), encoding="utf-8")
        (self.vault / "reference" / "MANIFEST.md").write_text("---\ntype: index\n---\n- [[ref-b]] inkhub_platform/api/main.py\n")
        (self.vault / "handoff").mkdir()
        (self.vault / "handoff" / "MANIFEST.md").write_text("---\ntype: index\n---\n# handoff/\n\n- [[x]] y\n")
        self.logs = mock.patch.object(brain, "log_event").start()
        self.addCleanup(mock.patch.stopall)


class Index(VaultCase):
    def test_index_reads_frontmatter_and_paths(self):
        leaf = {l.slug: l for l in brain.build_index(self.vault)}["gotcha-a"]
        self.assertEqual((leaf.type, leaf.as_of, leaf.folder), ("gotcha", "2026-09-01", "gotchas"))
        self.assertEqual(leaf.repo_paths, ["inkhub_platform/api/main.py"])
        self.assertEqual(leaf.tags, ["platform", "api"])

    def test_index_skips_superseded_manifest_and_unreadable(self):
        (self.vault / "reference" / "broken.md").write_bytes(b"\xff\xfe not utf8")
        slugs = {l.slug for l in brain.build_index(self.vault)}
        self.assertNotIn("old-ref", slugs)
        self.assertNotIn("MANIFEST", slugs)
        self.assertNotIn("broken", slugs)
        self.assertIn("ref-b", slugs)
        self.logs.assert_called()  # the unreadable leaf is reported, not fatal

    def test_index_cache_is_reused_until_the_vault_changes(self):
        cache = self.vault.parent / "index.json"
        brain.build_index(self.vault, cache)
        with mock.patch.object(brain, "parse_leaf", side_effect=AssertionError("re-parsed")):
            self.assertEqual(len(brain.build_index(self.vault, cache)), 4)
        (self.vault / "gotchas" / "gotcha-e.md").write_text(leaf_text("gotcha", "2026-10-01", "current", "x"))
        self.assertEqual(len(brain.build_index(self.vault, cache)), 5)

    def test_match_ranks_exact_over_directory_over_package(self):
        got = brain.match(brain.build_index(self.vault), ["inkhub_platform/api/main.py"], 12)
        self.assertEqual([l.slug for l, _ in got], ["gotcha-a", "inv-d", "ref-b", "decision-c"])
        self.assertIn("names inkhub_platform/api/main.py", got[0][1])

    def test_investigation_only_on_exact_file(self):
        got = brain.match(brain.build_index(self.vault), ["inkhub_platform/api/other.py"], 12)
        self.assertNotIn("inv-d", [l.slug for l, _ in got])
        got = brain.match(brain.build_index(self.vault), ["inkhub_platform/api/main.py"], 12)
        self.assertIn("inv-d", [l.slug for l, _ in got])

    def test_limit(self):
        self.assertEqual(len(brain.match(brain.build_index(self.vault), ["inkhub_platform/api/main.py"], 2)), 2)


class Ground(VaultCase):
    def test_ground_copies_matches_and_writes_brain_md(self):
        n = brain.ground(self.vault, self.inputs, ["inkhub_platform/api/main.py"], 12)
        self.assertEqual(n, 4)
        self.assertTrue((self.inputs / "brain" / "gotchas" / "gotcha-a.md").exists())
        self.assertIn("`gotchas/gotcha-a` gotcha, as_of 2026-09-01, current: names inkhub_platform/api/main.py",
                      (self.inputs / "brain.md").read_text())

    def test_ground_without_match_or_vault_writes_nothing(self):
        self.assertEqual(brain.ground(self.vault, self.inputs, ["README.md"], 12), 0)
        self.assertEqual(brain.ground(Path("/nonexistent"), self.inputs, ["x.py"], 12), 0)
        self.assertFalse((self.inputs / "brain.md").exists())

    def test_prompt_gets_the_brain_section_only_with_brain_md(self):
        import review
        with mock.patch.object(review, "BRAIN_DIR", self.vault):
            self.assertEqual(review.brain_note(self.inputs), ("", []))
            brain.ground(self.vault, self.inputs, ["inkhub_platform/api/main.py"], 12)
            text, dirs = review.brain_note(self.inputs)
        text = " ".join(text.split())
        self.assertIn("do not argue it again", text)
        self.assertIn("can be stale", text)
        self.assertEqual(dirs, ["--add-dir", str(self.vault)])


if __name__ == "__main__":
    unittest.main()
