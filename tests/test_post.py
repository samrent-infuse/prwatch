import os

# Before importing decide: identity and policy are read at import time.
os.environ.update(PRWATCH_ME="me,me-alt", PRWATCH_REVIEWER="reviewer-bot",
                  PRWATCH_REQUIRED_CHECK="scan-verdict", PRWATCH_REPO="example-org/example-repo",
                  PRWATCH_SETTLE_MIN="30", PRWATCH_SKIP_TITLE="do not review", PRWATCH_WATCHED_AUTHORS="bob",
                  PRWATCH_MAX_CHANGE_ROUNDS="2")

import datetime
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
        # only a critical finding blocks: important ones are approved "with fixes" (Samuel, 2026-10-29)
        important = {**V_OK, "findings": [{"severity": "important", "title": "t", "detail": "d"}]}
        self.assertEqual(render.decide_event(important, PR, set(), True), ("APPROVE", ""))
        self.assertEqual(render.decide_event(important, {**PR, "files": ["deploy/terraform/x.tf"]}, set(), True)[0],
                         "COMMENT")  # the other gates still hold
        critical = {**V_OK, "findings": [{"severity": "critical", "title": "t", "detail": "d"}]}
        self.assertEqual(render.decide_event(critical, PR, set(), True, True), ("REQUEST_CHANGES", ""))
        self.assertEqual(render.decide_event(critical, PR, set(), True, False)[0], "COMMENT")
        self.assertEqual(render.decide_event(critical, {**PR, "author": "me"}, {"me"}, True, True)[0], "COMMENT")
        self.assertEqual(render.decide_event(critical, {**PR, "draft": True}, set(), True, True)[0], "COMMENT")
        # an approval waits for CI that is still running and is withheld when CI failed
        self.assertEqual(render.decide_event(V_OK, {**PR, "ci": ("pending", "E2E pending")}, set(), True)[0], "WAIT")
        self.assertEqual(render.decide_event(V_OK, {**PR, "ci": ("failed", "Lint failure")}, set(), True)[0], "COMMENT")
        self.assertEqual(render.decide_event(important, {**PR, "ci": ("pending", "x")}, set(), True)[0], "WAIT")
        # the model's verdict word is not consulted: zero critical and important approves
        self.assertEqual(render.decide_event({**V_OK, "verdict": "comment"}, PR, set(), True), ("APPROVE", ""))

    def test_body_plain_footer_marker(self):
        b = render.body(V_OK, "abcdef1234", "review:1:abcdef1234")
        self.assertNotIn("—", b)
        self.assertNotIn("–", b)
        self.assertIn("Automated review (Claude Opus), reviewed at abcdef1.", b)
        self.assertIn("<!-- prwatch review:1:abcdef1234 -->", b)
        self.assertIn("### Minor\n1. `a.py:3`: naming", b)

    def test_body_layout_like_a_reviewer_writes_it(self):
        v = {**V_OK, "strengths": ["Small and tested."], "not_judged": ["the migration, not run"],
             "findings": V_OK["findings"] + [{"severity": "critical", "file": "b.py", "line": 9, "title": "boom",
                                               "detail": "it breaks"}]}
        b = render.body(v, "abcdef1234", "review:1:abcdef1234", event="REQUEST_CHANGES")
        self.assertTrue(b.startswith("## Automated code review\n"))
        self.assertIn("**Verdict: changes requested.**", b)
        for h in ("### Strengths\n- Small and tested.", "### Critical\n1. `b.py:9`: boom\n   it breaks",
                  "### Not judged\n- the migration, not run"):
            self.assertIn(h, b)
        self.assertNotIn("### Important", b)  # empty sections are left out
        self.assertLess(b.index("### Critical"), b.index("### Minor"))

    def test_approval_with_important_findings_says_with_fixes(self):
        v = {**V_OK, "findings": [{"severity": "important", "title": "t", "detail": "d"},
                                  {"severity": "important", "title": "u", "detail": "e"}]}
        b = render.body(v, "abcdef1234", "review:1:abcdef1234", event="APPROVE")
        self.assertIn("**Verdict: approve, with fixes: settle the 2 important findings before merging.**", b)
        self.assertIn("**Verdict: approve.**", render.body(V_OK, "abcdef1234", "review:1:abcdef1234", event="APPROVE"))

    def test_prompt_makes_blocking_findings_critical(self):
        text = " ".join(review.PROMPT.split())
        self.assertIn("must be a critical finding", text)
        self.assertNotIn("must be a critical or important finding", text)

    def test_rereview_body_has_the_earlier_findings_table(self):
        v = {**V_OK, "kind": "rereview", "earlier": [{"finding": "null check", "status": "fixed", "checked_at": "a.py:4"}]}
        b = render.body(v, "abcdef1234", "review:1:abcdef1234", event="APPROVE")
        self.assertTrue(b.startswith("## Automated re-review\n"))
        self.assertIn("**Verdict: approve.**", b)
        self.assertIn("| 1 | null check | fixed | `a.py:4` |", b)

    def test_moved_head_note(self):
        b = render.body(V_OK, "abcdef1234", "review:1:abcdef1234", event="COMMENT", moved_to="9999999aaa")
        self.assertIn("The PR has since moved to `9999999`", b)


