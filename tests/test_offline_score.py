"""Offline scorer tests. Feature 14.

Every case here is a bug that the calibration pass against the 49 LLM-scored
rows actually caught, or a hard rule from the rubric that must not drift. The
rank correlation itself is not asserted - that lives in
`scripts/calibrate_score.py`, because a threshold on it would either be too
loose to catch anything or too tight to survive a weight change.

Run: python -m unittest discover -s tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpipe import offline_score as o  # noqa: E402


def job(title="Software Engineer", desc="", **kw):
    base = {"job_id": "a" * 20, "company": "Acme", "title": title,
            "location": "New York, NY", "description_text": desc,
            "remote_flag": 0, "repost_count": 0}
    base.update(kw)
    return base


# A body long enough to clear the 200-char "thin" threshold, with no signals
# of its own, so a test can isolate the one phrase it cares about.
FILLER = ("We are building things and we would like you to help us build them. "
          "The team is distributed and collaborative and we care about quality. "
          "Benefits are competitive and the work is interesting. ") * 2


class YearsExtraction(unittest.TestCase):
    def test_reads_years_without_the_word_experience(self):
        # filters._YEARS_RE needs "experience" within three tokens and misses
        # both of these. They are why two eight-year reqs sit in the shortlist.
        self.assertEqual(o.max_years_required("Requires 8+ years in systems security"), 8)
        self.assertEqual(
            o.max_years_required("Have owned a program with 8+ years managing fleets"), 8)

    def test_ignores_company_history(self):
        self.assertIsNone(
            o.max_years_required("For over 30 years, Acme has served customers."))
        self.assertIsNone(o.max_years_required("Founded 40 years ago in a garage."))

    def test_ignores_a_four_year_degree(self):
        self.assertIsNone(
            o.max_years_required("A 4-year degree in a technical field is required."))

    def test_takes_the_largest_requirement(self):
        self.assertEqual(
            o.max_years_required("2 years of experience required, 5 years preferred "
                                 "for the senior track, minimum qualifications apply"), 5)


class DegreeDetection(unittest.TestCase):
    def test_a_degree_list_is_not_a_phd_wall(self):
        # "BS/MS/PhD" requires a bachelor's. Reading it as a doctorate wall took
        # a req the LLM scored 58 down to 15 in the first calibration pass.
        self.assertIsNone(o.degree_required("BS/MS/PhD in Computer Science or related"))
        self.assertIsNone(
            o.degree_required("Bachelor's degree required; PhD a plus"))

    def test_a_real_phd_requirement_is_caught(self):
        self.assertEqual(
            o.degree_required("PhD in Machine Learning is required for this role"), "phd")

    def test_phd_preferred_is_not_a_requirement(self):
        self.assertIsNone(o.degree_required("PhD preferred but not required"))


class AggregatorMetadata(unittest.TestCase):
    LINE = "category: AI/ML/Data | sponsorship: Other | degrees: Bachelor's, PhD"

    def test_parses_the_metadata_line(self):
        meta = o.parse_aggregator_meta(self.LINE)
        self.assertEqual(meta["category"], "AI/ML/Data")
        self.assertEqual(meta["degrees"], "Bachelor's, PhD")

    def test_a_bachelors_in_the_list_opens_the_door(self):
        self.assertIsNone(o.degree_floor(o.parse_aggregator_meta(self.LINE)))

    def test_phd_alone_is_a_floor(self):
        meta = o.parse_aggregator_meta("category: Software | degrees: PhD")
        self.assertEqual(o.degree_floor(meta), "phd")

    def test_no_degrees_field_is_not_a_floor(self):
        self.assertIsNone(o.degree_floor(o.parse_aggregator_meta("category: Software")))


class SubstringCollisions(unittest.TestCase):
    """The recurring bug class in this project: ca/canada, unit/United States,
    graduate/Undergraduate, city/authorized-to-work-in-the-city."""

    def test_data_warehouse_is_not_a_warehouse(self):
        result = o.specialist_domain("Data Analyst I", "Build our data warehouse in Snowflake")
        self.assertIsNone(result)

    def test_a_real_warehouse_still_scores(self):
        result = o.specialist_domain("Data Analyst", "Night shift at the distribution center")
        self.assertIsNotNone(result)

    def test_mentorship_from_senior_engineers_is_a_perk_not_a_requirement(self):
        # This took a posting the LLM scored 76 down to 39.
        payload = o.score_job(job(desc=FILLER + "You will get mentorship from our "
                                                "Co-Founder and senior engineers."))
        labels = [s["label"] for s in payload["_signals"]]
        self.assertNotIn("the body describes a senior role under a junior title", labels)

    def test_a_senior_role_title_in_the_body_still_counts(self):
        payload = o.score_job(job(desc=FILLER + "We are hiring a Senior Software Core "
                                                "Data engineer for this team."))
        labels = [s["label"] for s in payload["_signals"]]
        self.assertIn("the body describes a senior role under a junior title", labels)


class HardRules(unittest.TestCase):
    """Carried over from the LLM rubric. These are the ones that must not drift."""

    def test_clearance_caps_under_twenty(self):
        payload = o.score_job(job(
            title="Software Engineer New Grad",
            desc=FILLER + "An active TS/SCI security clearance is required."))
        self.assertLess(payload["fit_score"], 20)
        self.assertIn("security clearance required", payload["red_flags"])

    def test_clearance_beats_every_positive_signal(self):
        payload = o.score_job(job(
            title="Software Engineer I New Grad 2027",
            desc=FILLER + "New grad role, 0-2 years, Python and SQL and React. "
                          "Top Secret clearance required.",
            location="Baltimore, MD"))
        self.assertLess(payload["fit_score"], 20)

    def test_a_thin_row_is_capped_at_sixty(self):
        payload = o.score_job(job(title="Software Engineer New Grad 2027",
                                  desc="category: Software | degrees: Bachelor's"))
        self.assertLessEqual(payload["fit_score"], 60)
        self.assertIn("no description text", payload["rationale"].lower())

    def test_evidence_ids_are_always_real(self):
        from jobpipe.config import evidence
        real = {e["id"] for e in evidence()}
        payload = o.score_job(job(desc=FILLER + "Python, SQL and data pipelines."))
        for eid in payload["evidence_ids"]:
            self.assertIn(eid, real)

    def test_known_gaps_are_named_and_cost_points(self):
        payload = o.score_job(job(desc=FILLER + "You will write Kotlin and run Kubernetes."))
        joined = " ".join(payload["gaps"])
        self.assertIn("Kotlin", joined)
        self.assertIn("Kubernetes", joined)
        self.assertTrue(any(s["delta"] < 0 for s in payload["_signals"]
                            if s["label"].startswith("wants ")))

    def test_referral_path_is_never_guessed(self):
        self.assertEqual(o.referral_path("Acme", "A generic posting"), "none")
        self.assertEqual(o.referral_path("NIST", "Standards work"), "NIST")


class PayloadShape(unittest.TestCase):
    def test_carries_every_field_the_llm_payload_had(self):
        payload = o.score_job(job(desc=FILLER))
        for key in ("fit_score", "rationale", "resume_variant", "evidence_ids",
                    "gaps", "red_flags", "effort", "referral_path"):
            self.assertIn(key, payload)

    def test_score_stays_in_range(self):
        for j in (job(title="Senior Principal Staff Architect III",
                      desc=FILLER + "15 years of experience required. PhD required."),
                  job(title="Software Engineer I New Grad 2027",
                      desc=FILLER + "New grad, 0-2 years, Python SQL React TypeScript.")):
            score = o.score_job(j)["fit_score"]
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)

    def test_variant_is_one_of_the_four(self):
        payload = o.score_job(job(desc=FILLER + "Snowflake, SQL and dashboards."))
        self.assertIn(payload["resume_variant"],
                      ("systems", "data", "fullstack", "ai-tooling"))

    def test_rationale_names_what_moved_the_score(self):
        payload = o.score_job(job(title="Software Engineer New Grad",
                                  desc=FILLER + "PhD in Computer Science required."))
        self.assertIn("phd", payload["rationale"].lower())

    def test_scoring_is_deterministic(self):
        j = job(desc=FILLER + "Python and SQL, 2 years of experience.")
        self.assertEqual(o.score_job(j)["fit_score"], o.score_job(j)["fit_score"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StartupPollFreshness(unittest.TestCase):
    """Feature 15. The decision to poll, not the poll itself."""

    def setUp(self):
        import tempfile
        from jobpipe import db
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.db"
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def _run_at(self, iso):
        self.conn.execute(
            "INSERT INTO source_runs (source_key, ats_vendor, company_slug, "
            "ok, run_at) VALUES (?, 'greenhouse', 'acme', 1, ?)",
            ("greenhouse:acme", iso))
        self.conn.commit()

    def test_an_empty_database_polls(self):
        from jobpipe import web
        self.assertIsNone(web._minutes_since_last_poll(self.path))

    def test_a_recent_poll_is_measured_in_minutes(self):
        from datetime import datetime, timedelta, timezone
        from jobpipe import web
        self._run_at((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
        age = web._minutes_since_last_poll(self.path)
        self.assertIsNotNone(age)
        self.assertLess(age, web.STARTUP_POLL_MAX_AGE_SECONDS / 60)

    def test_a_stale_poll_is_past_the_threshold(self):
        from datetime import datetime, timedelta, timezone
        from jobpipe import web
        # The state this feature was written for: 44 hours since the last run.
        self._run_at((datetime.now(timezone.utc) - timedelta(hours=44)).isoformat())
        age = web._minutes_since_last_poll(self.path)
        self.assertGreater(age, web.STARTUP_POLL_MAX_AGE_SECONDS / 60)

    def test_a_naive_timestamp_is_read_as_utc(self):
        from jobpipe import web
        self._run_at("2020-01-01T00:00:00")
        self.assertGreater(web._minutes_since_last_poll(self.path), 1000)
