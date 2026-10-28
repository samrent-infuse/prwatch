import os

# Before importing decide (through review): identity and policy are read at import time.
os.environ.update(PRWATCH_ME="me,me-alt", PRWATCH_REVIEWER="reviewer-bot",
                  PRWATCH_REQUIRED_CHECK="scan-verdict", PRWATCH_REPO="example-org/example-repo",
                  PRWATCH_SETTLE_MIN="30", PRWATCH_SKIP_TITLE="do not review",
                  PRWATCH_MAX_CHANGE_ROUNDS="2")

import datetime
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import brain
import brain_run

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


class Apply(VaultCase):
    def setUp(self):
        super().setUp()
        self.leaf = {l.slug: l for l in brain.build_index(self.vault)}["ref-b"]
        self.mtime = self.leaf.path.stat().st_mtime

    def apply(self, edits, mtime=None):
        return brain.apply(self.leaf, edits, self.mtime if mtime is None else mtime, [1401, 1403], "abcdef1234",
                           self.backups, "2026-10-28")

    def test_edit_applies_once_updates_frontmatter_and_backs_up(self):
        self.assertEqual(self.apply([{"old": "port 5432", "new": "port 6432"}])[0], "edited")
        text = self.leaf.path.read_text()
        self.assertIn("port 6432", text)
        self.assertIn("as_of: 2026-10-28", text)
        self.assertIn('source: "built from source; refreshed from #1401, #1403 at abcdef1 by prwatch"', text)
        self.assertIn("port 5432", (self.backups / "2026-10-28" / "reference" / "ref-b.md").read_text())

    def test_crlf_leaf_stays_crlf(self):
        self.leaf.path.write_bytes(self.leaf.path.read_bytes().replace(b"\n", b"\r\n"))
        out = self.apply([{"old": "port 5432", "new": "port 6432"}], self.leaf.path.stat().st_mtime)
        self.assertEqual(out[0], "edited")
        raw = self.leaf.path.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"as_of: 2026-10-28\r\n", raw)

    def test_ambiguous_or_missing_old_is_stuck_and_untouched(self):
        before = self.leaf.path.read_bytes()
        self.assertEqual(self.apply([{"old": "the", "new": "x"}])[0], "stuck")
        self.assertEqual(self.apply([{"old": "not in the leaf", "new": "x"}])[0], "stuck")
        self.assertEqual(self.leaf.path.read_bytes(), before)
        self.assertFalse(self.backups.exists())

    def test_shrink_over_40_percent_or_lost_title_is_stuck(self):
        body = self.leaf.path.read_text().split("---", 2)[2]
        self.assertEqual(self.apply([{"old": body, "new": "\nshort\n"}])[0], "stuck")
        self.assertEqual(self.apply([{"old": 'title: "T"\n', "new": ""}])[0], "stuck")

    def test_changed_on_disk_since_read_is_skipped_without_backup(self):
        self.assertEqual(self.apply([{"old": "port 5432", "new": "x"}], self.mtime - 10)[0], "skipped")
        self.assertFalse(self.backups.exists())

    def test_log_creates_leaf_and_manifest_line_once(self):
        brain.log(self.vault, "a")
        brain.log(self.vault, "b")
        self.assertEqual((self.vault / "handoff" / "MANIFEST.md").read_text().count("brain-refresh-log"), 1)
        text = (self.vault / "handoff" / "brain-refresh-log.md").read_text()
        self.assertTrue(text.endswith("- a\n- b\n"))
        self.assertEqual(brain.parse_frontmatter(text)["type"], "handoff")

    def test_undo_restores_newest_backup(self):
        self.apply([{"old": "port 5432", "new": "port 6432"}])
        restored = brain.undo(self.vault, self.backups, "reference/ref-b", None)
        self.assertIn("port 5432", self.leaf.path.read_text())
        self.assertEqual(restored, self.backups / "2026-10-28" / "reference" / "ref-b.md")
        with self.assertRaises(FileNotFoundError):
            brain.undo(self.vault, self.backups, "reference/nope", None)


class RepoCase(VaultCase):
    """A vault plus a git repo whose commits carry dates, with review.REPO_DIR pointed at it."""

    def git(self, *args, date=None):
        env = {**os.environ, **({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {})}
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.repo, env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, files, msg, date):
        for name, text in files.items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", msg, date=date)
        return self.git("rev-parse", "HEAD")

    def setUp(self):
        super().setUp()
        import review
        import store
        self.repo = self.vault.parent / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.base = self.commit({"inkhub_platform/api/routes.py": "a\n", "inkhub_platform/api/other.py": "o\n"},
                                "base", "2026-01-02T10:00:00Z")
        self.head_tests_only = self.commit({"tests/test_x.py": "t\n", "README.md": "r\n"},
                                           "tests (#7)", "2026-03-01T10:00:00Z")
        self.head_touching_named = self.commit({"inkhub_platform/api/routes.py": "b\n"},
                                               "routes (#8)", "2026-05-01T10:00:00Z")
        self.git("update-ref", f"refs/remotes/origin/{review.BASE_BRANCH}", self.head_touching_named)
        self.git("checkout", "-q", "-b", "renamed", self.base)
        self.git("mv", "inkhub_platform/api/routes.py", "inkhub_platform/api/routes2.py")
        self.git("commit", "-qm", "rename", date="2026-02-01T10:00:00Z")
        self.head_renamed = self.git("rev-parse", "HEAD")
        mock.patch.object(review, "REPO_DIR", self.repo).start()
        self.store = store.Store(str(self.vault.parent / "s.db"))
        self.addCleanup(self.store.db.close)
        self.index = brain.build_index(self.vault)

    def store_row(self, slug):
        return self.store.brain_row(slug)