class Post(unittest.TestCase):
    def run_post(self, api_results, existing=None, pr=PR):
        with mock.patch.object(post, "pr_facts", return_value=pr), \
             mock.patch.object(post, "existing_review", return_value=existing), \
             mock.patch.object(post, "_api", side_effect=api_results) as api:
            return post.post(1, "h2", "review:1:h2", V_OK, True), api

    def test_expired_ci_wait_posts_a_comment_saying_why(self):
        marker = render.marker("review:1:h2")
        with mock.patch.object(post, "pr_facts", return_value={**PR, "ci": ("pending", "E2E Tests pending")}), \
                mock.patch.object(post, "existing_review", return_value=None), \
                mock.patch.object(post, "_api", side_effect=[(200, {"id": 9}, ""),
                                                             (200, {"id": 9, "body": marker}, "")]):
            out, detail, _ = post.post(1, "h2", "review:1:h2", V_OK, True, wait_expired=True)
        self.assertEqual(out, "posted")
        self.assertEqual(detail, "COMMENT (CI not cleared after 60 min: E2E Tests pending)")

    def test_posted_and_read_back(self):
        marker = render.marker("review:1:h2")
        (out, detail, rid), _ = self.run_post([(200, {"id": 9}, ""), (200, {"id": 9, "body": "x " + marker}, "")])
        self.assertEqual((out, rid), ("posted", 9))
        self.assertTrue(detail.startswith("APPROVE"))

    def test_already_on_github_posts_nothing(self):
        (out, _, rid), api = self.run_post([], existing=5)
        self.assertEqual((out, rid), ("posted", 5))
        api.assert_not_called()

    def test_head_moved_posts_a_comment_on_the_reviewed_commit(self):
        marker = render.marker("review:1:h2")
        (out, detail, _), api = self.run_post([(200, {"id": 9}, ""), (200, {"id": 9, "body": marker}, "")],
                                              pr={**PR, "head": "h3"})
        self.assertEqual(out, "posted")
        self.assertTrue(detail.startswith("COMMENT"))
        payload = json.loads(Path(api.call_args_list[0].args[-1]).read_text())
        self.assertEqual((payload["event"], payload["commit_id"]), ("COMMENT", "h2"))
        self.assertIn("moved to `h3`", payload["body"])

    def test_5xx_and_network_retry_422_sticks(self):
        self.assertEqual(self.run_post([(502, None, "HTTP 502")])[0][0], "retry")
        self.assertEqual(self.run_post([(0, None, "connection reset")])[0][0], "retry")
        self.assertEqual(self.run_post([(422, None, "HTTP 422 commit_id not part of PR")])[0][0], "stuck")

    def test_read_back_mismatch_retries(self):
        self.assertEqual(self.run_post([(200, {"id": 9}, ""), (404, None, "")])[0][0], "retry")


