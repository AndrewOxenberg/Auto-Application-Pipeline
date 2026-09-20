"""The hosted WSGI app. Feature 20.

Called the way Vercel calls it, with a WSGI environ, against the fake Turso
server seeded by a real `sync.push`. The personal routes must not exist here.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_turso import FakeTurso  # noqa: E402
from jobpipe import db, hosted, remote, sync  # noqa: E402
from test_sync import add_job, add_run  # noqa: E402


def call(method, path, body=None, ctype="application/json", query=""):
    raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
           "CONTENT_TYPE": ctype if body is not None else "",
           "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
    got = {}

    def start_response(status, headers):
        got["status"], got["headers"] = int(status.split()[0]), dict(headers)

    out = b"".join(hosted.app(env, start_response))
    ctype_out = got["headers"]["Content-Type"]
    return got["status"], got["headers"], (json.loads(out) if "json" in ctype_out else out)


class Hosted(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        engine = db.connect(Path(self.dir.name) / "engine.db")
        add_job(engine, "a1")
        add_job(engine, "b2", "reject", "title-deny:manager")
        add_run(engine)
        engine.commit()
        self.fake = FakeTurso(Path(self.dir.name) / "site.db").__enter__()
        sync.push(engine, remote.Connection(self.fake.url, self.fake.token))
        engine.close()
        self.env = mock.patch.dict(os.environ, {"TURSO_DATABASE_URL": self.fake.url,
                                                "TURSO_AUTH_TOKEN": self.fake.token})
        self.env.start()
        os.environ.pop("GITHUB_DISPATCH_TOKEN", None)

    def tearDown(self):
        self.env.stop()
        self.fake.__exit__(None, None, None)
        self.dir.cleanup()

    def test_index_is_served_flagged_as_hosted(self):
        code, headers, body = call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"window.JOBPIPE_HOSTED = true", body)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_read_endpoints(self):
        self.assertEqual(call("GET", "/api/summary")[2]["passed"], 1)
        jobs = call("GET", "/api/jobs", query="bin=all")[2]
        self.assertEqual({j["job_id"] for j in jobs["jobs"]}, {"a1", "b2"})
        self.assertEqual(call("GET", "/api/job/a1")[2]["title"], "Engineer a1")
        self.assertEqual(len(call("GET", "/api/sources")[2]["sources"]), 1)
        self.assertIn("reasons", call("GET", "/api/rejects")[2])

    def test_personal_routes_do_not_exist(self):
        for method, path in (("GET", "/api/resume"), ("GET", "/api/apply/pending"),
                             ("POST", "/api/apply/pending"), ("POST", "/api/apply/done"),
                             ("GET", "/api/job/a1/fillpacket"), ("GET", "/debug/fill.js")):
            code, _, body = call(method, path, body={} if method == "POST" else None)
            self.assertEqual(code, 404, path)
            self.assertIn("hosted", body["error"])

    def test_triage_round_trip(self):
        code, _, body = call("POST", "/api/job/a1/triage", {"status": "shortlisted",
                                                            "note": "phone"})
        self.assertEqual((code, body["changed"]), (200, ["status", "note"]))
        job = call("GET", "/api/job/a1")[2]
        self.assertEqual((job["status"], job["notes"]), ("shortlisted", "phone"))

    def test_triage_refuses_a_form_post(self):
        code, _, _ = call("POST", "/api/job/a1/triage", b"status=dismissed",
                          ctype="application/x-www-form-urlencoded")
        self.assertEqual(code, 415)
        self.assertEqual(call("GET", "/api/job/a1")[2]["status"], "discovered")

    def test_triage_errors(self):
        self.assertEqual(call("POST", "/api/job/zz/triage", {"status": "x"})[0], 404)
        self.assertEqual(call("POST", "/api/job/ffff/triage", {"status": "shortlisted"})[0], 404)
        self.assertEqual(call("POST", "/api/job/a1/triage", {"status": "hired"})[0], 400)
        self.assertEqual(call("POST", "/api/job/a1/triage", b"[1]")[0], 400)
        self.assertEqual(call("GET", "/api/job/not-hex")[0], 400)
        self.assertEqual(call("GET", "/api/job/../etc")[0], 404)

    def test_poll_status_without_dispatch(self):
        body = call("GET", "/api/poll")[2]
        self.assertEqual((body["hosted"], body["can_dispatch"]), (True, False))
        self.assertIsNotNone(body["last_run"])
        self.assertEqual(call("POST", "/api/poll", {})[0], 501)

    def test_missing_store_config_is_a_clear_503(self):
        with mock.patch.dict(os.environ, {"TURSO_DATABASE_URL": ""}):
            code, _, body = call("GET", "/api/summary")
        self.assertEqual(code, 503)
        self.assertIn("TURSO", body["error"])

    def test_store_refusing_us_is_a_502(self):
        with mock.patch.dict(os.environ, {"TURSO_AUTH_TOKEN": "wrong"}):
            code, _, body = call("GET", "/api/summary")
        self.assertEqual(code, 502)
        self.assertIn("site store", body["error"])


class LocalWriteThrough(unittest.TestCase):
    """Triage on the laptop's server also lands on the site, when switched on."""

    def setUp(self):
        from jobpipe import web
        self.web = web
        self.dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.dir.name) / "local.db"
        local = db.connect(self.db_path)
        add_job(local, "a1")
        add_job(local, "b2")
        local.commit()
        self.fake = FakeTurso(Path(self.dir.name) / "site.db").__enter__()
        self.site = remote.Connection(self.fake.url, self.fake.token)
        sync.push(local, self.site)
        add_job(local, "c3")          # exists locally, not yet on the site
        local.commit()
        local.close()
        self.env = mock.patch.dict(os.environ, {"TURSO_DATABASE_URL": self.fake.url,
                                                "TURSO_AUTH_TOKEN": self.fake.token})
        self.env.start()
        web.Handler.db_path = self.db_path
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.web.Handler.mirror = False
        self.web.Handler.db_path = None
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.stop()
        self.fake.__exit__(None, None, None)
        self.dir.cleanup()

    def post(self, job_id, payload):
        import urllib.request
        req = urllib.request.Request(f"{self.base}/api/job/{job_id}/triage",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def site_status(self, job_id):
        return self.site.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]

    def test_off_by_default(self):
        body = self.post("a1", {"status": "shortlisted"})
        self.assertNotIn("mirrored", body)
        self.assertEqual(self.site_status("a1"), "discovered")

    def test_mirrors_when_switched_on(self):
        self.web.Handler.mirror = True
        body = self.post("a1", {"status": "submitted", "note": "applied from laptop"})
        self.assertTrue(body["mirrored"])
        row = self.site.execute("SELECT status, notes FROM jobs WHERE job_id='a1'").fetchone()
        self.assertEqual(tuple(row), ("submitted", "applied from laptop"))

    def test_job_not_on_site_yet_is_reported_not_raised(self):
        self.web.Handler.mirror = True
        body = self.post("c3", {"status": "shortlisted"})
        self.assertEqual((body["ok"], body["mirrored"]), (True, False))
        self.assertIn("not on the hosted site", body["mirror_error"])


class Dispatch(unittest.TestCase):
    """Run poll on the hosted site queues the GitHub workflow."""

    def setUp(self):
        self.calls = []
        test = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.calls.append((self.path, self.headers["Authorization"], body))
                self.send_response(204)
                self.end_headers()

        self.gh = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.gh.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.env = mock.patch.dict(os.environ, {
            "GITHUB_DISPATCH_TOKEN": "ghp_test",
            "GITHUB_API_URL": f"http://127.0.0.1:{self.gh.server_address[1]}",
            "GITHUB_REPO": "me/repo"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.gh.shutdown()
        self.gh.server_close()

    def test_dispatch_queues_the_workflow(self):
        code, _, body = call("POST", "/api/poll", {})
        self.assertEqual((code, body["started"]), (200, True))
        self.assertEqual(self.calls, [("/repos/me/repo/actions/workflows/poll.yml/dispatches",
                                       "Bearer ghp_test", {"ref": "main"})])


if __name__ == "__main__":
    unittest.main()