class Scan(RepoCase):
    def setUp(self):
        super().setUp()
        self.store.brain_upsert("ref-b", path="reference/ref-b.md", sync_sha=self.base, state="ok")

    def test_scan_marks_due_only_on_named_file_change(self):
        self.assertEqual(brain_run.scan(self.store, self.index, self.head_touching_named), ["ref-b"])
        self.assertEqual(self.store_row("ref-b")["state"], "due")
        self.assertTrue(self.store_row("ref-b")["touched_at"])

    def test_scan_moves_sync_when_only_tests_or_other_files_changed(self):
        self.assertEqual(brain_run.scan(self.store, self.index, self.head_tests_only), [])
        self.assertEqual(self.store_row("ref-b")["sync_sha"], self.head_tests_only)
        self.assertEqual(self.store_row("ref-b")["state"], "ok")

    def test_unrelated_master_move_does_not_restart_the_settle_wait(self):
        brain_run.scan(self.store, self.index, self.head_touching_named)
        self.store.brain_upsert("ref-b", touched_at="2026-01-01T00:00:00Z")
        self.git("checkout", "-q", "main")
        later =self.commit({"inkhub_platform/api/other.py": "o2\n"}, "other (#9)", "2026-06-01T10:00:00Z")
        brain_run.scan(self.store, self.index, later, prev_head=self.head_touching_named)
        self.assertEqual(self.store_row("ref-b")["touched_at"], "2026-01-01T00:00:00Z")
        again = self.commit({"inkhub_platform/api/routes.py": "c\n"}, "routes again (#10)", "2026-06-02T10:00:00Z")
        brain_run.scan(self.store, self.index, again, prev_head=later)
        self.assertNotEqual(self.store_row("ref-b")["touched_at"], "2026-01-01T00:00:00Z")

    def test_rename_or_delete_of_named_file_counts(self):
        self.assertEqual(brain_run.changed_named_files(self.base, self.head_renamed, ["inkhub_platform/api/routes.py"]),
                         ["inkhub_platform/api/routes.py"])
        self.assertEqual(brain_run.changed_named_files(self.base, self.head_tests_only,
                                                       ["inkhub_platform/api/routes.py"]), [])

    def test_unknown_sync_sha_reanchors_from_as_of(self):
        self.store.brain_upsert("ref-b", sync_sha="0" * 40, state="ok")
        self.assertIsNone(brain_run.changed_named_files("0" * 40, self.head_tests_only, ["x.py"]))
        brain_run.scan(self.store, self.index, self.head_tests_only)
        self.assertEqual(self.store_row("ref-b")["sync_sha"], self.head_tests_only)

    def test_anchor_is_last_commit_on_or_before_as_of(self):
        self.assertEqual(brain_run.anchor("2026-01-02"), self.base)
        self.assertEqual(brain_run.anchor("2026-04-20"), self.head_tests_only)
        self.assertEqual(brain_run.anchor("undated"), "")

    def test_master_head_comes_from_the_tick_query(self):
        import fetch
        data = {"data": {"repository": {"defaultBranchRef": {"target": {"oid": "abc"}},
                                        "pullRequests": {"nodes": []}}}}
        with mock.patch.object(fetch, "gh", return_value=json.dumps(data)):
            fetch.open_prs()
        self.assertEqual(fetch.master_head(), "abc")