class Workers(unittest.TestCase):
    """Model jobs run on worker threads; the main thread only starts them and collects them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(f"{self.tmp.name}/s.db")
        self.addCleanup(self.store.db.close)
        self.futures = []

        def submit(fn, *args):
            f = mock.Mock()
            f.done.return_value = False
            self.futures.append(f)
            return f
        for p in (mock.patch.object(main, "POOL", mock.Mock(submit=submit)), mock.patch.object(main, "RUNNING", {}),
                  mock.patch.object(main, "MAX_PARALLEL", 2), mock.patch.object(main, "log")):
            p.start()
        self.addCleanup(mock.patch.stopall)

    def test_dispatch_starts_up_to_the_limit_and_never_twice(self):
        a1 = decide.Action(kind="review", pr=1, head="h1")
        self.assertEqual(main.dispatch(a1), "")
        self.assertEqual(main.dispatch(a1), "review running")
        self.assertEqual(main.dispatch(decide.Action(kind="rereview", pr=2, head="h2", base="h1")), "")
        self.assertIn("waits", main.dispatch(decide.Action(kind="review", pr=3, head="h3")))
        self.assertEqual(len(self.futures), 2)

    def test_harvest_frees_the_slot_and_records_a_drafted_fix(self):
        a = decide.Action(kind="fix_draft", pr=4, head="h4", review_id=77)
        main.dispatch(a)
        self.futures[0].done.return_value = True
        self.futures[0].result.return_value = True
        main.harvest(self.store)
        self.assertEqual(main.RUNNING, {})
        self.assertEqual(self.store.row(4)[0].last_fix_review_id, 77)

    def test_work_uses_its_own_store_and_reports_auth_breaks(self):
        with mock.patch.object(main, "DB", f"{self.tmp.name}/s.db"), \
                mock.patch.object(main, "run_review", side_effect=post.AuthBroken("HTTP 401")):
            main._work(decide.Action(kind="review", pr=5, head="h5"))
        self.assertIn("HTTP 401", self.store.get_meta("auth_broken"))


class RunRecords(unittest.TestCase):
    def test_a_run_keeps_its_files_in_one_folder(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(review, "RUNS", Path(tmp.name)):
            folder = review.keep_run("PR-1-abc1234", {"prompt.md": "p", "result.json": "{}"})
        self.assertEqual(sorted(x.name for x in folder.iterdir()), ["prompt.md", "result.json"])
        self.assertTrue(folder.name.startswith("PR-1-abc1234-"))


class Compare(unittest.TestCase):
    def test_compare_text_counts_and_lists_new_lines_first(self):
        old = [("a.py", "+x = 1"), ("a.py", "+y = 2")]
        new = [("a.py", "+x = 1"), ("a.py", "+y = 3")]
        text = review.compare_text(old, new)
        self.assertTrue(text.startswith("PATCH_CHANGED"))
        self.assertIn("only in the reviewed patch: 1, only in the new patch: 1", text)
        self.assertLess(text.index("> a.py: +y = 3"), text.index("< a.py: +y = 2"))
        self.assertTrue(review.compare_text(old, old[:1]).startswith("PATCH_SUBSET"))
        self.assertTrue(review.compare_text(old, old).startswith("PATCH_IDENTICAL"))

    def test_rereview_prompt_has_the_cheap_paths(self):
        text = " ".join(review.REREVIEW.split()) + " " + " ".join(review.BASE_MOVED.split())
        self.assertIn("under about 150 changed lines", text)
        self.assertIn("compare.txt", text)


class ReviewerBrief(unittest.TestCase):
    def test_brief_is_filled_by_code_from_the_inputs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        inputs = Path(tmp.name)
        (inputs / "pr.md").write_text("# Fix the thing\n\nAuthor: bob\n\nbody\n")
        (inputs / "input.diff").write_text("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n-a\n+b\n+c\n"
                                           "diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n+d\n")
        (inputs / "previous.md").write_text("earlier")
        brief = review.reviewer_brief(inputs, 7, "h" * 40, "b" * 40)
        self.assertIn('pull request #7 of example-org/example-repo, "Fix the thing" by bob', brief)
        self.assertIn("2 files, +3 -1", brief)
        self.assertIn(f"{inputs}/previous.md", brief)
        self.assertNotIn("conversation.md", brief)
        self.assertNotIn("{", brief)  # every placeholder filled
        self.assertNotIn("—", brief)

    def test_run_prompt_dispatches_the_brief_and_reads_no_skill_files(self):
        self.assertNotIn("SKILL.md", review.PROMPT)
        self.assertIn("reviewer-prompt.md", review.PROMPT)


class Ledger(unittest.TestCase):
    def test_a_call_records_its_reviewer_model(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(f"{tmp.name}/s.db")
        self.addCleanup(store.db.close)
        store.end_call(store.start_call(1, "review", "opus", "h", "r"), "verdict comment",
                       {"in_tokens": 1, "out_tokens": 2, "cache_read": 3, "cache_write": 4, "cost_usd": 0.5,
                        "reviewer": "sonnet"})
        self.assertEqual(store.db.execute("select reviewer from calls").fetchone()[0], "sonnet")
        self.assertEqual(store.reviewers_today(), {"sonnet": 1})


class MeasureContext(unittest.TestCase):
    def test_measure_reports_the_fixed_context_of_a_lean_run(self):
        out = json.dumps({"total_cost_usd": 0.01, "modelUsage": {"claude-haiku-5-5": {
            "inputTokens": 5, "outputTokens": 2, "cacheReadInputTokens": 9000, "cacheCreationInputTokens": 1000}}})
        with mock.patch.object(review.subprocess, "run", return_value=mock.Mock(stdout=out, returncode=0)) as run:
            got = review.measure_context()
        self.assertEqual(got["context_tokens"], 10005)
        args = run.call_args.args[0]
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("haiku", args)


class TokenFallback(unittest.TestCase):
    """A second plan's token takes over when the first hits its usage limit, until the limit resets."""

    def setUp(self):
        from prwatch import claude_auth
        self.auth = claude_auth
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (mock.patch.object(claude_auth, "LIMITS", Path(tmp.name) / "token_limits.json"),
                  mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok-1", "PRWATCH_SECOND_OAUTH_TOKEN": "tok-2"}),
                  mock.patch.object(claude_auth, "_oauth", return_value={})):
            p.start()
        self.addCleanup(mock.patch.stopall)

    def test_primary_first_then_second_while_primary_is_limited(self):
        self.assertEqual(self.auth.pick(), ("primary", "tok-1"))
        self.auth.mark_limited("primary", datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc))
        self.assertEqual(self.auth.pick(), ("second", "tok-2"))
        self.assertIn("second token", self.auth.state()[1])
        self.auth.mark_limited("second", datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc))
        self.assertEqual(self.auth.pick(), (None, None))
        self.assertFalse(self.auth.state()[0])

    def test_a_past_limit_is_forgotten(self):
        self.auth.mark_limited("primary", datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))
        self.assertEqual(self.auth.pick()[0], "primary")

    def test_limit_reset_reads_the_epoch_or_backs_off(self):
        at = self.auth.limit_reset("Claude AI usage limit reached|1900000000")
        self.assertEqual(int(at.timestamp()), 1900000000)
        later = self.auth.limit_reset("Too many requests")
        self.assertGreater(later, datetime.datetime.now(datetime.timezone.utc))

    def run_with(self, results):
        outs = [mock.Mock(stdout=o, stderr="", returncode=rc) for o, rc in results]
        with mock.patch.object(review.subprocess, "run", side_effect=outs) as run:
            p, name = review.run_claude(["claude", "-p", "x"], cwd=".", timeout=10)
        return p, name, run

    def test_a_limited_run_is_retried_on_the_second_token(self):
        limited = json.dumps({"is_error": True, "subtype": "error", "result": "Claude AI usage limit reached|1900000000"})
        ok = json.dumps({"is_error": False, "subtype": "success", "result": "done"})
        p, name, run = self.run_with([(limited, 1), (ok, 0)])
        self.assertEqual((name, json.loads(p.stdout)["result"]), ("second", "done"))
        self.assertEqual([c.kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] for c in run.call_args_list], ["tok-1", "tok-2"])
        self.assertEqual(self.auth.pick()[0], "second")  # primary stays limited until its reset

    def test_a_successful_run_mentioning_rate_limits_is_not_a_limit(self):
        ok = json.dumps({"is_error": False, "subtype": "success", "result": "fix the rate limit handling"})
        p, name, run = self.run_with([(ok, 0)])
        self.assertEqual((name, run.call_count), ("primary", 1))
        self.assertEqual(self.auth.pick()[0], "primary")

    def test_an_ordinary_failure_is_not_a_limit(self):
        p, name, run = self.run_with([(json.dumps({"is_error": True, "subtype": "error_max_turns", "result": "x"}), 1)])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.auth.pick()[0], "primary")


