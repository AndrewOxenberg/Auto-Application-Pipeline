"""Offline tests. No network — payload fixtures are trimmed copies of real
responses captured on 2026-08-25.

Run: python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpipe import db, dedup, ingest  # noqa: E402
from jobpipe.filters import Tier1  # noqa: E402
from jobpipe.sources import ashby, greenhouse, lever, simplify  # noqa: E402
from jobpipe.util import (age_text, dedup_key, iso_normalize, job_id,  # noqa: E402
                          norm_location, norm_title, strip_html)

GH_PAYLOAD = {"jobs": [
    {"id": 8130725, "requisition_id": "SHARED-REQ", "title": "Software Engineer, New Grad",
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/8130725",
     "location": {"name": "New York, NY"}, "company_name": "Acme",
     "departments": [{"name": "Engineering"}],
     "first_published": "2026-08-19T14:02:07-04:00",
     "updated_at": "2026-08-20T10:00:00-04:00",
     "content": "&lt;p&gt;Build things.&lt;/p&gt;"},
    # Same requisition_id, different post: these must not collapse.
    {"id": 8130726, "requisition_id": "SHARED-REQ", "title": "Software Engineer, New Grad",
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/8130726",
     "location": {"name": "Seattle, WA"}, "company_name": "Acme",
     "departments": [{"name": "Engineering"}],
     "first_published": "2026-08-19T14:02:07-04:00", "content": "<p>Build things.</p>"},
]}

LEVER_PAYLOAD = [
    {"id": "ac978161", "text": "Backend Engineer",
     "hostedUrl": "https://jobs.lever.co/acme/ac978161",
     "applyUrl": "https://jobs.lever.co/acme/ac978161/apply",
     "createdAt": 1711403416463, "workplaceType": "remote",
     "categories": {"commitment": "Full-time", "team": "Platform",
                    "location": "New York", "allLocations": ["New York", "Remote"]},
     "descriptionPlain": "We need someone.", "additionalPlain": "Benefits."},
]

ASHBY_PAYLOAD = {"jobs": [
    {"id": "34413f8d", "title": "Security Engineer", "department": "Engineering",
     "team": "Backend", "employmentType": "FullTime", "location": "New York, NY (HQ)",
     "secondaryLocations": [{"location": "Remote (Canada)"}], "isRemote": True,
     "isListed": True, "publishedAt": "2026-04-07T17:12:35.753+00:00",
     "jobUrl": "https://jobs.ashbyhq.com/acme/34413f8d",
     "applyUrl": "https://jobs.ashbyhq.com/acme/34413f8d/app",
     "descriptionPlain": "Keep things safe."},
    {"id": "unlisted-1", "title": "Hidden Role", "isListed": False,
     "location": "NY", "descriptionPlain": "x"},
]}

SIMPLIFY_PAYLOAD = [
    {"id": "20fe605e", "source": "Simplify", "category": "Software",
     "company_name": "Mechanize", "title": "Software Engineer", "active": True,
     "is_visible": True, "date_posted": 1767841111, "date_updated": 1767841111,
     "url": "https://jobs.ashbyhq.com/mechanize/1ef28bb2", "locations": ["SF"],
     "sponsorship": "Other", "degrees": []},
    {"id": "inactive-1", "company_name": "Ghost Co", "title": "SWE", "active": False,
     "is_visible": True, "url": "https://jobs.lever.co/ghost/1", "locations": ["NY"]},
]


class TestUtil(unittest.TestCase):
    def test_norm_location_handles_initialisms(self):
        self.assertEqual(norm_location("Washington, D.C., USA"), "dc")
        self.assertEqual(norm_location("New York City, NY"), "new york ny")
        self.assertEqual(norm_location("Remote - US"), "remote")
        self.assertEqual(norm_location(""), "")

    def test_norm_title_drops_levels_and_ids(self):
        self.assertEqual(norm_title("Software Engineer II (New Grad) #12345"),
                         "software engineer")

    def test_iso_normalize_accepts_all_three_shapes(self):
        self.assertTrue(iso_normalize(1711403416463).startswith("2024-03-25"))
        self.assertTrue(iso_normalize("1767841111").startswith("2026-01-08"))
        self.assertEqual(iso_normalize("2026-04-07T17:12:35.753+00:00"),
                         "2026-04-07T17:12:35+00:00")
        self.assertIsNone(iso_normalize(None))
        self.assertIsNone(iso_normalize("not a date"))

    def test_strip_html_unescapes_twice(self):
        self.assertEqual(strip_html("&lt;p&gt;Hi&nbsp;there&lt;/p&gt;"), "Hi there")

    def test_age_text_uses_hours_under_a_day(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        iso = lambda delta: (now - delta).isoformat()
        self.assertEqual(age_text(iso(timedelta(minutes=20))), "<1h")
        self.assertEqual(age_text(iso(timedelta(hours=2, minutes=5))), "2h")
        self.assertEqual(age_text(iso(timedelta(hours=22))), "22h")
        self.assertEqual(age_text(iso(timedelta(hours=25))), "1d")
        self.assertEqual(age_text(iso(timedelta(days=9, hours=3))), "9d")
        self.assertEqual(age_text(None), "?")
        self.assertEqual(age_text("not a date"), "?")
        # A source whose clock runs ahead of ours must not print "-1d".
        self.assertEqual(age_text(iso(timedelta(hours=-3))), "0h")

    def test_job_id_is_stable_and_distinct(self):
        self.assertEqual(job_id("greenhouse", "acme", "1"),
                         job_id("greenhouse", "acme", "1"))
        self.assertNotEqual(job_id("greenhouse", "acme", "1"),
                            job_id("greenhouse", "acme", "2"))


class TestParsers(unittest.TestCase):
    def test_greenhouse_does_not_collapse_shared_requisition_ids(self):
        jobs = greenhouse.parse(GH_PAYLOAD, "acme")
        self.assertEqual(len(jobs), 2)
        self.assertNotEqual(jobs[0]["job_id"], jobs[1]["job_id"])
        self.assertEqual(jobs[0]["description_text"], "Build things.")
        self.assertEqual(jobs[0]["location"], "New York, NY")
        self.assertTrue(jobs[0]["posted_at"].startswith("2026-08-19"))

    def test_lever_epoch_and_remote_flag(self):
        jobs = lever.parse(LEVER_PAYLOAD, "acme")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["remote_flag"], 1)
        self.assertEqual(jobs[0]["employment_type"], "Full-time")
        self.assertIn("Benefits.", jobs[0]["description_text"])
        self.assertTrue(jobs[0]["posted_at"].startswith("2024-03-25"))

    def test_lever_empty_array_is_not_a_crash(self):
        self.assertEqual(lever.parse([], "plaid"), [])

    def test_ashby_skips_unlisted(self):
        jobs = ashby.parse(ASHBY_PAYLOAD, "acme")
        self.assertEqual([j["title"] for j in jobs], ["Security Engineer"])
        self.assertIn("Remote (Canada)", jobs[0]["location"])

    def test_simplify_skips_inactive_and_detects_ats(self):
        jobs = simplify.parse(SIMPLIFY_PAYLOAD, "newgrad")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["department"], "ashby")

    def test_detect_ats(self):
        cases = {
            "https://job-boards.greenhouse.io/databricks/jobs/1": ("greenhouse", "databricks"),
            "https://jobs.lever.co/palantir/abc": ("lever", "palantir"),
            "https://jobs.ashbyhq.com/mechanize/1ef": ("ashby", "mechanize"),
            "https://westernunion.wd5.myworkdayjobs.com/en-US/WU/job/x": ("workday", "westernunion"),
            "https://example.com/careers": None,
        }
        for url, expected in cases.items():
            self.assertEqual(simplify.detect_ats(url), expected, url)


class TestTier1(unittest.TestCase):
    def setUp(self):
        self.t1 = Tier1({
            "title": {"allow": [r"\bsoftware\b", r"\bengineer\b"],
                      "deny": [r"\bsenior\b", r"\bmechanical\b"]},
            "employment": {"exclude_internships": True,
                           "internship_title_patterns": [r"\bintern\b", r"\bco.?op\b",
                                                         r"\bsummer analyst\b"],
                           "internship_employment_types": ["internship", "temporary"]},
            "references": {"exclude_reference_required": True, "patterns": [
                r"\b(?:three|two|3|2)\s+(?:professional\s+)?references\b",
                r"\breferences?\s+(?:are\s+|will\s+be\s+)?required\b",
                r"\bprovide\s+(?:\w+\s+){0,4}references\b",
                r"\bletters?\s+of\s+recommendation\b",
            ]},
            "experience": {"max_years_required": 2},
            "grad_window": {"earliest": "2026-12", "latest": "2027-08"},
            "location": {"allow_substrings": ["dc", "ny", "ca"],
                         "deny_substrings": ["canada", "united kingdom"],
                         "allow_remote": True, "keep_unknown": True},
            "posting_age": {"max_days": 14},
            "hard_reject_phrases": ["security clearance"],
            "ghost_jobs": {"max_reposts": 3},
        })

    def verdict(self, **job):
        job.setdefault("title", "Software Engineer")
        job.setdefault("description_text", "")
        job.setdefault("location", "New York, NY")
        return self.t1.evaluate(job)

    def test_pass(self):
        self.assertEqual(self.verdict()[0], "pass")

    def test_title_deny_beats_allow(self):
        self.assertEqual(self.verdict(title="Senior Software Engineer")[0], "reject")

    def test_title_must_match_allow(self):
        v, reason = self.verdict(title="Product Designer")
        self.assertEqual((v, reason), ("reject", "title-not-allowed"))

    def test_years_of_experience_ceiling(self):
        self.assertEqual(self.verdict(
            description_text="5+ years of relevant experience")[0], "reject")
        self.assertEqual(self.verdict(
            description_text="1-2 years experience preferred")[0], "pass")

    def test_internship_rejected_by_title(self):
        for title in ("Software Engineer Intern", "Software Engineering Co-op",
                      "Backend Engineer Coop", "Software Engineer, Summer Analyst"):
            v, reason = self.verdict(title=title)
            self.assertEqual(v, "reject", title)
            self.assertTrue(reason.startswith("internship:"), reason)

    def test_internship_rejected_by_employment_type(self):
        # A clean title under an Internship employment type is the case a
        # title-only rule misses.
        v, reason = self.verdict(title="Software Engineer",
                                 employment_type="Internship")
        self.assertEqual((v, reason), ("reject", "internship:type:internship"))

    def test_full_time_role_survives_the_internship_rule(self):
        self.assertEqual(self.verdict(title="Software Engineer",
                                      employment_type="Full-time")[0], "pass")
        # "International" and "Internal" must not trip the \bintern\b pattern.
        self.assertEqual(self.verdict(title="Software Engineer, Internal Tools")[0],
                         "pass")

    def test_internship_rule_is_switchable(self):
        cfg = dict(self.t1.cfg)
        cfg["employment"] = dict(cfg["employment"], exclude_internships=False)
        off = Tier1(cfg)
        self.assertEqual(off.evaluate({"title": "Software Engineer Intern",
                                       "description_text": "",
                                       "location": "New York, NY"})[0], "pass")

    def test_reference_requirement_rejected(self):
        for desc in ("Please provide three professional references.",
                     "References are required at time of application.",
                     "Submit a resume and two letters of recommendation."):
            v, reason = self.verdict(description_text=desc)
            self.assertEqual(v, "reject", desc)
            self.assertTrue(reason.startswith("references:"), reason)

    def test_technical_uses_of_reference_survive(self):
        # The failure mode this rule could easily have: eating real software
        # postings that talk about references in a code sense.
        for desc in ("Resolve circular references in the dependency graph.",
                     "Own our reference architecture for the platform.",
                     "Build cross-references between schema files.",
                     "Reference checks are conducted after an offer is made."):
            self.assertEqual(self.verdict(description_text=desc)[0], "pass", desc)

    def test_reference_rule_is_switchable(self):
        cfg = dict(self.t1.cfg)
        cfg["references"] = dict(cfg["references"], exclude_reference_required=False)
        off = Tier1(cfg)
        self.assertEqual(off.evaluate({
            "title": "Software Engineer", "location": "New York, NY",
            "description_text": "Please provide three professional references."})[0],
            "pass")

    def test_clearance_phrase(self):
        self.assertEqual(self.verdict(
            description_text="Active security clearance required.")[0], "reject")

    def test_grad_window(self):
        self.assertEqual(self.verdict(title="Software Engineer, 2025 Grads")[0], "reject")
        self.assertEqual(self.verdict(title="Software Engineer, 2027 Grads")[0], "pass")
        # No year mentioned at all is not evidence of a bad fit.
        self.assertEqual(self.verdict(title="Software Engineer")[0], "pass")

    def test_location_allow_wins_over_deny_in_multi_site_postings(self):
        self.assertEqual(self.verdict(location="New York, NY, Toronto, Canada")[0], "pass")

    def test_remote_does_not_bypass_the_deny_list(self):
        v, reason = self.verdict(location="Remote - Canada", remote_flag=1)
        self.assertEqual(v, "reject")
        self.assertIn("canada", reason)

    def test_remote_us_passes(self):
        self.assertEqual(self.verdict(location="Remote", remote_flag=1)[0], "pass")

    def test_ca_does_not_match_canada(self):
        # The substring trap: "ca" must match the token, not the word "canada".
        v, reason = self.verdict(location="Vancouver, Canada")
        self.assertEqual(v, "reject")
        self.assertIn("canada", reason)

    def test_unknown_location_is_kept(self):
        self.assertEqual(self.verdict(location="")[0], "pass")

    def test_stale_posting(self):
        self.assertEqual(self.verdict(posted_at="2020-01-01T00:00:00+00:00")[0], "reject")

    def test_ghost_reposts(self):
        self.assertEqual(self.verdict(repost_count=4)[0], "reject")
        self.assertEqual(self.verdict(repost_count=2)[0], "pass")


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _job(self, **over):
        job = {
            "job_id": "abc123", "company": "Acme", "company_slug": "acme",
            "title": "Software Engineer", "location": "New York, NY",
            "remote_flag": 0, "req_id": "1", "url": "https://x/1",
            "apply_url": "https://x/1/apply", "ats_vendor": "greenhouse",
            "department": "Eng", "employment_type": None,
            "description_text": "text", "posted_at": "2026-08-01T00:00:00+00:00",
            "content_hash": "h1", "dedup_key": dedup_key("Acme", "Software Engineer",
                                                         "New York, NY"),
        }
        job.update(over)
        return job

    def test_insert_then_update(self):
        self.assertEqual(db.upsert_job(self.conn, self._job()), "inserted")
        self.assertEqual(db.upsert_job(self.conn, self._job()), "updated")
        row = self.conn.execute("SELECT seen_count, repost_count FROM jobs").fetchone()
        self.assertEqual((row["seen_count"], row["repost_count"]), (2, 0))

    def test_close_then_repost_increments_ghost_counter(self):
        db.upsert_job(self.conn, self._job())
        closed = db.close_missing(self.conn, "greenhouse:acme", set())
        self.assertEqual(closed, 1)
        db.upsert_job(self.conn, self._job())
        row = self.conn.execute("SELECT repost_count, is_open FROM jobs").fetchone()
        self.assertEqual((row["repost_count"], row["is_open"]), (1, 1))

    def test_changed_content_invalidates_the_score_cache(self):
        db.upsert_job(self.conn, self._job())
        self.conn.execute("UPDATE jobs SET fit_score=88, score_json='{}'")
        db.upsert_job(self.conn, self._job(content_hash="h2"))
        self.assertIsNone(self.conn.execute("SELECT fit_score FROM jobs").fetchone()[0])

    def test_dedup_prefers_the_real_ats_over_the_aggregator(self):
        db.upsert_job(self.conn, self._job(job_id="agg1", ats_vendor="simplify",
                                           company_slug="newgrad"))
        db.upsert_job(self.conn, self._job(job_id="gh1"))
        result = dedup.rebuild(self.conn)
        self.assertEqual(result["marked"], 1)
        row = self.conn.execute(
            "SELECT job_id, duplicate_of FROM jobs WHERE duplicate_of IS NOT NULL"
        ).fetchone()
        self.assertEqual((row["job_id"], row["duplicate_of"]), ("agg1", "gh1"))

    def test_health_flags_a_source_that_is_200_but_empty(self):
        for _ in range(2):
            db.record_run(self.conn, source_key="lever:plaid", ats_vendor="lever",
                          company_slug="plaid", ok=1, http_status=200, fetched=0)
        self.conn.commit()
        alerts = ingest.health(self.conn)
        self.assertEqual([a["kind"] for a in alerts], ["empty"])

    def test_health_flags_a_failing_source(self):
        for _ in range(2):
            db.record_run(self.conn, source_key="greenhouse:embed",
                          ats_vendor="greenhouse", company_slug="embed", ok=0,
                          http_status=404, fetched=0, error="HTTP 404")
        self.conn.commit()
        self.assertEqual([a["kind"] for a in ingest.health(self.conn)], ["failing"])

    def test_health_is_quiet_after_one_run(self):
        db.record_run(self.conn, source_key="lever:plaid", ats_vendor="lever",
                      company_slug="plaid", ok=1, http_status=200, fetched=0)
        self.conn.commit()
        self.assertEqual(ingest.health(self.conn), [])


class TestScorer(unittest.TestCase):
    """Offline only. Nothing here calls the API or needs a key."""

    def setUp(self):
        from jobpipe import score
        self.score = score

    def test_system_prompt_is_cacheable_and_ends_with_the_breakpoint(self):
        blocks = self.score.build_system()
        # The breakpoint belongs on the last stable block, never earlier.
        self.assertNotIn("cache_control", blocks[0])
        self.assertEqual(blocks[-1]["cache_control"], {"type": "ephemeral"})
        # Under roughly 1024 tokens nothing caches at all.
        self.assertGreater(sum(len(b["text"]) for b in blocks), 4096)

    def test_system_prompt_is_byte_stable_across_calls(self):
        # A prefix that varies per request silently defeats the cache.
        self.assertEqual(self.score.build_system(), self.score.build_system())

    def test_system_prompt_carries_real_evidence_ids(self):
        text = "".join(b["text"] for b in self.score.build_system())
        self.assertIn("copytrader-scale", text)
        self.assertIn("no security clearance", text.lower())

    def test_user_turn_truncation_is_declared(self):
        job = {"company": "Acme", "title": "SWE", "location": "NY",
               "description_text": "x" * 9000}
        turn = self.score.build_user(job, description_chars=1000)
        self.assertIn("[description truncated for scoring]", turn)
        self.assertLess(len(turn), 1600)

    def test_missing_description_caps_the_score_in_the_prompt(self):
        turn = self.score.build_user({"company": "Acme", "title": "SWE",
                                      "description_text": ""})
        self.assertIn("cap the score at 60", turn)

    def test_schema_requires_every_property(self):
        schema = self.score.SCORE_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_cost_estimate_scales_with_job_count(self):
        job = {"company": "Acme", "title": "SWE", "location": "NY",
               "description_text": "a real posting " * 100}
        one = self.score.estimate_cost([job])
        many = self.score.estimate_cost([job] * 50)
        self.assertGreater(many["usd"], one["usd"])
        # Caching means 50 jobs must cost far less than 50 separate first calls.
        self.assertLess(many["usd"], one["usd"] * 50)

    def test_model_is_the_one_the_plan_names(self):
        self.assertEqual(self.score.MODEL, "claude-haiku-4-5")


class TestScorerPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "score.db")
        from jobpipe import score
        self.score = score
        self.conn.execute(
            "INSERT INTO jobs (job_id, company, company_slug, title, ats_vendor, "
            "first_seen_at, last_seen_at, filter_verdict, content_hash) "
            "VALUES ('a1','Acme','acme','SWE','greenhouse','2026-08-01','2026-08-01',"
            "'pass','h1')")
        self.conn.execute(
            "INSERT INTO jobs (job_id, company, company_slug, title, ats_vendor, "
            "first_seen_at, last_seen_at, filter_verdict, content_hash) "
            "VALUES ('b2','Acme','acme','Senior SWE','greenhouse','2026-08-01',"
            "'2026-08-01','reject','h2')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_pending_covers_only_tier1_survivors(self):
        ids = [j["job_id"] for j in self.score.pending(self.conn)]
        self.assertEqual(ids, ["a1"])

    def test_pending_skips_already_scored(self):
        self.conn.execute("UPDATE jobs SET fit_score=81 WHERE job_id='a1'")
        self.assertEqual(self.score.pending(self.conn), [])
        self.assertEqual(len(self.score.pending(self.conn, rescore=True)), 1)

    def test_manual_export_carries_the_same_prompt_submit_would_send(self):
        jobs = self.score.pending(self.conn)
        exported = self.score.manual_export(jobs)
        self.assertEqual(exported[0]["job_id"], "a1")
        self.assertEqual(exported[0]["prompt"], self.score.build_user(jobs[0]))

    def test_ingest_manual_writes_a_valid_result(self):
        good = {"job_id": "a1", "fit_score": 77, "rationale": "Solid overlap.",
                "resume_variant": "systems", "evidence_ids": ["copytrader-scale"],
                "gaps": [], "red_flags": [], "effort": "low", "referral_path": "none"}
        run = self.score.ingest_manual(self.conn, [good], {"a1": "h1"})
        self.assertEqual((run.scored, run.errored), (1, 0))
        row = self.conn.execute(
            "SELECT fit_score, scored_at, score_json FROM jobs WHERE job_id='a1'").fetchone()
        self.assertEqual(row["fit_score"], 77)
        self.assertIsNotNone(row["scored_at"])
        saved = json.loads(row["score_json"])
        self.assertEqual(saved["_model"], "manual")
        self.assertEqual(saved["_scored_content_hash"], "h1")

    def test_ingest_manual_rejects_an_invented_evidence_id(self):
        bad = {"job_id": "a1", "fit_score": 77, "rationale": "x",
               "resume_variant": "systems", "evidence_ids": ["not-a-real-id"],
               "gaps": [], "red_flags": [], "effort": "low", "referral_path": "none"}
        run = self.score.ingest_manual(self.conn, [bad], {"a1": "h1"})
        self.assertEqual((run.scored, run.errored), (0, 1))
        self.assertIsNone(
            self.conn.execute("SELECT fit_score FROM jobs WHERE job_id='a1'").fetchone()[0])

    def test_ingest_manual_rejects_an_out_of_range_score(self):
        bad = {"job_id": "a1", "fit_score": 150, "rationale": "x",
               "resume_variant": "systems", "evidence_ids": [],
               "gaps": [], "red_flags": [], "effort": "low", "referral_path": "none"}
        run = self.score.ingest_manual(self.conn, [bad], {"a1": "h1"})
        self.assertEqual((run.scored, run.errored), (0, 1))

    def test_ingest_manual_rejects_a_missing_field(self):
        bad = {"job_id": "a1", "fit_score": 77}
        run = self.score.ingest_manual(self.conn, [bad], {"a1": "h1"})
        self.assertEqual((run.scored, run.errored), (0, 1))
        self.assertIn("missing fields", run.errors[0])

    def test_changed_posting_text_clears_the_score(self):
        # upsert_job invalidates on content_hash change, which is what makes the
        # scoring cache safe to trust.
        self.conn.execute("UPDATE jobs SET fit_score=81 WHERE job_id='a1'")
        db.upsert_job(self.conn, {
            "job_id": "a1", "company": "Acme", "company_slug": "acme", "title": "SWE",
            "location": None, "remote_flag": 0, "req_id": "1", "url": None,
            "apply_url": None, "ats_vendor": "greenhouse", "department": None,
            "employment_type": None, "description_text": "changed",
            "posted_at": None, "content_hash": "h9", "dedup_key": "d1"})
        self.assertIsNone(
            self.conn.execute("SELECT fit_score FROM jobs WHERE job_id='a1'").fetchone()[0])


class TestApplyMapping(unittest.TestCase):
    """Offline. Every case here is a label seen on a real Greenhouse form."""

    #: Tests run against the checked-in example profile, never the user's real
    #: one, so they assert the same thing on every machine and the suite does
    #: not depend on private data.
    @staticmethod
    def example_facts():
        import yaml
        from jobpipe.config import PROFILE_DIR
        return yaml.safe_load(
            (PROFILE_DIR / "facts.example.yaml").read_text(encoding="utf-8"))

    def setUp(self):
        from jobpipe import apply
        self.apply = apply
        self.rules = apply._rules(self.example_facts())

    def answer(self, label, **kw):
        fld = self.apply.Field(label=label, **kw)
        return self.apply.resolve(fld, self.rules)

    # -- the substring traps, all three of which shipped as bugs first --
    def test_united_states_is_not_an_address_line_2(self):
        # "unit" matched inside "United States" and ate the work-auth question.
        f = self.answer("Are you legally authorized to work in the United States?",
                        options=["I am authorized to work in the United States "
                                 "for any employer", "I require sponsorship"])
        self.assertEqual(f.status, "filled")
        self.assertIn("any employer", f.value)

    def test_undergraduate_gpa_is_not_a_graduate_gpa(self):
        # "graduate" matched inside "Undergraduate" and answered N/A.
        f = self.answer("GPA (Undergraduate)",
                        options=["Not applicable/Do not recall", "4.0 out of 4.0",
                                 "3.6 out of 4.0", "3.5 out of 4.0"])
        self.assertEqual(f.value, "3.5 out of 4.0")

    def test_graduate_gpa_is_not_applicable(self):
        f = self.answer("GPA (Graduate)",
                        options=["Other/Not Applicable", "4.0 out of 4.0",
                                 "3.9 out of 4.0"])
        self.assertEqual(f.value, "Other/Not Applicable")

    def test_essential_functions_answers_yes_not_the_accommodation_value(self):
        # This one answered "No" at first, which is materially wrong.
        f = self.answer("Can you perform all of the essential functions of this "
                        "role with or without reasonable accommodation?",
                        options=["Yes", "No"])
        self.assertEqual(f.value, "Yes")

    # -- refusals --
    def test_salary_is_never_filled(self):
        for label in ("Desired Salary", "Expected Compensation", "Pay Rate"):
            f = self.answer(label)
            self.assertEqual(f.status, "skipped", label)
            self.assertIn("salary", f.note)

    def test_identifiers_and_signatures_are_never_filled(self):
        for label in ("Social Security Number", "Driver's License Number",
                      "Password", "Date of Birth", "Signature", "Bank Account",
                      "Initials", "Type your initials to sign"):
            self.assertEqual(self.answer(label).status, "skipped", label)

    def test_middle_initial_is_not_treated_as_a_signature(self):
        # The signature guard's \binitials?\b matched "Middle Initial".
        self.assertEqual(self.answer("Middle Initial").value, "A")

    # -- documents --
    def test_resume_goes_to_the_upload_not_the_textarea(self):
        upload = self.answer("Resume/CV", type="input_file")
        self.assertEqual(upload.status, "filled")
        self.assertTrue(str(upload.value).endswith(".pdf"))
        paste = self.answer("Resume/CV", type="textarea")
        self.assertEqual(paste.status, "unanswered")

    def test_missing_document_is_unanswered_not_invented(self):
        f = self.answer("Cover Letter", type="input_file")
        self.assertEqual(f.status, "unanswered")
        self.assertIsNone(f.value)

    # -- ordinary fields --
    def test_core_identity_fields(self):
        cases = {"First Name": "Jordan", "Last Name": "Rivera",
                 "Email": "jordan.rivera@example.com", "City": "Springfield",
                 "State": "IL", "Zip Code": "62701",
                 "Middle Initial": "A", "Pronouns": "they/them"}
        for label, expected in cases.items():
            self.assertEqual(self.answer(label).value, expected, label)

    def test_location_picker_gets_the_state(self):
        # A bare city name lets a geocoded picker choose another state's
        # version of it, which is how an address ends up in the wrong place.
        f = self.answer("Location (City)")
        self.assertEqual(f.value, "Springfield, IL")

    def test_full_name_for_boards_with_one_name_field(self):
        # Lever asks for a single "Full name"; without this the required name
        # field on every Lever form went unanswered.
        self.assertEqual(self.answer("Full Name").value, "Jordan Rivera")

    def test_clearance_reports_none_held(self):
        f = self.answer("Active Security Clearance(s)",
                        options=["Top Secret", "Secret", "Never held a clearance"])
        self.assertEqual(f.value, "Never held a clearance")

    def test_eeo_answers_match_the_options_a_form_offers(self):
        # Answered rather than declined, as of 2026-08-26. Each rule carries
        # several phrasings because option wording differs between boards.
        # The example profile declines everything, which every board allows.
        cases = [
            ("Gender", ["Male", "Female", "Decline to self identify"],
             "Decline to self identify"),
            ("Race / Ethnicity", ["White", "Asian", "Decline to self identify"],
             "Decline to self identify"),
            ("Veteran Status",
             ["I am not a protected veteran", "I identify as a veteran"],
             "I am not a protected veteran"),
        ]
        for label, options, expected in cases:
            self.assertEqual(self.answer(label, options=options).value,
                             expected, label)

    def test_unknown_label_is_flagged_not_guessed(self):
        f = self.answer("SAT Score")
        self.assertEqual(f.status, "unanswered")
        self.assertIsNone(f.value)

    def test_select_with_no_matching_option_goes_to_review(self):
        f = self.answer("How did you hear about this job?",
                        options=["Carrier pigeon", "Smoke signal"])
        self.assertIn(f.status, ("review", "filled"))
        if f.status == "filled":
            self.assertIn(f.value, ["Carrier pigeon", "Smoke signal"])

    def test_option_matching_prefers_a_real_option(self):
        f = self.answer("How did you hear about this job?",
                        options=["Careers site", "Friend or family", "Glassdoor"])
        self.assertEqual(f.value, "Careers site")

    def test_nearest_numeric_option(self):
        pick = self.apply.nearest_numeric_option
        opts = ["4.0 out of 4.0", "3.9 out of 4.0", "3.8 out of 4.0"]
        self.assertEqual(pick("3.89", opts), "3.9 out of 4.0")
        self.assertEqual(pick("3.81", opts), "3.8 out of 4.0")
        self.assertIsNone(pick("not a number", opts))

    def test_greenhouse_post_id_comes_from_the_url_not_req_id(self):
        # req_id is the requisition id, shared across cities; /jobs/{req_id} 404s.
        job = {"url": "https://boards.greenhouse.io/spacex/jobs/8719858002?gh_jid=8719858002",
               "req_id": "6485678002"}
        self.assertEqual(self.apply.greenhouse_post_id(job), "8719858002")
        self.assertIsNone(self.apply.greenhouse_post_id({"url": "https://x.com/jobs"}))

    def test_instructions_forbid_submitting(self):
        packet = self.apply.Packet(job_id="abc", company="Acme", title="SWE",
                                   apply_url="https://x/apply")
        text = self.apply.instructions(packet)
        self.assertIn("DO NOT SUBMIT", text)
        self.assertIn("Never click Submit", text)

    def test_packet_dict_carries_the_submit_ban(self):
        packet = self.apply.Packet(job_id="abc")
        self.assertIs(packet.to_dict()["never_submit"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
