import os

# Before importing decide: identity and policy are read at import time.
os.environ.update(PRWATCH_ME="me,me-alt", PRWATCH_REVIEWER="reviewer-bot",
                  PRWATCH_REQUIRED_CHECK="scan-verdict", PRWATCH_REPO="example-org/example-repo",
                  PRWATCH_SETTLE_MIN="30", PRWATCH_SKIP_TITLE="do not review", PRWATCH_WATCHED_AUTHORS="bob",
                  PRWATCH_MAX_CHANGE_ROUNDS="2")

import json
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from prwatch import decide
from prwatch import fetch
from prwatch import main
from prwatch import post
from prwatch import render
from prwatch import review
from prwatch.store import Store

V_OK = {"verdict": "approve", "summary": "Looks right — small change.", "findings": [
    {"severity": "minor", "file": "a.py", "line": 3, "title": "naming", "detail": "x – y"}]}
PR = {"state": "OPEN", "head": "h2", "draft": False, "author": "alice", "labels": [], "files": ["api/a.py"],
      "merged_at_head": False}


class Render(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(render.validate(V_OK), (V_OK, None))
        self.assertIsNone(render.validate(None)[0])
        self.assertIsNone(render.validate({"verdict": "yes", "summary": "", "findings": []})[0])
        self.assertIsNone(render.validate({**V_OK, "findings": [{"severity": "high", "title": "x"}]})[0])

    def test_approve_needs_every_rule(self):
        self.assertEqual(render.decide_event(V_OK, PR, {"me"}, True), ("APPROVE", ""))
        cases = [
            (V_OK, {**PR, "draft": True}, True, "draft"),
            (V_OK, {**PR, "labels": ["hold"]}, True, "hold label"),
            (V_OK, {**PR, "files": ["deploy/terraform/x.tf"]}, True, "infra path"),
            (V_OK, {**PR, "author": "me"}, True, "own PR"),
            (V_OK, {**PR, "merged_at_head": True}, True, "merged"),
            (V_OK, PR, False, "not enabled"),
        ]
        for v, pr, allow, why in cases:
            event, reason = render.decide_event(v, pr, {"me"}, allow)
            self.assertEqual(event, "COMMENT", why)
            self.assertIn(why, reason)
        important = {**V_OK, "findings": [{"severity": "important", "title": "t", "detail": "d"}]}
        self.assertEqual(render.decide_event(important, PR, set(), True), ("COMMENT", ""))
        critical = {**V_OK, "findings": [{"severity": "critical", "title": "t", "detail": "d"}]}
        self.assertEqual(render.decide_event(critical, PR, set(), True, True), ("REQUEST_CHANGES", ""))
        self.assertEqual(render.decide_event(critical, PR, set(), True, False)[0], "COMMENT")
        self.assertEqual(render.decide_event(critical, {**PR, "author": "me"}, {"me"}, True, True)[0], "COMMENT")
        self.assertEqual(render.decide_event(critical, {**PR, "draft": True}, set(), True, True)[0], "COMMENT")
        # an approval waits for CI that is still running and is withheld when CI failed
        self.assertEqual(render.decide_event(V_OK, {**PR, "ci": ("pending", "E2E pending")}, set(), True)[0], "WAIT")
        self.assertEqual(render.decide_event(V_OK, {**PR, "ci": ("failed", "Lint failure")}, set(), True)[0], "COMMENT")
        self.assertEqual(render.decide_event(important, {**PR, "ci": ("pending", "x")}, set(), True)[0], "COMMENT")
        # the model's verdict word is not consulted: zero critical and important approves
        self.assertEqual(render.decide_event({**V_OK, "verdict": "comment"}, PR, set(), True), ("APPROVE", ""))

    def test_body_plain_footer_marker(self):
        b = render.body(V_OK, "abcdef1234", "review:1:abcdef1234")
        self.assertNotIn("—", b)
        self.assertNotIn("–", b)
        self.assertIn("Automated review (Claude Opus), reviewed at abcdef1.", b)
        self.assertIn("<!-- prwatch review:1:abcdef1234 -->", b)
        self.assertIn("**Minor** `a.py:3`: naming", b)


class Post(unittest.TestCase):
    def run_post(self, api_results, existing=None, pr=PR):
        with mock.patch.object(post, "pr_facts", return_value=pr), \
             mock.patch.object(post, "existing_review", return_value=existing), \
             mock.patch.object(post, "_api", side_effect=api_results) as api:
            return post.post(1, "h2", "review:1:h2", V_OK, True), api

    def test_posted_and_read_back(self):
        marker = render.marker("review:1:h2")
        (out, detail, rid), _ = self.run_post([(200, {"id": 9}, ""), (200, {"id": 9, "body": "x " + marker}, "")])
        self.assertEqual((out, rid), ("posted", 9))
        self.assertTrue(detail.startswith("APPROVE"))

    def test_already_on_github_posts_nothing(self):
        (out, _, rid), api = self.run_post([], existing=5)
        self.assertEqual((out, rid), ("posted", 5))
        api.assert_not_called()

    def test_head_moved_drops(self):
        (out, _, _), api = self.run_post([], pr={**PR, "head": "h3"})
        self.assertEqual(out, "dropped")
        api.assert_not_called()

    def test_5xx_and_network_retry_422_sticks(self):
        self.assertEqual(self.run_post([(502, None, "HTTP 502")])[0][0], "retry")
        self.assertEqual(self.run_post([(0, None, "connection reset")])[0][0], "retry")
        self.assertEqual(self.run_post([(422, None, "HTTP 422 commit_id not part of PR")])[0][0], "stuck")

    def test_read_back_mismatch_retries(self):
        self.assertEqual(self.run_post([(200, {"id": 9}, ""), (404, None, "")])[0][0], "retry")


class Drain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(f"{self.tmp.name}/s.db")
        self.store.upsert_job("review:1:h2", pr=1, head="h2", kind="review", state="post",
                              verdict_json=json.dumps(V_OK))
        self.logs = mock.patch.object(main, "log").start()

    def tearDown(self):
        mock.patch.stopall()
        self.store.db.close()
        self.tmp.cleanup()

    def drain(self, outcome):
        with mock.patch.object(post, "post", return_value=outcome) as p:
            main.drain_posts(self.store)
        return p

    def test_backoff_then_stuck_without_rerunning_the_model(self):
        for i, minutes in enumerate(post.POST_BACKOFF_MIN, 1):
            self.store.upsert_job("review:1:h2", next_try_at="")  # pretend the backoff elapsed
            self.drain(("retry", "HTTP 502", None))
            job = self.store.job("review:1:h2")
            self.assertEqual((job["state"], job["post_tries"]), ("post", i))
            self.assertTrue(job["next_try_at"])
        self.store.upsert_job("review:1:h2", next_try_at="")
        self.drain(("retry", "HTTP 502", None))
        self.assertEqual(self.store.job("review:1:h2")["state"], "stuck")
        self.assertEqual(self.store.calls_today(), 0)

    def test_wait_for_ci_rechecks_without_counting_a_retry(self):
        self.drain(("wait", "approval waits: CI E2E pending", None))
        job = self.store.job("review:1:h2")
        self.assertEqual((job["state"], job["post_tries"]), ("post", 0))
        self.assertTrue(job["next_try_at"])
        self.assertEqual(self.logs.call_count, 1)
        self.store.upsert_job("review:1:h2", next_try_at="")
        self.drain(("wait", "approval waits: CI E2E pending", None))
        self.assertEqual(self.logs.call_count, 1)  # same reason: not logged again

    def test_not_due_is_left_alone(self):
        self.store.upsert_job("review:1:h2", next_try_at="2999-01-01T00:00:00Z")
        self.drain(("posted", "x", 1)).assert_not_called()

    def test_posted_records_head(self):
        self.drain(("posted", "COMMENT", 77))
        self.assertEqual(self.store.job("review:1:h2")["review_id"], 77)
        self.assertEqual(self.store.row(1)[0].last_reviewed_head, "h2")


class PreviousReview(unittest.TestCase):
    REVIEWS = [
        {"id": 1, "user": {"login": "me"}, "commit_id": "old", "body": "older head"},
        {"id": 2, "user": {"login": "reviewer-bot"}, "commit_id": "h1", "body": "not mine"},
        {"id": 3, "user": {"login": "me"}, "commit_id": "h1", "body": "first on h1"},
        {"id": 4, "user": {"login": "me-alt"}, "commit_id": "h1", "body": "Important: fix x"},
        {"id": 5, "user": {"login": "me"}, "commit_id": "h1", "body": "  "},
    ]

    def fake_gh(self, *args):
        if args[1].endswith("/reviews/4/comments"):
            return json.dumps([{"path": "a.py", "line": 3, "body": "here"}])
        return json.dumps(self.REVIEWS)

    def test_last_review_of_mine_on_that_head_with_inline_comments(self):
        with mock.patch.object(fetch, "gh", side_effect=self.fake_gh):
            self.assertEqual(fetch.previous_review(9, "h1"), "Important: fix x\n\nInline comments:\na.py:3: here")
            self.assertEqual(fetch.previous_review(9, "none"), "")

    def test_run_review_falls_back_to_github_when_no_local_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(f"{tmp.name}/s.db")
        self.addCleanup(store.db.close)
        a = decide.Action(kind="rereview", pr=9, head="h2", base="h1", reason="")
        with mock.patch.object(main.act, "REVIEWS", Path(tmp.name)), \
                mock.patch.object(main, "log"), \
                mock.patch.object(fetch, "previous_review", return_value="from github") as prev, \
                mock.patch.object(main.review, "run", return_value=(None, "boom", "", None)) as run:
            main.run_review(store, a)
        prev.assert_called_once_with(9, "h1")
        self.assertEqual(run.call_args.args[3], "from github")


class SamePatch(unittest.TestCase):
    """patch_lines against a real git repository: a rebase keeps the patch, an edit changes it."""

    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.dir,
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, name, text, msg):
        (Path(self.dir) / name).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", msg)
        return self.git("rev-parse", "HEAD")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.git("init", "-q", "-b", "main")
        self.commit("a.py", "one\ntwo\n", "base")
        self.git("checkout", "-qb", "pr")
        self.reviewed = self.commit("a.py", "one\ntwo\nthree\n", "pr change")
        self.git("checkout", "-q", "main")
        newer = self.commit("b.py", "other\n", "base moves on")
        self.git("update-ref", f"refs/remotes/origin/{review.BASE_BRANCH}", newer)
        self.git("checkout", "-qb", "rebased")
        self.rebased = self.commit("a.py", "one\ntwo\nthree\n", "pr change, rebased")
        self.edited = self.commit("a.py", "one\ntwo\nthree!\n", "pr change, edited")
        patcher = mock.patch.object(review, "REPO_DIR", Path(self.dir))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rebase_keeps_the_patch_and_an_edit_changes_it(self):
        self.assertEqual(review.patch_lines(self.reviewed), [("a.py", "+three")])
        self.assertEqual(review.patch_lines(self.reviewed), review.patch_lines(self.rebased))
        self.assertNotEqual(review.patch_lines(self.reviewed), review.patch_lines(self.edited))


