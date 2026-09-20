"""Engine <-> site sync. Feature 20.

Against tests/fake_turso.py, so every push and pull crosses a real socket in
Turso's protocol. The rules under test are the ones in sync.py's docstring:
only changed rows travel, the site owns triage, the engine owns everything else
including scores, and a stripped description never wipes the site's copy.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_turso import FakeTurso  # noqa: E402
from jobpipe import db, filters, remote, sync, web  # noqa: E402
from jobpipe.util import content_hash  # noqa: E402


def add_job(conn, jid, verdict="pass", reason=None, status="discovered", is_open=1,
            duplicate_of=None, desc=None, score=None, title=None):
    title = title or f"Engineer {jid}"
    desc = f"description of {jid}" if desc is None else desc
    conn.execute(
        "INSERT INTO jobs (job_id, company, company_slug, title, location, url, ats_vendor, "
        "is_open, duplicate_of, filter_verdict, filter_reason, status, fit_score, "
        "description_text, content_hash, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (jid, "Acme", "acme", title, "Remote", f"https://x/{jid}", "greenhouse", is_open,
         duplicate_of, verdict, reason, status, score, desc,
         content_hash(title, "Remote", desc)))
    db.log_event(conn, jid, "discovered", f"https://x/{jid}")


def add_run(conn, source="greenhouse:acme", ok=1, fetched=5):
    db.record_run(conn, source_key=source, ats_vendor=source.split(":")[0],
                  company_slug=source.split(":")[1], ok=ok, http_status=200 if ok else 500,
                  fetched=fetched, inserted=0, updated=0, duration_ms=10,
                  error=None if ok else "boom")


class SyncBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.engine = db.connect(Path(self.dir.name) / "engine.db")
        add_job(self.engine, "p1", score=80)
        add_job(self.engine, "p2", status="submitted")
        add_job(self.engine, "r1", "reject", "title-deny:manager")
        add_job(self.engine, "c1", is_open=0)
        add_job(self.engine, "d1", duplicate_of="p1")
        self.engine.execute("UPDATE jobs SET notes='ask about visa' WHERE job_id='p2'")
        for _ in range(5):
            add_run(self.engine)
        add_run(self.engine, "lever:beta", ok=0, fetched=0)
        add_run(self.engine, "lever:beta", ok=0, fetched=0)
        self.engine.commit()
        self.fake = FakeTurso(Path(self.dir.name) / "site.db").__enter__()
        self.site = remote.Connection(self.fake.url, self.fake.token)

    def tearDown(self):
        self.engine.close()
        self.fake.__exit__(None, None, None)
        self.dir.cleanup()

    def site_row(self, jid):
        return self.site.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone()


class Push(SyncBase):
    def test_first_push_sends_everything_once(self):
        s = sync.push(self.engine, self.site)
        self.assertTrue(s["full"])
        self.assertEqual((s["jobs"], s["new_jobs"]), (5, 5))
        self.assertEqual(s["events"], 5)
        again = sync.push(self.engine, self.site)
        self.assertEqual((again["full"], again["jobs"], again["events"], again["runs"]),
                         (False, 0, 0, 0))

    def test_site_answers_like_the_engine(self):
        sync.push(self.engine, self.site)
        a, b = web.Api(self.engine), web.Api(self.site)
        self.assertEqual(a.summary(), b.summary())
        self.assertEqual(a.sources(), b.sources())
        self.assertEqual(a.jobs({"bin": ["all"]}), b.jobs({"bin": ["all"]}))

    def test_triage_travels_on_insert(self):
        sync.push(self.engine, self.site)
        self.assertEqual(self.site_row("p2")["status"], "submitted")
        self.assertEqual(self.site_row("p2")["notes"], "ask about visa")

    def test_descriptions_only_for_rows_a_list_can_reach(self):
        sync.push(self.engine, self.site)
        self.assertEqual(self.site_row("r1")["description_text"], "description of r1")
        self.assertIsNone(self.site_row("c1")["description_text"])
        self.assertIsNone(self.site_row("d1")["description_text"])

    def test_churn_alone_pushes_nothing(self):
        # Past the 1 -> 2 step, which is a real change (the NEW flag clears).
        self.engine.execute("UPDATE jobs SET seen_count=3")
        self.engine.commit()
        sync.push(self.engine, self.site)
        self.engine.execute("UPDATE jobs SET seen_count=seen_count+1, "
                            "last_seen_at=datetime('now','+1 hour')")
        self.engine.commit()
        self.assertEqual(sync.push(self.engine, self.site)["jobs"], 0)

    def test_new_flag_clears_once_then_churn_is_quiet(self):
        self.engine.execute("UPDATE jobs SET seen_count=1")
        self.engine.commit()
        sync.push(self.engine, self.site)
        pushes = []
        for n in (2, 3, 4):
            self.engine.execute("UPDATE jobs SET seen_count=? WHERE job_id='p1'", (n,))
            self.engine.commit()
            pushes.append(sync.push(self.engine, self.site)["jobs"])
        self.assertEqual(pushes, [1, 0, 0])
        self.assertEqual(self.site_row("p1")["seen_count"], 2)

    def test_posting_change_keeps_site_triage(self):
        sync.push(self.engine, self.site)
        web.Api(self.site).triage("p1", {"status": "shortlisted", "note": "from phone"})
        self.engine.execute("UPDATE jobs SET title='Senior Engineer p1' WHERE job_id='p1'")
        self.engine.commit()
        self.assertEqual(sync.push(self.engine, self.site)["jobs"], 1)
        row = self.site_row("p1")
        self.assertEqual((row["title"], row["status"], row["notes"]),
                         ("Senior Engineer p1", "shortlisted", "from phone"))

    def test_scores_come_from_the_engine(self):
        # The scheduled poll runs the offline scorer, so a new score is an
        # engine change like any other and travels on the next push.
        sync.push(self.engine, self.site)
        self.engine.execute("UPDATE jobs SET fit_score=91, scored_at=datetime('now') "
                            "WHERE job_id='p2'")
        self.engine.commit()
        self.assertEqual(sync.push(self.engine, self.site)["jobs"], 1)
        self.assertEqual(self.site_row("p2")["fit_score"], 91)
        # A text change wipes the engine's score, and the site follows.
        self.engine.execute("UPDATE jobs SET content_hash='new', fit_score=NULL "
                            "WHERE job_id='p2'")
        self.engine.commit()
        sync.push(self.engine, self.site)
        self.assertIsNone(self.site_row("p2")["fit_score"])

    def test_stripped_description_keeps_the_sites_copy(self):
        sync.push(self.engine, self.site)
        sync.strip(self.engine)
        self.assertIsNone(self.engine.execute(
            "SELECT description_text FROM jobs WHERE job_id='r1'").fetchone()[0])
        self.engine.execute("UPDATE jobs SET filter_reason='title-deny:lead' WHERE job_id='r1'")
        self.engine.commit()
        sync.push(self.engine, self.site)
        row = self.site_row("r1")
        self.assertEqual((row["filter_reason"], row["description_text"]),
                         ("title-deny:lead", "description of r1"))

    def test_closing_a_posting_drops_its_text_on_the_site(self):
        sync.push(self.engine, self.site)
        self.engine.execute("UPDATE jobs SET is_open=0 WHERE job_id='p1'")
        self.engine.commit()
        sync.push(self.engine, self.site)
        row = self.site_row("p1")
        self.assertEqual((row["is_open"], row["description_text"]), (0, None))

    def test_new_events_only(self):
        sync.push(self.engine, self.site)
        db.log_event(self.engine, "p1", "reposted", "repost #1")
        self.engine.commit()
        self.assertEqual(sync.push(self.engine, self.site)["events"], 1)
        kinds = [r[0] for r in self.site.execute(
            "SELECT kind FROM events WHERE job_id='p1' ORDER BY id")]
        self.assertEqual(kinds, ["discovered", "reposted"])

    def test_site_keeps_last_three_runs_per_source(self):
        sync.push(self.engine, self.site)
        n = dict(self.site.execute(
            "SELECT source_key, COUNT(*) FROM source_runs GROUP BY source_key").fetchall())
        self.assertEqual(n, {"greenhouse:acme": 3, "lever:beta": 2})
        for _ in range(4):
            add_run(self.engine)
        self.engine.commit()
        sync.push(self.engine, self.site)
        n = self.site.execute("SELECT COUNT(*) FROM source_runs "
                              "WHERE source_key='greenhouse:acme'").fetchone()[0]
        self.assertEqual(n, 3)
        self.assertEqual(web.Api(self.engine).sources(), web.Api(self.site).sources())

    def test_interrupted_push_resumes_without_resending(self):
        for i in range(40):
            add_job(self.engine, f"n{i:02d}")
        self.engine.commit()
        old = sync.BATCH_STMTS
        sync.BATCH_STMTS = 10
        try:
            self.fake.fail_after = len(self.fake.requests) + 12   # schema, meta, then 2 batches
            with self.assertRaises(remote.RemoteError):
                sync.push(self.engine, self.site)
            sent = self.engine.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0]
            self.assertGreater(sent, 0)
            self.fake.fail_after = None
            s = sync.push(self.engine, self.site)
            self.assertEqual(s["jobs"], 45 - sent)
        finally:
            sync.BATCH_STMTS = old
        self.assertEqual(self.site.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 45)

    def test_reset_site_gets_a_full_push(self):
        sync.push(self.engine, self.site)
        with FakeTurso(Path(self.dir.name) / "fresh.db") as fresh:
            s = sync.push(self.engine, remote.Connection(fresh.url, fresh.token))
            self.assertTrue(s["full"])
            self.assertEqual(s["jobs"], 5)

    def test_new_engine_column_reaches_the_site(self):
        sync.push(self.engine, self.site)
        self.engine.execute("ALTER TABLE jobs ADD COLUMN salary TEXT")
        self.engine.execute("UPDATE jobs SET salary='$150k' WHERE job_id='p1'")
        self.engine.commit()
        sync.push(self.engine, self.site)
        self.assertEqual(self.site_row("p1")["salary"], "$150k")


class Pull(SyncBase):
    def test_site_triage_comes_back_and_scores_do_not(self):
        sync.push(self.engine, self.site)
        web.Api(self.site).triage("p1", {"status": "dismissed", "note": "not now"})
        self.site.execute("UPDATE jobs SET fit_score=55 WHERE job_id='r1'")
        self.site.commit()
        s = sync.pull(self.engine, self.site)
        self.assertEqual((s["status"], s["notes"]), (1, 1))
        self.assertNotIn("fit_score", s)
        self.assertIsNone(self.engine.execute(
            "SELECT fit_score FROM jobs WHERE job_id='r1'").fetchone()[0])
        row = self.engine.execute("SELECT status, notes FROM jobs WHERE job_id='p1'").fetchone()
        self.assertEqual(tuple(row), ("dismissed", "not now"))

    def test_pull_then_push_is_quiet(self):
        sync.push(self.engine, self.site)
        web.Api(self.site).triage("p1", {"status": "shortlisted"})
        sync.pull(self.engine, self.site)
        s = sync.push(self.engine, self.site)
        self.assertEqual((s["jobs"], s["events"]), (0, 0))

    def test_nothing_changed_nothing_written(self):
        sync.push(self.engine, self.site)
        s = sync.pull(self.engine, self.site)
        self.assertEqual(sum(s[c] for c in sync.SITE_OWNED), 0)


class Strip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.dir.name) / "e.db")

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def test_strip_keeps_only_passing_open_text(self):
        add_job(self.conn, "p1")
        add_job(self.conn, "r1", "reject", "yoe:5y-required")
        add_job(self.conn, "c1", is_open=0)
        self.conn.commit()
        self.assertEqual(sync.strip(self.conn), 2)
        left = dict(self.conn.execute("SELECT job_id, description_text FROM jobs"))
        self.assertEqual(left, {"p1": "description of p1", "r1": None, "c1": None})

    def test_refilter_keeps_a_stripped_rows_verdict(self):
        # The text said 5 years required; stripped, the rule could not fire.
        add_job(self.conn, "r1", "reject", "yoe:5y-required",
                desc="Requires 5+ years of professional experience.")
        self.conn.commit()
        sync.strip(self.conn)
        counts = filters.apply(self.conn, only_new=False)
        self.assertEqual(counts["kept"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT filter_reason FROM jobs WHERE job_id='r1'").fetchone()[0],
            "yoe:5y-required")

    def test_refilter_still_judges_a_posting_that_never_had_text(self):
        add_job(self.conn, "e1", "reject", "stale:99d", desc="")
        self.conn.execute("UPDATE jobs SET description_text=NULL WHERE job_id='e1'")
        self.conn.commit()
        counts = filters.apply(self.conn, only_new=False)
        self.assertEqual(counts["kept"], 0)
        self.assertEqual(counts["evaluated"], 1)


if __name__ == "__main__":
    unittest.main()
