"""Local web UI. Stdlib http.server, bound to loopback, single user, no auth.

Serves one HTML page plus a small JSON API over the existing jobs.db. Nothing
here writes to the pipeline's ingest path; the only mutation is triage
(status + note), which the CLI and later phases read back out of `jobs.status`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import apply, db, ingest
from .util import now_iso

UI_DIR = Path(__file__).with_name("ui")

# Tier-1 reject reasons are free-form strings prefixed by the rule that fired.
# The UI groups them into bins, in the order they appear in the legend.
BINS = [
    ("pass", "PASS", None),
    ("title", "TITLE", ("title-deny:", "title-not-allowed")),
    ("internship", "INTERNSHIP", ("internship:",)),
    ("references", "REFERENCES", ("references:",)),
    ("location", "LOCATION", ("location:", "location-deny:", "location-unknown")),
    ("experience", "EXPERIENCE", ("yoe:",)),
    ("phrase", "CLEARANCE", ("phrase:",)),
    ("grad", "GRAD WINDOW", ("grad-window:",)),
    ("stale", "STALE", ("stale:",)),
    ("ghost", "GHOST", ("ghost:",)),
]
BIN_BY_KEY = {key: (label, prefixes) for key, label, prefixes in BINS}

# Triage states. The plan's Stage 6 machine starts at `discovered`; these two
# extend the front of it rather than replacing anything downstream.
TRIAGE_STATES = {"discovered", "shortlisted", "submitted", "dismissed"}

_OPEN = "is_open=1 AND duplicate_of IS NULL"


class Poller:
    """Runs an ingest in a background thread so the page can show progress.

    One at a time, process-wide. The scheduled task is a separate process and
    cannot be locked out from here; `PRAGMA busy_timeout` in db.connect is what
    keeps the two from colliding.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = {"running": False, "done": 0, "total": 0, "new": 0,
                      "updated": 0, "closed": 0, "failed": 0, "started_at": None,
                      "finished_at": None, "error": None, "source": None}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def start(self, db_path=None) -> tuple[bool, str]:
        with self._lock:
            if self.state["running"]:
                return False, "a poll is already running"
            self.state.update({
                "running": True, "done": 0, "total": 0, "new": 0, "updated": 0,
                "closed": 0, "failed": 0, "scored": 0, "score_errors": 0,
                "started_at": now_iso(),
                "finished_at": None, "error": None, "source": None})
        self._thread = threading.Thread(target=self._run, args=(db_path,), daemon=True)
        self._thread.start()
        return True, "started"

    def _targets(self) -> list[tuple[str, str]]:
        from .config import companies, lists
        from .sources import VENDORS, simplify
        targets = [(c["ats"], c["slug"]) for c in companies() if c.get("ats") in VENDORS]
        targets += [(simplify.VENDOR, key) for key in (lists() or simplify.LISTS)]
        return targets

    def _run(self, db_path) -> None:
        from . import dedup, filters, ingest
        conn = None
        try:
            targets = self._targets()
            with self._lock:
                self.state["total"] = len(targets)
            conn = db.connect(db_path) if db_path else db.connect()

            for res in ingest.ingest_all(conn, targets, workers=12, timeout=30):
                with self._lock:
                    self.state["done"] += 1
                    self.state["new"] += res.inserted
                    self.state["updated"] += res.updated
                    self.state["closed"] += res.closed
                    self.state["failed"] += 0 if res.ok else 1
                    self.state["source"] = res.source_key

            with self._lock:
                self.state["source"] = "deduplicating"
            dedup.rebuild(conn)
            with self._lock:
                self.state["source"] = "filtering"
            filters.apply(conn, only_new=True)

            # Score whatever just cleared Tier-1, so a posting found at 04:00
            # is ranked by the time anyone looks. No-ops without an API key.
            from . import score as _score
            if _score.has_credentials():
                with self._lock:
                    self.state["source"] = "scoring"
                result = _score.score_pending(conn)
                with self._lock:
                    self.state["scored"] = result.scored
                    if result.errored:
                        self.state["score_errors"] = result.errored
        except Exception as e:  # noqa: BLE001 - the thread must report, not vanish
            with self._lock:
                self.state["error"] = f"{type(e).__name__}: {e}"
        finally:
            if conn is not None:
                conn.close()
            with self._lock:
                self.state["running"] = False
                self.state["finished_at"] = now_iso()
                self.state["source"] = None