class GitLock(unittest.TestCase):
    def test_fetch_and_worktree_hold_the_lock_reads_do_not(self):
        seen = {}

        def fake_run(args, **kw):
            seen[args[1]] = review.GIT_LOCK.locked()
            return mock.Mock(stdout="", returncode=0)
        with mock.patch.object(review.subprocess, "run", side_effect=fake_run):
            review.git("fetch", "origin")
            review.git("worktree", "add", "x", "h")
            review.git("diff", "a", "b")
        self.assertEqual(seen, {"fetch": True, "worktree": True, "diff": False})


class HandReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(f"{self.tmp.name}/s.db")
        self.addCleanup(self.store.db.close)
        mock.patch.object(main, "log").start()
        mock.patch.object(main.fetch, "gh", return_value=json.dumps({"headRefOid": "h9"})).start()
        self.addCleanup(mock.patch.stopall)

    def test_test_mode_prints_the_body_and_posts_nothing(self):
        with mock.patch.object(review, "run", return_value=(V_OK, "", "b1", None)) as run, \
                mock.patch.object(post, "post") as p, mock.patch("builtins.print") as out:
            main.hand_review(self.store, 9, test=True, reviewer_model="sonnet")
        self.assertEqual(run.call_args.kwargs.get("reviewer"), "sonnet")
        p.assert_not_called()
        self.assertIsNone(self.store.job("review:9:h9"))
        self.assertIn("Automated review (Claude Opus), reviewed at h9.", "".join(str(c) for c in out.call_args_list))


