import os

# Before importing decide: identity and policy are read at import time.
os.environ.update(PRWATCH_ME="me,me-alt", PRWATCH_REVIEWER="reviewer-bot",
                  PRWATCH_REQUIRED_CHECK="scan-verdict", PRWATCH_REPO="example-org/example-repo",
                  PRWATCH_SETTLE_MIN="30", PRWATCH_SKIP_TITLE="do not review", PRWATCH_WATCHED_AUTHORS="bob",
                  PRWATCH_MAX_CHANGE_ROUNDS="2")

import unittest
from unittest import mock

from prwatch import decide as d
GREEN = [
    {"name": "quality-gate / Code quality, dependencies and secrets", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"name": "quality-gate", "status": "COMPLETED", "conclusion": "SKIPPED"},
    {"name": "Lint & Test", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"name": "scan-verdict / open the scan report", "status": "COMPLETED", "conclusion": "SUCCESS"},
]


def snap(**kw):
    base = dict(number=1, title="t", author="alice", draft=False, head="h2", base="master",
                review_requests=set(), reviews=[], checks=list(GREEN))
    base.update(kw)
    return d.Snapshot(**base)


def review(author, state, commit, rid=1, at="2026-10-26T10:00:00Z"):
    return {"id": rid, "author": author, "state": state, "commit": commit, "submitted": at}


class CiCleared(unittest.TestCase):
    def test_green_with_shadowed_skip(self):
        self.assertEqual(d.ci_cleared(GREEN), (True, "cleared"))

    def test_bare_skip_blocks(self):
        checks = GREEN + [{"name": "E2E Tests", "status": "COMPLETED", "conclusion": "SKIPPED"}]
        self.assertFalse(d.ci_cleared(checks)[0])

    def test_pending_blocks(self):
        checks = GREEN + [{"name": "E2E Tests", "status": "IN_PROGRESS", "conclusion": ""}]
        self.assertFalse(d.ci_cleared(checks)[0])

    def test_failure_blocks(self):
        checks = GREEN[:2] + [{"name": "Lint & Test", "status": "COMPLETED", "conclusion": "FAILURE"}] + GREEN[3:]
        self.assertFalse(d.ci_cleared(checks)[0])

    def test_system_verdict_required(self):
        self.assertEqual(d.ci_cleared(GREEN[:3]), (False, "scan-verdict verdict not posted"))

    def test_no_checks(self):
        self.assertFalse(d.ci_cleared([])[0])