class Group(VaultCase):
    def setUp(self):
        super().setUp()
        for slug, path in (("ref-old", "inkhub_ai/a.py"), ("ref-same-package", "inkhub_ai/b/c.py"),
                           ("ref-recent", "inkhub_ai/d.py"), ("ref-other", "inkhub_crawling/e.py")):
            (self.vault / "reference" / f"{slug}.md").write_text(leaf_text("reference", "2026-04-20", "current", path))
        self.index = brain.build_index(self.vault)
        self.now = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.timezone.utc)
        row = lambda slug, touched, state="due", nxt="": {"slug": slug, "state": state, "touched_at": touched,
                                                         "next_try_at": nxt}
        self.rows = [row("ref-same-package", "2026-10-28T10:30:00Z"), row("ref-old", "2026-10-28T10:00:00Z"),
                     row("ref-recent", "2026-10-28T11:50:00Z"), row("ref-other", "2026-10-28T09:00:00Z", "ok"),
                     row("ref-b", "2026-10-28T09:00:00Z", "due", "2026-10-28T13:00:00Z")]

    def test_pick_group_waits_for_settle_and_groups_by_package(self):
        group = brain.pick_group(self.rows, self.index, self.now, 60)
        self.assertEqual([l.slug for l in group], ["ref-old", "ref-same-package"])

    def test_pick_group_caps_at_six(self):
        rows = []
        for i in range(9):
            (self.vault / "reference" / f"many-{i}.md").write_text(leaf_text("reference", "2026-04-20", "current",
                                                                             f"inkhub_ai/m{i}.py"))
            rows.append({"slug": f"many-{i}", "state": "due", "touched_at": "2026-10-28T08:00:00Z", "next_try_at": ""})
        self.assertEqual(len(brain.pick_group(rows, brain.build_index(self.vault), self.now, 60)), 6)


OK_OUTPUT = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.31,
             "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 100, "outputTokens": 50,
                                                  "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}},
             "structured_output": {"leaves": [{"slug": "ref-b", "verdict": "edited",
                                               "edits": [{"old": "port 5432", "new": "port 6432"}],
                                               "why": "routes.py: b"}],
                                   "suggestions": [{"kind": "manifest_hook", "slug": "ref-b", "text": "say 6432"}]}}


class Refresh(RepoCase):
    def setUp(self):
        super().setUp()
        import review
        mock.patch.object(review, "WORK", self.vault.parent / "work").start()
        mock.patch.object(review, "HERE", self.vault.parent / "here").start()
        self.store.brain_upsert("ref-b", path="reference/ref-b.md", sync_sha=self.base, state="due",
                                touched_at="2026-10-28T00:00:00Z")
        self.leaf = {l.slug: l for l in self.index}["ref-b"]
        self.git("checkout", "-q", "main")

    def test_validate_rejects_unknown_verdict_and_bad_edits(self):
        self.assertIsNone(brain_run.validate({"leaves": [{"slug": "x", "verdict": "maybe"}], "suggestions": []})[0])
        self.assertIsNone(brain_run.validate({"leaves": [{"slug": "x", "verdict": "edited", "edits": [{"old": 1}]}]})[0])
        self.assertIsNotNone(brain_run.validate({"leaves": [{"slug": "x", "verdict": "unchanged", "edits": [],
                                                             "why": ""}], "suggestions": []})[0])

    def test_refresh_applies_edits_logs_and_records_spend(self):
        with mock.patch.object(brain_run, "_claude", return_value=(json.dumps(OK_OUTPUT), 0)) as c:
            brain_run.refresh(self.store, self.vault, [self.leaf], self.head_touching_named)
        prompt = c.call_args.args[0]
        self.assertIn("diff.txt", prompt)
        row = self.store_row("ref-b")
        self.assertEqual((row["state"], row["sync_sha"]), ("ok", self.head_touching_named))
        self.assertIn("port 6432", self.leaf.path.read_text())
        self.assertIn("#8", self.leaf.path.read_text())  # the PR number from the commit subject
        log = (self.vault / "handoff" / "brain-refresh-log.md").read_text()
        self.assertIn("[[ref-b]]", log)
        self.assertIn("say 6432", log)
        self.assertEqual(tuple(self.store.db.execute("select kind, cost_usd from calls").fetchone()),
                         ("brain_refresh", 0.31))

    def test_unchanged_only_moves_the_sync_point(self):
        out = {**OK_OUTPUT, "structured_output": {"leaves": [{"slug": "ref-b", "verdict": "unchanged", "edits": [],
                                                              "why": ""}], "suggestions": []}}
        before = self.leaf.path.read_bytes()
        with mock.patch.object(brain_run, "_claude", return_value=(json.dumps(out), 0)):
            brain_run.refresh(self.store, self.vault, [self.leaf], self.head_touching_named)
        self.assertEqual(self.leaf.path.read_bytes(), before)
        self.assertEqual(self.store_row("ref-b")["sync_sha"], self.head_touching_named)

    def test_failed_call_retries_then_sticks(self):
        with mock.patch.object(brain_run, "_claude", return_value=("not json", 1)):
            for i in range(3):
                self.store.brain_upsert("ref-b", next_try_at="")
                brain_run.refresh(self.store, self.vault, [self.leaf], self.head_touching_named)
                if i == 0:
                    self.assertEqual(self.store_row("ref-b")["state"], "due")
                    self.assertTrue(self.store_row("ref-b")["next_try_at"])
        self.assertEqual(self.store_row("ref-b")["state"], "stuck")

    def test_verify_mode_sends_current_files(self):
        with mock.patch.object(brain_run, "_claude", return_value=(json.dumps(OK_OUTPUT), 0)) as c:
            brain_run.refresh(self.store, self.vault, [self.leaf], self.head_touching_named, verify=True)
        self.assertIn("current/", c.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