class ApproveLater(unittest.TestCase):
    """A clean verdict posted as a COMMENT because CI had not cleared gets its approval once CI clears."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(f"{self.tmp.name}/s.db")
        self.addCleanup(self.store.db.close)
        self.store.upsert_job("review:1:h2", pr=1, head="h2", kind="review", state="posted",
                              verdict_json=json.dumps(V_OK), posted_event="COMMENT",
                              withheld="CI quality-gate skipped")
        mock.patch.object(main, "log").start()
        mock.patch.object(main, "ALLOW_APPROVE", True).start()
        self.addCleanup(mock.patch.stopall)

    def run_upgrade(self, pr, api_results=()):
        with mock.patch.object(post, "pr_facts", return_value=pr), \
                mock.patch.object(post, "_api", side_effect=list(api_results)) as api:
            main.approve_later(self.store)
        return api

    def test_ci_cleared_posts_a_short_approval_once(self):
        marker = render.marker("review:1:h2:approve")
        api = self.run_upgrade({**PR, "ci": ("cleared", "")},
                               [(200, {"id": 7}, ""), (200, {"id": 7, "body": marker}, "")])
        payload = json.loads(Path(api.call_args_list[0].args[-1]).read_text())
        self.assertEqual((payload["event"], payload["commit_id"]), ("APPROVE", "h2"))
        self.assertIn("CI has cleared on `h2`", payload["body"])
        self.assertIn("Automated review (Claude Opus), reviewed at h2.", payload["body"])
        job = self.store.job("review:1:h2")
        self.assertEqual((job["posted_event"], job["withheld"]), ("APPROVE", ""))
        self.run_upgrade({**PR, "ci": ("cleared", "")}).assert_not_called()  # done: never twice

    def test_ci_still_running_waits(self):
        self.run_upgrade({**PR, "ci": ("pending", "E2E pending")}).assert_not_called()
        self.assertEqual(self.store.job("review:1:h2")["withheld"], "CI quality-gate skipped")

    def test_head_moved_or_other_gate_gives_up(self):
        self.run_upgrade({**PR, "head": "h3", "ci": ("cleared", "")}).assert_not_called()
        self.assertEqual(self.store.job("review:1:h2")["withheld"], "")
        self.store.upsert_job("review:1:h2", withheld="CI x")
        self.run_upgrade({**PR, "labels": ["hold"], "ci": ("cleared", "")}).assert_not_called()
        self.assertEqual(self.store.job("review:1:h2")["withheld"], "")

    def test_critical_verdict_is_never_upgraded(self):
        bad = {**V_OK, "findings": [{"severity": "critical", "title": "t", "detail": "d"}]}
        self.store.upsert_job("review:1:h2", verdict_json=json.dumps(bad))
        self.run_upgrade({**PR, "ci": ("cleared", "")}).assert_not_called()


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

    def test_posted_records_its_event_and_why(self):
        self.drain(("posted", "COMMENT (CI Lint failure)", 5))
        job = self.store.job("review:1:h2")
        self.assertEqual((job["posted_event"], job["withheld"]), ("COMMENT", "CI Lint failure"))

    def test_a_long_ci_wait_ends_in_a_comment(self):
        self.store.upsert_job("review:1:h2", verdict_at="2026-01-01T00:00:00Z")
        p = self.drain(("wait", "approval waits: CI E2E pending", None))
        self.assertTrue(p.call_args.kwargs.get("wait_expired"))
        self.store.upsert_job("review:1:h2", verdict_at="2999-01-01T00:00:00Z", next_try_at="")
        self.assertFalse(self.drain(("wait", "x", None)).call_args.kwargs.get("wait_expired"))

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