class Asked(unittest.TestCase):
    def test_requested_and_green_reviews(self):
        role, acts, _ = d.decide(snap(review_requests={"me"}), d.Row())
        self.assertEqual(role, "asked")
        self.assertEqual([a.kind for a in acts], ["review"])

    def test_first_review_does_not_wait_for_ci(self):
        s = snap(review_requests={"me"}, checks=GREEN[:3])
        self.assertEqual([a.kind for a in d.decide(s, d.Row())[1]], ["review"])

    def test_rereview_waits_for_ci(self):
        s = snap(reviews=[review("me", "COMMENTED", "h1")], checks=GREEN[:3])
        _, acts, note = d.decide(s, d.Row(), files_since_last=["api/x.py"])
        self.assertEqual(acts, [])
        self.assertTrue(note.startswith("waiting for CI"))

    def test_ci_status_failed_wins_over_pending(self):
        pending = {"name": "E2E", "status": "IN_PROGRESS", "conclusion": ""}
        failed = {"name": "Lint", "status": "COMPLETED", "conclusion": "FAILURE"}
        self.assertEqual(d.ci_status([pending, failed])[0], "failed")
        self.assertEqual(d.ci_status([pending] + GREEN)[0], "pending")
        self.assertEqual(d.ci_status(GREEN)[0], "cleared")
        running = [{**GREEN[0], "status": "IN_PROGRESS", "conclusion": ""}] + GREEN[1:]
        self.assertEqual(d.ci_status(running)[0], "pending")  # a skip whose reusable job still runs
        self.assertEqual(d.ci_status(running[1:2] + [{**GREEN[0], "status": "QUEUED", "conclusion": ""}])[0],
                         "pending")

    def test_change_round_cap_comments_once_then_stays_quiet(self):
        s = snap(reviews=[review("me", "CHANGES_REQUESTED", "h0", rid=1, at="2026-10-26T09:00:00Z"),
                          review("me", "CHANGES_REQUESTED", "h1", rid=2)])
        _, acts, note = d.decide(s, d.Row(), files_since_last=["api/x.py"], head_age_min=60)
        self.assertEqual([a.kind for a in acts], ["cap_comment"])
        self.assertIn("2 change requests", note)
        self.assertEqual(d.decide(s, d.Row(cap_commented=1), files_since_last=["api/x.py"], head_age_min=60)[1], [])
        s.review_requests = {"me"}  # a re-request still gets a re-review
        self.assertEqual([a.kind for a in d.decide(s, d.Row(cap_commented=1), files_since_last=["api/x.py"])[1]],
                         ["rereview"])

    def test_same_head_no_call(self):
        s = snap(review_requests={"me"})
        self.assertEqual(d.decide(s, d.Row(last_reviewed_head="h2"))[1], [])

    def test_github_review_on_head_is_done_even_if_row_is_stale(self):
        s = snap(review_requests={"me-alt"}, reviews=[review("me", "COMMENTED", "h2")])
        self.assertEqual(d.decide(s, d.Row(last_reviewed_head="h1"))[1], [])

    def test_correction_push_rereviews_from_last_head(self):
        s = snap(reviews=[review("me", "COMMENTED", "h1")])
        _, acts, _ = d.decide(s, d.Row(), files_since_last=["api/x.py"])
        self.assertEqual([(a.kind, a.base) for a in acts], [("rereview", "h1")])

    def test_docs_only_push_skips_model(self):
        s = snap(reviews=[review("me", "COMMENTED", "h1")])
        _, acts, note = d.decide(s, d.Row(), files_since_last=["docs/a.md", "README.md"])
        self.assertEqual([a.kind for a in acts], ["skip"])
        self.assertIn("docs-only", note)

    def test_skipped_head_stays_quiet(self):
        s = snap(reviews=[review("me", "COMMENTED", "h1")])
        self.assertEqual(d.decide(s, d.Row(skipped_head="h2"), files_since_last=["a.md"])[1], [])

    def test_unknown_files_counts_as_change(self):
        s = snap(reviews=[review("me", "COMMENTED", "h1")])
        self.assertEqual([a.kind for a in d.decide(s, d.Row(), files_since_last=None)[1]], ["rereview"])

    def test_rereview_waits_for_the_head_to_settle(self):
        s = snap(reviews=[review("me", "COMMENTED", "h1")])
        _, acts, note = d.decide(s, d.Row(), files_since_last=["api/x.py"], head_age_min=5)
        self.assertEqual(acts, [])
        self.assertIn("settling", note)
        self.assertEqual([a.kind for a in d.decide(s, d.Row(), files_since_last=["api/x.py"],
                                                    head_age_min=31)[1]], ["rereview"])

    def test_rerequest_skips_the_settle_wait(self):
        s = snap(review_requests={"me"}, reviews=[review("me", "COMMENTED", "h1")])
        self.assertEqual([a.kind for a in d.decide(s, d.Row(), files_since_last=["api/x.py"],
                                                    head_age_min=1)[1]], ["rereview"])

    def test_skip_keyword_in_title_ignores_the_pr(self):
        s = snap(title="Fix thing (Do Not Review)", review_requests={"me"})
        _, acts, note = d.decide(s, d.Row())
        self.assertEqual(acts, [])
        self.assertIn("skip keyword", note)

    def test_pin_counts_as_request(self):
        self.assertEqual([a.kind for a in d.decide(snap(), d.Row(), pins=frozenset({1}))[1]], ["review"])

    def test_other_does_nothing(self):
        self.assertEqual(d.decide(snap(), d.Row())[:2], ("other", []))

    def test_watched_author_gets_a_first_review_unasked(self):
        role, acts, _ = d.decide(snap(author="bob"), d.Row())
        self.assertEqual((role, [a.kind for a in acts]), ("asked", ["review"]))

    def test_watched_author_rereviewed_after_corrections(self):
        s = snap(author="bob", reviews=[review("me", "CHANGES_REQUESTED", "h1")])
        self.assertEqual([a.kind for a in d.decide(s, d.Row(), files_since_last=["api/x.py"])[1]], ["rereview"])

    def test_draft_reviewed_only_when_my_review_is_requested(self):
        self.assertEqual([a.kind for a in d.decide(snap(draft=True, review_requests={"me-alt"}), d.Row())[1]],
                         ["review"])
        self.assertEqual(d.decide(snap(draft=True, author="bob"), d.Row())[1], [])
        with mock.patch.object(d, "REVIEW_DRAFTS", True):
            self.assertEqual([a.kind for a in d.decide(snap(draft=True, author="bob"), d.Row())[1]], ["review"])

    def test_my_own_draft_gets_nothing(self):
        self.assertEqual(d.decide(snap(draft=True, author="me"), d.Row())[1:], ([], "draft"))


class Mine(unittest.TestCase):
    def test_requests_reviewer_once_per_head(self):
        s = snap(author="me")
        self.assertEqual([a.kind for a in d.decide(s, d.Row())[1]], ["request_reviewer"])
        self.assertEqual(d.decide(s, d.Row(last_rerequest_head="h2"))[1], [])

    def test_no_request_while_pending_or_reviewed_here(self):
        self.assertEqual(d.decide(snap(author="me", review_requests={"reviewer-bot"}), d.Row())[1], [])
        s = snap(author="me", reviews=[review("reviewer-bot", "APPROVED", "h2")])
        self.assertEqual(d.decide(s, d.Row())[1], [])

    def test_changes_requested_drafts_fix_once(self):
        s = snap(author="me", review_requests={"reviewer-bot"},
                 reviews=[review("reviewer-bot", "CHANGES_REQUESTED", "h2", rid=7)])
        acts = d.decide(s, d.Row())[1]
        self.assertEqual([(a.kind, a.review_id) for a in acts], [("fix_draft", 7)])
        self.assertEqual(d.decide(s, d.Row(last_fix_review_id=7))[1], [])

    def test_later_approval_supersedes_changes_requested(self):
        s = snap(author="me", review_requests={"reviewer-bot"}, reviews=[
            review("reviewer-bot", "CHANGES_REQUESTED", "h1", rid=7, at="2026-10-26T09:00:00Z"),
            review("reviewer-bot", "APPROVED", "h2", rid=8, at="2026-10-26T10:00:00Z")])
        self.assertEqual(d.decide(s, d.Row())[1], [])


class Caps(unittest.TestCase):
    def test_caps(self):
        a = d.Action("review", 1, "h")
        self.assertTrue(d.within_caps(a, 5, 1, 6, 2)[0])
        self.assertFalse(d.within_caps(a, 6, 0, 6, 2)[0])
        self.assertFalse(d.within_caps(a, 0, 2, 6, 2)[0])
        self.assertTrue(d.within_caps(a, 99, 99, 0, 0)[0])  # 0 = no cap
        self.assertTrue(d.within_caps(d.Action("request_reviewer", 1, "h"), 99, 99, 6, 2)[0])


if __name__ == "__main__":
    unittest.main()