POLLER = Poller()


class PendingApply:
    """The job whose application the browser extension should fill next.

    Clicking Auto Apply parks the job here and opens the form. The extension's
    content script, running on the ATS page, asks what is pending and fills it.
    That indirection exists because a page served from 127.0.0.1 cannot touch a
    form on greenhouse.io — same-origin policy, not a missing feature.
    """

    TTL_SECONDS = 600

    def __init__(self):
        self._lock = threading.Lock()
        self._job_id: str | None = None
        self._set_at: float = 0.0

    def set(self, job_id: str) -> None:
        with self._lock:
            self._job_id, self._set_at = job_id, time.monotonic()

    def clear(self) -> None:
        with self._lock:
            self._job_id, self._set_at = None, 0.0

    def get(self) -> str | None:
        with self._lock:
            if not self._job_id:
                return None
            # A stale slot would fill a form the user opened hours later.
            if time.monotonic() - self._set_at > self.TTL_SECONDS:
                self._job_id = None
                return None
            return self._job_id


PENDING = PendingApply()


def _bin_sql(key: str) -> tuple[str, list]:
    """WHERE fragment for one bin. Kept server-side so the UI never builds SQL."""
    if key in ("all", ""):
        return "", []
    if key == "pass":
        return "filter_verdict='pass'", []
    entry = BIN_BY_KEY.get(key)
    if not entry or not entry[1]:
        return "", []
    prefixes = entry[1]
    clause = " OR ".join("filter_reason LIKE ?" for _ in prefixes)
    return f"({clause})", [f"{p}%" for p in prefixes]


