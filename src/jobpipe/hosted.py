"""The hosted site: a WSGI app for Vercel's Python runtime. Feature 20.

Same `web.Api` as the local server, over Turso instead of the SQLite file.
What is deliberately not here: the resume, fill packets and Auto Apply (they
read profile/facts.yaml, which never leaves the laptop), and the local poll
thread (polls run on GitHub Actions). The password check lives in
middleware.ts, in front of every request, including this one.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import parse_qs

from . import remote
from .web import Api, UI_DIR

_ID = r"[0-9a-f]{1,40}"
LOCAL_ONLY = re.compile(rf"^/(api/resume|api/apply/.*|api/job/{_ID}/fillpacket|debug/.*)$")
MAX_BODY = 64_000
WORKFLOW = "poll.yml"


class HttpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _site():
    conn = remote.from_env(os.environ)
    if conn is None:
        raise HttpError(503, "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are not set")
    return conn


def _index() -> bytes:
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("</head>", "<script>window.JOBPIPE_HOSTED = true;</script>\n</head>", 1
                        ).encode("utf-8")


def _poll_status(conn) -> dict:
    last = conn.execute("SELECT MAX(run_at) FROM source_runs").fetchone()[0]
    return {"hosted": True, "running": False, "last_run": last,
            "can_dispatch": bool(os.environ.get("GITHUB_DISPATCH_TOKEN"))}


def _dispatch() -> dict:
    """Queue the poll workflow on GitHub Actions. Needs a fine-grained token with
    Actions: read and write on the one repo, in GITHUB_DISPATCH_TOKEN."""
    token = os.environ.get("GITHUB_DISPATCH_TOKEN")
    repo = os.environ.get("GITHUB_REPO", "AndrewOxenberg/Auto-Application-Pipeline")
    if not token:
        raise HttpError(501, "polls run on GitHub Actions every 4 hours; "
                             "set GITHUB_DISPATCH_TOKEN to queue one from here")
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    req = urllib.request.Request(
        f"{base}/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches",
        data=json.dumps({"ref": os.environ.get("GITHUB_REF_NAME", "main")}).encode(),
        method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "jobpipe-hosted",
        })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = resp.status == 204
    except urllib.error.HTTPError as e:
        raise HttpError(502, f"GitHub said {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as e:
        raise HttpError(502, f"cannot reach GitHub: {e.reason}")
    if not ok:
        raise HttpError(502, "GitHub did not accept the dispatch")
    return {"hosted": True, "started": True, "running": False, "can_dispatch": True,
            "message": "poll queued on GitHub Actions, results in about 5 minutes"}


def _body(environ) -> dict:
    # Cross-site forms cannot send application/json without a CORS preflight,
    # which this site never approves. That keeps a malicious page from triaging.
    ctype = (environ.get("CONTENT_TYPE") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HttpError(415, "send application/json")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise HttpError(400, "bad Content-Length")
    if length > MAX_BODY:
        raise HttpError(413, "payload too large")
    try:
        payload = json.loads(environ["wsgi.input"].read(length) or b"{}")
    except json.JSONDecodeError as e:
        raise HttpError(400, f"bad JSON: {e}")
    if not isinstance(payload, dict):
        raise HttpError(400, "body must be a JSON object")
    return payload


def route(method: str, path: str, query: dict, environ: dict):
    """Returns (status, content type, body bytes)."""
    if method in ("GET", "HEAD") and path in ("/", "/index.html"):
        return 200, "text/html; charset=utf-8", _index()
    if path == "/favicon.ico":
        return 204, "image/x-icon", b""
    if LOCAL_ONLY.match(path):
        raise HttpError(404, "not available on the hosted site; use the laptop for this")
    if not path.startswith("/api/"):
        raise HttpError(404, f"no route {path}")

    if method == "POST":
        if path == "/api/poll":
            return 200, None, _dispatch()
        m = re.fullmatch(rf"/api/job/({_ID})/triage", path)
        if not m:
            raise HttpError(404, f"no route {path}")
        payload = _body(environ)
        conn = _site()
        try:
            return 200, None, Api(conn).triage(m.group(1), payload)
        except KeyError:
            raise HttpError(404, "no such job")
        except ValueError as e:
            raise HttpError(400, str(e))

    if method not in ("GET", "HEAD"):
        raise HttpError(405, f"{method} not allowed")
    conn = _site()
    api = Api(conn)
    if path == "/api/summary":
        return 200, None, api.summary()
    if path == "/api/jobs":
        return 200, None, api.jobs(query)
    if path == "/api/sources":
        return 200, None, api.sources()
    if path == "/api/rejects":
        return 200, None, api.rejects()
    if path == "/api/poll":
        return 200, None, _poll_status(conn)
    m = re.fullmatch(r"/api/job/([^/]+)", path)
    if m:
        if not re.fullmatch(_ID, m.group(1)):
            raise HttpError(400, "malformed job id")
        job = api.job(m.group(1))
        if job is None:
            raise HttpError(404, "no such job")
        return 200, None, job
    raise HttpError(404, f"no route {path}")


_REASONS = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found",
            405: "Method Not Allowed", 413: "Payload Too Large",
            415: "Unsupported Media Type", 500: "Internal Server Error",
            501: "Not Implemented", 502: "Bad Gateway", 503: "Service Unavailable"}


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO") or "/"
    query = parse_qs(environ.get("QUERY_STRING", ""))
    try:
        code, ctype, body = route(method, path, query, environ)
    except HttpError as e:
        code, ctype, body = e.code, None, {"error": str(e)}
    except remote.RemoteError as e:
        code, ctype, body = 502, None, {"error": f"site store: {e}"}
    except (ValueError, TypeError) as e:
        code, ctype, body = 500, None, {"error": f"{type(e).__name__}: {e}"}
    if ctype is None:
        ctype = "application/json; charset=utf-8"
        body = json.dumps(body, default=str).encode("utf-8")
    start_response(f"{code} {_REASONS.get(code, 'Error')}", [
        ("Content-Type", ctype),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
    ])
    return [b"" if method == "HEAD" else body]