class ReviewerModel(unittest.TestCase):
    def diff(self, path, lines):
        head = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1 +1 @@"]
        return "\n".join(head + ["+x"] * lines) + "\n"

    def test_small_safe_diff_gets_the_small_model(self):
        with mock.patch.object(review, "REVIEWER_MODEL_SMALL", "sonnet"):
            self.assertEqual(review.reviewer_model(self.diff("api/a.py", 10))[0], "sonnet")
            self.assertEqual(review.reviewer_model(self.diff("api/a.py", 401))[0], "opus")
            self.assertEqual(review.reviewer_model(self.diff("deploy/x.tf", 3))[0], "opus")
            self.assertEqual(review.reviewer_model(self.diff("svc/main.py", 3))[0], "opus")
        self.assertEqual(review.reviewer_model(self.diff("api/a.py", 10))[0], "opus")  # unset = always Opus


class CarriedVerdict(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(f"{tmp.name}/s.db")
        self.addCleanup(self.store.db.close)
        self.store.upsert_job("review:9:h1", pr=9, head="h1", kind="review", state="posted",
                              verdict_json=json.dumps(V_OK))
        self.a = decide.Action(kind="rereview", pr=9, head="h2", base="h1", reason="")
        for p in (mock.patch.object(main.act, "REVIEWS", Path(tmp.name)), mock.patch.object(main, "log")):
            p.start()
            self.addCleanup(p.stop)

    def test_same_patch_posts_the_earlier_verdict_without_a_model_call(self):
        with mock.patch.object(review, "same_patch", return_value=True), \
                mock.patch.object(review, "run") as run:
            main.run_review(self.store, self.a)
            main.run_review(self.store, decide.Action(kind="rereview", pr=9, head="h3", base="h2", reason=""))
        run.assert_not_called()
        v = json.loads(self.store.job("review:9:h3")["verdict_json"])
        self.assertEqual(v["findings"], V_OK["findings"])
        self.assertTrue(v["summary"].startswith(main.CARRIED + "h2"))
        self.assertEqual(v["summary"].count(main.CARRIED), 1)
        self.assertEqual(self.store.calls_today(), 0)

    def test_changed_or_unknown_patch_runs_the_model(self):
        for same in (False, None):
            with mock.patch.object(review, "same_patch", return_value=same), \
                    mock.patch.object(main.fetch, "previous_review", return_value=""), \
                    mock.patch.object(review, "run", return_value=(None, "boom", "", None)) as run:
                main.run_review(self.store, self.a)
            run.assert_called_once()


class Usage(unittest.TestCase):
    OUT = {"total_cost_usd": 1.5, "modelUsage": {
        "claude-opus-5-5": {"inputTokens": 10, "outputTokens": 200, "cacheReadInputTokens": 3000,
                            "cacheCreationInputTokens": 400},
        "claude-haiku-5-5": {"inputTokens": 5, "outputTokens": 20, "cacheReadInputTokens": 0,
                             "cacheCreationInputTokens": 0}}}

    def test_usage_sums_every_model(self):
        self.assertEqual(review.usage_of(self.OUT), {"in_tokens": 15, "out_tokens": 220, "cache_read": 3000,
                                                     "cache_write": 400, "cost_usd": 1.5})
        self.assertEqual(review.usage_of({})["out_tokens"], 0)

    def test_ledger_stores_and_sums_today(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(f"{tmp.name}/s.db")
        self.addCleanup(store.db.close)
        u = review.usage_of(self.OUT)
        store.end_call(store.start_call(1, "review", "opus", "h", "r"), "verdict approve", u)
        store.end_call(store.start_call(2, "review", "opus", "h", "r"), "verdict comment", u)
        store.end_call(store.start_call(3, "review", "opus", "h", "r"), "timed out", None)
        today = store.usage_today()
        self.assertEqual((today["out_tokens"], today["cache_read"], today["cost_usd"]), (440, 6000, 3.0))
        self.assertIn("$1.50 list", main.usage_note(u))
        self.assertEqual(main.usage_note(None), "")

    def test_old_database_gets_the_columns(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = sqlite3.connect(f"{tmp.name}/old.db")
        db.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, pr INTEGER NOT NULL, "
                   "kind TEXT NOT NULL, model TEXT NOT NULL, head TEXT NOT NULL, reason TEXT NOT NULL, outcome TEXT)")
        db.commit()
        db.close()
        store = Store(f"{tmp.name}/old.db")
        self.addCleanup(store.db.close)
        self.assertIn("cost_usd", {r[1] for r in store.db.execute("PRAGMA table_info(calls)")})


if __name__ == "__main__":
    unittest.main()
