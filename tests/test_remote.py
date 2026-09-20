"""The Turso client. Feature 20.

Runs against tests/fake_turso.py, a local server speaking Turso's HTTP protocol
over a real socket, so nothing here needs an account or the network. The test
that matters most is the last class: the site's `web.Api`, run over the remote
connection, must answer every endpoint exactly as it does over sqlite3.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_turso import FakeTurso  # noqa: E402
from jobpipe import db, remote, web  # noqa: E402


class Client(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.fake = FakeTurso(Path(self.dir.name) / "remote.db").__enter__()
        self.conn = remote.Connection(self.fake.url, self.fake.token)
        self.conn.execute("CREATE TABLE t (i INTEGER, f REAL, s TEXT, b BLOB, n TEXT)")
        self.conn.commit()

    def tearDown(self):
        self.fake.__exit__(None, None, None)
        self.dir.cleanup()

    def test_libsql_url_becomes_https(self):
        c = remote.Connection("libsql://db-org.turso.io", "x")
        self.assertEqual(c.endpoint, "https://db-org.turso.io/v2/pipeline")

    def test_types_round_trip(self):
        big = 2 ** 53 + 1   # past float precision: must travel as a string
        self.conn.execute("INSERT INTO t VALUES (?,?,?,?,?)",
                          (big, 1.5, "かな ✓", b"\x00\xff", None))
        self.conn.commit()
        row = self.conn.execute("SELECT i, f, s, b, n FROM t").fetchone()
        self.assertEqual(tuple(row), (big, 1.5, "かな ✓", b"\x00\xff", None))

    def test_row_behaves_like_sqlite3_row(self):
        self.conn.execute("INSERT INTO t (i, s) VALUES (?,?)", (7, "x"))
        self.conn.commit()
        row = self.conn.execute("SELECT i, s FROM t").fetchone()
        self.assertEqual(row[0], 7)
        self.assertEqual(row["s"], "x")
        self.assertEqual(dict(row), {"i": 7, "s": "x"})
        self.assertEqual(row.keys(), ["i", "s"])

    def test_writes_wait_for_commit(self):
        self.conn.execute("INSERT INTO t (i) VALUES (1)")
        # Another connection cannot see it yet: nothing has been sent.
        other = remote.Connection(self.fake.url, self.fake.token)
        self.assertEqual(other.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
        self.conn.commit()
        self.assertEqual(other.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    def test_a_read_sees_earlier_uncommitted_writes(self):
        self.conn.execute("INSERT INTO t (i) VALUES (1)")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    def test_failed_batch_rolls_back_whole(self):
        self.conn.execute("INSERT INTO t (i) VALUES (1)")
        self.conn.execute("INSERT INTO no_such_table VALUES (2)")
        with self.assertRaises(remote.RemoteError):
            self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)

    def test_close_discards_uncommitted(self):
        self.conn.execute("INSERT INTO t (i) VALUES (1)")
        self.conn.close()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)

    def test_sql_error_on_read_raises(self):
        with self.assertRaises(remote.RemoteError):
            self.conn.execute("SELECT nope FROM t")

    def test_bad_token_raises_with_status(self):
        bad = remote.Connection(self.fake.url, "wrong")
        with self.assertRaisesRegex(remote.RemoteError, "HTTP 401"):
            bad.execute("SELECT 1")

    def test_unreachable_server_raises(self):
        dead = remote.Connection("http://127.0.0.1:9", "x", timeout=0.5)
        with self.assertRaisesRegex(remote.RemoteError, "cannot reach"):
            dead.execute("SELECT 1")

    def test_counts_rows_written(self):
        self.conn.executemany("INSERT INTO t (i) VALUES (?)", [(1,), (2,), (3,)])
        self.conn.commit()
        self.assertEqual(self.conn.rows_written, 3)

    def test_schema_file_applies(self):
        self.conn.executescript(db.SCHEMA.read_text(encoding="utf-8"))
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        self.assertTrue({"jobs", "events", "source_runs", "idx_jobs_bins"} <= names, names)

    def test_from_env_needs_both_vars(self):
        self.assertIsNone(remote.from_env({"TURSO_DATABASE_URL": "libsql://x"}))
        self.assertIsNotNone(remote.from_env(
            {"TURSO_DATABASE_URL": "libsql://x", "TURSO_AUTH_TOKEN": "t"}))


class ApiParity(unittest.TestCase):
    """Every endpoint the hosted site serves, local vs remote, same answer."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        local_path = Path(self.dir.name) / "local.db"
        self.local = db.connect(local_path)
        rows = [
            ("p1", "pass", None, "discovered", 88), ("p2", "pass", None, "shortlisted", None),
            ("p3", "pass", None, "submitted", 40), ("p4", "pass", None, "dismissed", None),
            ("r1", "reject", "title-deny:manager", "discovered", None),
            ("r2", "reject", "location:berlin", "discovered", None),
            ("r3", "reject", "something-new:unmapped", "discovered", None),
        ]
        for jid, verdict, reason, status, score in rows:
            self.local.execute(
                "INSERT INTO jobs (job_id, company, title, location, url, ats_vendor, "
                "company_slug, is_open, filter_verdict, filter_reason, status, fit_score, "
                "description_text, first_seen_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,datetime('now'),datetime('now'))",
                (jid, "Acme", f"Engineer {jid}", "Remote", f"https://x/{jid}",
                 "greenhouse", "acme", verdict, reason, status, score, f"desc {jid}"))
            db.log_event(self.local, jid, "discovered", f"https://x/{jid}")
        for ok, fetched in ((1, 5), (0, 0), (0, 0)):
            db.record_run(self.local, source_key="greenhouse:acme", ats_vendor="greenhouse",
                          company_slug="acme", ok=ok, http_status=200 if ok else 500,
                          fetched=fetched, inserted=0, updated=0, duration_ms=10,
                          error=None if ok else "boom")
        self.local.commit()
        self.local.close()
        # The fake server gets an identical copy of the file.
        remote_path = Path(self.dir.name) / "remote.db"
        shutil.copy(local_path, remote_path)
        self.local = db.connect(local_path)
        self.fake = FakeTurso(remote_path).__enter__()
        self.remote = remote.Connection(self.fake.url, self.fake.token)
        self.a, self.b = web.Api(self.local), web.Api(self.remote)

    def tearDown(self):
        self.local.close()
        self.fake.__exit__(None, None, None)
        self.dir.cleanup()

    def test_summary(self):
        self.assertEqual(self.a.summary(), self.b.summary())

    def test_sources_and_alerts(self):
        got = self.b.sources()
        self.assertEqual(self.a.sources(), got)
        self.assertEqual([x["kind"] for x in got["alerts"]], ["failing"])

    def test_rejects(self):
        self.assertEqual(self.a.rejects(), self.b.rejects())

    def test_job_lists_for_every_bin_and_sort(self):
        for bin_key in ("pass", "all", "applied", "passed", "unbinned"):
            for sort in web.SORTS:
                q = {"bin": [bin_key], "sort": [sort]}
                self.assertEqual(self.a.jobs(q), self.b.jobs(q), (bin_key, sort))
        q = {"bin": ["all"], "q": ["p2"]}
        self.assertEqual(self.a.jobs(q), self.b.jobs(q))

    def test_job_detail(self):
        self.assertEqual(self.a.job("p1"), self.b.job("p1"))
        self.assertIsNone(self.b.job("nope"))

    def test_triage_writes_status_note_and_event_together(self):
        self.b.triage("p1", {"status": "shortlisted", "note": "  call Tue  "})
        self.a.triage("p1", {"status": "shortlisted", "note": "  call Tue  "})
        ra, rb = self.a.job("p1"), self.b.job("p1")
        self.assertEqual(rb["status"], "shortlisted")
        self.assertEqual(rb["notes"], "call Tue")
        self.assertEqual([e["kind"] for e in ra["events"]], [e["kind"] for e in rb["events"]])

    def test_triage_rejects_unknown_state(self):
        with self.assertRaises(ValueError):
            self.b.triage("p1", {"status": "hired"})


if __name__ == "__main__":
    unittest.main()