class Api:
    """Query layer. One connection per request, opened by the handler."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def summary(self) -> dict:
        """One grouped pass over the covering index, then tallied in Python.

        The obvious shape — one COUNT(*) per bin — was eight full table scans of
        rows holding kilobytes of description each, and took 2.8 seconds.
        """
        c = self.conn
        rows = c.execute(
            f"SELECT filter_verdict, filter_reason, status, "
            f"       SUM(CASE WHEN seen_count=1 THEN 1 ELSE 0 END) AS fresh, "
            f"       COUNT(*) AS n "
            f"FROM jobs WHERE {_OPEN} "
            f"GROUP BY filter_verdict, filter_reason, status").fetchall()

        tally = {key: 0 for key, _, _ in BINS}
        triage: dict[str, int] = {}
        openn = fresh = passed = 0
        for r in rows:
            openn += r["n"]
            triage[r["status"]] = triage.get(r["status"], 0) + r["n"]
            if r["filter_verdict"] == "pass":
                tally["pass"] += r["n"]
                passed += r["n"]
                fresh += r["fresh"]
                continue
            reason = r["filter_reason"] or ""
            for key, _, prefixes in BINS:
                if prefixes and reason.startswith(prefixes):
                    tally[key] += r["n"]
                    break
            else:
                tally["unbinned"] = tally.get("unbinned", 0) + r["n"]

        labels = dict((key, label) for key, label, _ in BINS)
        labels["unbinned"] = "UNBINNED"
        counts = [{"key": k, "label": labels[k], "count": tally[k]}
                  for k, _, _ in BINS]
        if tally.get("unbinned"):
            counts.append({"key": "unbinned", "label": "UNBINNED",
                           "count": tally["unbinned"]})

        meta = c.execute(
            "SELECT (SELECT COUNT(*) FROM jobs) AS total, "
            "       (SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NOT NULL) AS dupes, "
            "       (SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL) AS scored, "
            "       (SELECT MAX(run_at) FROM source_runs) AS last_run, "
            "       (SELECT COUNT(DISTINCT source_key) FROM source_runs) AS sources"
        ).fetchone()

        return {
            "total": meta["total"],
            "open": openn,
            "passed": passed,
            "yield_pct": round(passed / openn * 100, 2) if openn else 0.0,
            "duplicates": meta["dupes"],
            "fresh": fresh,
            "scored": meta["scored"],
            "bins": counts,
            "sources": meta["sources"],
            "last_run": meta["last_run"],
            "alerts": ingest.health(c),
            "triage": triage,
        }

    def jobs(self, q: dict) -> dict:
        where = [_OPEN]
        params: list = []

        bin_where, bin_params = _bin_sql(q.get("bin", ["pass"])[0])
        if bin_where:
            where.append(bin_where)
            params += bin_params

        search = (q.get("q", [""])[0] or "").strip()
        if search:
            where.append("(company LIKE ? OR title LIKE ? OR location LIKE ?)")
            params += [f"%{search}%"] * 3

        status = q.get("status", [""])[0]
        if status and status != "all":
            where.append("status=?")
            params.append(status)

        if q.get("fresh", [""])[0] == "1":
            where.append("seen_count=1")

        limit = min(int(q.get("limit", ["120"])[0] or 120), 500)
        offset = max(int(q.get("offset", ["0"])[0] or 0), 0)

        clause = " AND ".join(where)
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {clause}", params).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT job_id, company, title, location, remote_flag, ats_vendor, "
            f"posted_at, first_seen_at, seen_count, repost_count, fit_score, "
            f"status, filter_verdict, filter_reason, url "
            f"FROM jobs WHERE {clause} "
            f"ORDER BY fit_score IS NULL, fit_score DESC, "
            f"COALESCE(posted_at, first_seen_at) DESC LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "jobs": [dict(r) for r in rows]}

    def job(self, job_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        job["events"] = [dict(e) for e in self.conn.execute(
            "SELECT at, kind, detail FROM events WHERE job_id=? "
            "ORDER BY id DESC LIMIT 12", (job_id,))]
        if job.get("duplicate_of"):
            dup = self.conn.execute(
                "SELECT company, title, ats_vendor, url FROM jobs WHERE job_id=?",
                (job["duplicate_of"],)).fetchone()
            job["canonical"] = dict(dup) if dup else None
        job["also_seen"] = [dict(r) for r in self.conn.execute(
            "SELECT job_id, ats_vendor, url FROM jobs WHERE duplicate_of=? LIMIT 6",
            (job_id,))]
        return job

    def triage(self, job_id: str, payload: dict) -> dict:
        row = self.conn.execute(
            "SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)

        status = payload.get("status")
        note = payload.get("note")
        changed = []

        if status is not None:
            if status not in TRIAGE_STATES:
                raise ValueError(
                    f"status must be one of {sorted(TRIAGE_STATES)}, got {status!r}")
            if status != row["status"]:
                self.conn.execute("UPDATE jobs SET status=? WHERE job_id=?",
                                  (status, job_id))
                db.log_event(self.conn, job_id, status, f"was {row['status']}")
                changed.append("status")

        if note is not None:
            self.conn.execute("UPDATE jobs SET notes=? WHERE job_id=?",
                              (note.strip() or None, job_id))
            changed.append("note")

        self.conn.commit()
        return {"ok": True, "changed": changed, "at": now_iso()}

    def sources(self) -> dict:
        rows = self.conn.execute(
            "SELECT source_key, ats_vendor, ok, fetched, inserted, http_status, "
            "duration_ms, run_at, error FROM source_runs "
            "WHERE id IN (SELECT MAX(id) FROM source_runs GROUP BY source_key) "
            "ORDER BY ok ASC, fetched ASC").fetchall()
        return {"alerts": ingest.health(self.conn),
                "sources": [dict(r) for r in rows]}

    def rejects(self) -> dict:
        rows = self.conn.execute(
            f"SELECT filter_reason, COUNT(*) n FROM jobs "
            f"WHERE {_OPEN} AND filter_verdict='reject' AND filter_reason IS NOT NULL "
            f"GROUP BY filter_reason ORDER BY n DESC LIMIT 40").fetchall()
        return {"reasons": [dict(r) for r in rows]}


_ID_RE = re.compile(r"^[0-9a-f]{1,40}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "jobpipe"
    db_path: Path | None = None

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter than the default access log
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _api(self):
        conn = db.connect(self.db_path) if self.db_path else db.connect()
        return conn, Api(conn)

    # -- routing ----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        route, query = parsed.path, parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if route == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")

        if route == "/debug/fill.js":
            # Served as JavaScript so a page can pull it in with a <script>
            # tag, which is not subject to CORS. Debug only: it is how the
            # shipping fill engine gets exercised against real ATS forms.
            job = (query.get("job") or [""])[0]
            if not re.fullmatch(r"[0-9a-f]{1,40}", job):
                return self._error(400, "bad job id")
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
            import debug_fill
            try:
                body = debug_fill.blob(job, with_resume="stub")
            except SystemExit as e:
                return self._error(404, str(e))
            return self._send(200, body.encode("utf-8"),
                              "application/javascript; charset=utf-8")

        if not route.startswith("/api/"):
            return self._error(404, f"no route {route}")

        conn, api = self._api()
        try:
            if route == "/api/resume":
                # Served so the extension can turn it into a File object. A
                # content script cannot read the disk, but it can be handed
                # bytes and build a File from them, which is what makes the
                # resume upload automatable after all.
                f = apply.facts()
                rel = (f.get("documents") or {}).get("resume")
                path = (apply.ROOT / rel) if rel else None
                if not path or not path.exists():
                    return self._error(404, f"no resume at {rel}")
                return self._send(200, path.read_bytes(), "application/pdf")

            if route == "/api/apply/pending":
                job_id = PENDING.get()
                if not job_id:
                    return self._json({"pending": False})
                row = conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                   (job_id,)).fetchone()
                if row is None:
                    PENDING.clear()
                    return self._json({"pending": False})
                packet = apply.build(dict(row))
                payload = packet.to_dict()
                payload["pending"] = True
                return self._json(payload)

            if route == "/api/poll":
                return self._json(POLLER.snapshot())
            if route == "/api/summary":
                return self._json(api.summary())
            if route == "/api/jobs":
                return self._json(api.jobs(query))
            if route == "/api/sources":
                return self._json(api.sources())
            if route == "/api/rejects":
                return self._json(api.rejects())
            fill = re.fullmatch(r"/api/job/([0-9a-f]{1,40})/fillpacket", route)
            if fill:
                row = conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                   (fill.group(1),)).fetchone()
                if row is None:
                    return self._error(404, "no such job")
                packet = apply.build(dict(row))
                payload = packet.to_dict()
                payload["instructions"] = apply.instructions(packet)
                return self._json(payload)

            if route.startswith("/api/job/"):
                job_id = route[len("/api/job/"):]
                if not _ID_RE.match(job_id):
                    return self._error(400, "malformed job id")
                job = api.job(job_id)
                return self._json(job) if job else self._error(404, "no such job")
            return self._error(404, f"no route {route}")
        except (sqlite3.Error, ValueError) as e:
            return self._error(500, f"{type(e).__name__}: {e}")
        finally:
            conn.close()

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_POST(self):  # noqa: N802
        route = urlparse(self.path).path

        if route == "/api/apply/done":
            PENDING.clear()
            return self._json({"ok": True})

        if route == "/api/apply/pending":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, f"bad body: {e}")
            job_id = (body or {}).get("job_id", "")
            if not _ID_RE.match(str(job_id)):
                return self._error(400, "malformed job id")
            PENDING.set(job_id)
            return self._json({"ok": True, "job_id": job_id})

        if route == "/api/poll":
            started, message = POLLER.start(self.db_path)
            return self._json({"started": started, "message": message,
                               **POLLER.snapshot()}, 200 if started else 409)

        match = re.fullmatch(r"/api/job/([0-9a-f]{1,40})/triage", route)
        if not match:
            return self._error(404, f"no route {route}")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error(400, "bad Content-Length")
        if length > 64_000:
            return self._error(413, "payload too large")

        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as e:
            return self._error(400, f"bad JSON: {e}")
        if not isinstance(payload, dict):
            return self._error(400, "body must be a JSON object")

        conn, api = self._api()
        try:
            return self._json(api.triage(match.group(1), payload))
        except KeyError:
            return self._error(404, "no such job")
        except ValueError as e:
            return self._error(400, str(e))
        except sqlite3.Error as e:
            return self._error(500, f"{type(e).__name__}: {e}")
        finally:
            conn.close()

    # -- static -----------------------------------------------------------
    def _file(self, name: str, ctype: str) -> None:
        path = UI_DIR / name
        if not path.exists():
            return self._error(404, f"missing {name}")
        self._send(200, path.read_bytes(), ctype)


def serve(host: str = "127.0.0.1", port: int = 8765, db_path: Path | None = None,
          open_browser: bool = True) -> None:
    Handler.db_path = db_path
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"jobpipe ui  {url}")
    print("ctrl-c to stop")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
