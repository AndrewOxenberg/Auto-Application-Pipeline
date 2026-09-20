"""A stand-in Turso server for offline tests. Feature 20.

Speaks the same Hrana-over-HTTP shape as Turso (`POST /v2/pipeline`) and runs
the SQL on a local sqlite3 file, so `remote.Connection` is exercised over a
real socket with real JSON. `rows_read` is approximated by rows returned.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _decode(v):
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        assert isinstance(v["value"], str), "Hrana sends integers as strings"
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":
        return base64.b64decode(v["base64"])
    return v["value"]


def _encode(x):
    if x is None:
        return {"type": "null"}
    if isinstance(x, int):
        return {"type": "integer", "value": str(x)}
    if isinstance(x, float):
        return {"type": "float", "value": x}
    if isinstance(x, bytes):
        return {"type": "blob", "base64": base64.b64encode(x).decode()}
    return {"type": "text", "value": x}


class FakeTurso:
    def __init__(self, db_path, token="test-token"):
        self.db_path = str(db_path)
        self.token = token
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                    isolation_level=None)
        self.requests = []   # every request body, for assertions
        self.fail_after = None   # set to N: request N+1 onward gets an HTTP 500
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                if self.path != "/v2/pipeline":
                    self.send_error(404)
                    return
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if fake.fail_after is not None and len(fake.requests) >= fake.fail_after:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error":"injected failure"}')
                    return
                fake.requests.append(body)
                with fake.lock:
                    results = [fake._handle(r) for r in body["requests"]]
                out = json.dumps({"baton": None, "base_url": None, "results": results}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.conn.close()

    # -- protocol ----------------------------------------------------------------

    def _run(self, stmt):
        if "named_args" in stmt:
            params = {a["name"].lstrip(":@$"): _decode(a["value"]) for a in stmt["named_args"]}
        else:
            params = [_decode(a) for a in stmt.get("args", [])]
        before = self.conn.total_changes
        cur = self.conn.execute(stmt["sql"], params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        written = self.conn.total_changes - before
        return {"cols": [{"name": c, "decltype": None} for c in cols],
                "rows": [[_encode(v) for v in r] for r in rows],
                "affected_row_count": written,
                "last_insert_rowid": str(cur.lastrowid) if cur.lastrowid else None,
                "rows_read": len(rows), "rows_written": written}

    def _cond(self, c, ok):
        t = c["type"]
        if t == "ok":
            return ok[c["step"]] is True
        if t == "error":
            return ok[c["step"]] is False
        if t == "not":
            return not self._cond(c["cond"], ok)
        if t == "and":
            return all(self._cond(x, ok) for x in c["conds"])
        if t == "or":
            return any(self._cond(x, ok) for x in c["conds"])
        raise ValueError(t)

    def _handle(self, req):
        if req["type"] == "close":
            return {"type": "ok", "response": {"type": "close"}}
        if req["type"] == "execute":
            try:
                return {"type": "ok", "response": {"type": "execute",
                                                   "result": self._run(req["stmt"])}}
            except sqlite3.Error as e:
                return {"type": "error", "error": {"message": str(e), "code": "SQLITE_ERROR"}}
        if req["type"] == "batch":
            steps = req["batch"]["steps"]
            ok, results, errors = [], [], []
            for step in steps:
                if "condition" in step and not self._cond(step["condition"], ok):
                    ok.append(None); results.append(None); errors.append(None)
                    continue
                try:
                    results.append(self._run(step["stmt"])); errors.append(None); ok.append(True)
                except sqlite3.Error as e:
                    results.append(None); errors.append({"message": str(e)}); ok.append(False)
            return {"type": "ok", "response": {"type": "batch", "result": {
                "step_results": results, "step_errors": errors}}}
        return {"type": "error", "error": {"message": f"unknown request {req['type']}"}}
