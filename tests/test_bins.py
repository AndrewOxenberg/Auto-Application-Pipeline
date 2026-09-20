"""Bin filtering. Features 17 and 19.

These run against a temporary database rather than the live one, so nothing
here depends on what has actually been triaged today.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpipe import db, web  # noqa: E402


class Bins(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.dir.name) / "t.db")
        self.api = web.Api(self.conn)
        self._add("p1", "pass", None, "discovered")
        self._add("p2", "pass", None, "shortlisted")
        self._add("p3", "pass", None, "submitted")
        self._add("p4", "pass", None, "dismissed")
        self._add("r1", "reject", "title-deny:manager", "discovered")
        self._add("r2", "reject", "location:berlin", "discovered")
        self._add("r3", "reject", "something-new:unmapped", "discovered")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def _add(self, job_id, verdict, reason, status):
        self.conn.execute(
            "INSERT INTO jobs (job_id, company, title, url, ats_vendor, "
            "company_slug, is_open, filter_verdict, filter_reason, status, "
            "first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,1,?,?,?,datetime('now'),datetime('now'))",
            (job_id, "Acme", "Software Engineer", f"https://x/{job_id}",
             "greenhouse", "acme", verdict, reason, status))

    def ids(self, **kw):
        return {j["job_id"] for j in self.api.jobs(
            {k: [v] for k, v in kw.items()})["jobs"]}

    # -- feature 19 --------------------------------------------------------

    def test_pass_excludes_applied_and_dismissed(self):
        self.assertEqual(self.ids(bin="pass"), {"p1", "p2"})

    def test_shortlisted_stays_in_pass(self):
        # Shortlisting is a note to self, not a decision that closes the job.
        self.assertIn("p2", self.ids(bin="pass"))

    def test_applied_tab_shows_only_applied(self):
        self.assertEqual(self.ids(bin="applied"), {"p3"})

    def test_passed_tab_shows_only_dismissed(self):
        self.assertEqual(self.ids(bin="passed"), {"p4"})

    def test_all_still_shows_everything(self):
        self.assertEqual(len(self.ids(bin="all")), 7)

    def test_the_partition_is_exact(self):
        summary = self.api.summary()
        self.assertEqual(sum(b["count"] for b in summary["bins"]),
                         summary["open"])

    def test_yield_stays_on_the_tier1_number(self):
        # Applying to a job must not change what the filter achieved.
        summary = self.api.summary()
        self.assertEqual(summary["passed"], 4)
        pass_bin = next(b for b in summary["bins"] if b["key"] == "pass")
        self.assertEqual(pass_bin["count"], 2)

    def test_triage_bins_are_labelled_as_a_different_kind(self):
        kinds = {b["key"]: b["kind"] for b in self.api.summary()["bins"]}
        self.assertEqual(kinds["applied"], "triage")
        self.assertEqual(kinds["passed"], "triage")
        self.assertEqual(kinds["title"], "tier1")

    # -- the unbinned bug --------------------------------------------------

    def test_unbinned_returns_only_unmapped_rejects(self):
        # It used to return an empty WHERE clause, so clicking the chip showed
        # the entire database under a header that read as a filtered view.
        self.assertEqual(self.ids(bin="unbinned"), {"r3"})

    def test_unbinned_count_matches_what_the_filter_returns(self):
        summary = self.api.summary()
        unbinned = next((b for b in summary["bins"] if b["key"] == "unbinned"),
                        {"count": 0})
        self.assertEqual(unbinned["count"], len(self.ids(bin="unbinned")))

    def test_a_mapped_reject_is_not_unbinned(self):
        self.assertNotIn("r1", self.ids(bin="unbinned"))
        self.assertEqual(self.ids(bin="title"), {"r1"})

    # -- feature 17, still true --------------------------------------------

    def test_fresh_filters_on_time_not_seen_count(self):
        self.conn.execute("UPDATE jobs SET seen_count=9")
        self.conn.commit()
        # Every row was just inserted, so all of them are fresh regardless of
        # how many times they have been seen.
        self.assertEqual(len(self.ids(bin="all", fresh="1")), 7)

    def test_stale_rows_are_not_fresh(self):
        self.conn.execute("UPDATE jobs SET first_seen_at=datetime('now','-3 days')")
        self.conn.commit()
        self.assertEqual(self.ids(bin="all", fresh="1"), set())

    # -- feature 18 --------------------------------------------------------

    def test_sort_is_echoed_back(self):
        self.assertEqual(self.api.jobs({"sort": ["age"]})["sort"], "age")

    def test_an_unknown_sort_falls_back_to_the_default(self):
        self.assertEqual(self.api.jobs({"sort": ["'; DROP TABLE jobs--"]})["sort"],
                         web.DEFAULT_SORT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
