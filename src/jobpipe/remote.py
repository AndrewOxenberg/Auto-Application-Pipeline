"""Turso (libSQL) over its HTTP API, shaped like the slice of sqlite3 we use.

Feature 20. The hosted site keeps its data in Turso, which speaks SQLite, so
`web.Api` runs unchanged against this connection: `execute()` returns a cursor
with fetchone/fetchall, rows index by name or position, and `dict(row)` works.
Stdlib only, like the rest of the pipeline.

Writes are queued until `commit()` and sent as one transactional batch, so a
triage (status UPDATE plus an events INSERT) lands whole or not at all. A read
flushes the queue first, so it always sees earlier writes.

Protocol: Hrana over HTTP, `POST /v2/pipeline` (docs.turso.tech/sdk/http/reference).
Integers travel as strings, floats as JSON numbers, blobs as base64.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import urllib.error
import urllib.request


class RemoteError(Exception):
    """The server rejected a statement, or the request itself failed."""


class Row:
    """sqlite3.Row lookalike: r[0], r["col"], keys(), dict(r)."""

    __slots__ = ("_cols", "_vals", "_index")

    def __init__(self, cols: list[str], vals: list, index: dict[str, int]):
        self._cols, self._vals, self._index = cols, vals, index

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._vals[key]
        return self._vals[self._index[key]]

    def keys(self) -> list[str]:
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self) -> int:
        return len(self._vals)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._cols, self._vals))!r})"


class Cursor:
    def __init__(self, result: dict | None):
        result = result or {}
        cols = [c.get("name") for c in result.get("cols", [])]
        index = {name: i for i, name in enumerate(cols)}
        self._rows = [Row(cols, [_decode(v) for v in r], index)
                      for r in result.get("rows", [])]
        self._pos = 0
        self.description = tuple((c, None, None, None, None, None, None) for c in cols)
        self.rowcount = result.get("affected_row_count", -1)
        rowid = result.get("last_insert_rowid")
        self.lastrowid = int(rowid) if rowid is not None else None
        self.rows_read = result.get("rows_read", 0)
        self.rows_written = result.get("rows_written", 0)

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list[Row]:
        rest = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


def _encode(v) -> dict:
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode("ascii")}
    return {"type": "text", "value": str(v)}


def _decode(v: dict):
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":
        return base64.b64decode(v.get("base64", ""))
    return v.get("value")


def _stmt(sql: str, params=()) -> dict:
    if isinstance(params, dict):
        return {"sql": sql, "named_args": [
            {"name": k if k[:1] in ":@$" else ":" + k, "value": _encode(v)}
            for k, v in params.items()]}
    return {"sql": sql, "args": [_encode(p) for p in params]}


def _is_read(sql: str) -> bool:
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    return head in ("SELECT", "WITH", "PRAGMA", "EXPLAIN", "VALUES")


class Connection:
    """One logical connection. Each flush is its own HTTP request."""

    def __init__(self, url: str, token: str, timeout: float = 30.0):
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        self.endpoint = url.rstrip("/") + "/v2/pipeline"
        self.token = token
        self.timeout = timeout
        self._queue: list[dict] = []
        # Turso meters rows read and written, so keep a running tally to measure by.
        self.rows_read = 0
        self.rows_written = 0
        self.requests = 0

    # -- transport -----------------------------------------------------------

    def _post(self, requests: list[dict]) -> list[dict]:
        body = json.dumps({"requests": requests + [{"type": "close"}]}).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RemoteError(f"HTTP {e.code} from Turso: {detail}") from None
        except urllib.error.URLError as e:
            raise RemoteError(f"cannot reach Turso: {e.reason}") from None
        self.requests += 1
        results = payload.get("results", [])
        for r in results:
            if r.get("type") == "error":
                raise RemoteError(r.get("error", {}).get("message", "unknown error"))
        return results[:-1]   # drop the close

    def _tally(self, result: dict | None) -> None:
        if result:
            self.rows_read += result.get("rows_read", 0) or 0
            self.rows_written += result.get("rows_written", 0) or 0

    # -- sqlite3-shaped API ----------------------------------------------------

    def execute(self, sql: str, params=()) -> Cursor:
        if not _is_read(sql):
            self._queue.append(_stmt(sql, params))
            return Cursor(None)
        self.commit()
        res = self._post([{"type": "execute", "stmt": _stmt(sql, params)}])[0]
        result = res["response"]["result"]
        self._tally(result)
        return Cursor(result)

    def executemany(self, sql: str, seq) -> Cursor:
        for params in seq:
            self._queue.append(_stmt(sql, params))
        return Cursor(None)

    def commit(self) -> list[Cursor]:
        """Send queued writes as one transaction. Returns a cursor per write."""
        if not self._queue:
            return []
        queue, self._queue = self._queue, []
        return self.batch_stmts(queue)

    def rollback(self) -> None:
        self._queue = []

    def close(self) -> None:
        # Like sqlite3: closing without commit discards the uncommitted writes.
        self._queue = []

    def batch(self, statements: list[tuple[str, tuple | list | dict]]) -> list[Cursor]:
        """Run statements as one transaction, now. Reads allowed."""
        self.commit()
        return self.batch_stmts([_stmt(sql, params) for sql, params in statements])

    def batch_stmts(self, stmts: list[dict]) -> list[Cursor]:
        # BEGIN, each step only if the one before succeeded, COMMIT only if the
        # last did, ROLLBACK otherwise. The same shape the libSQL clients send.
        steps = [{"stmt": {"sql": "BEGIN"}}]
        for i, s in enumerate(stmts):
            steps.append({"stmt": s, "condition": {"type": "ok", "step": i}})
        n = len(steps)
        steps.append({"stmt": {"sql": "COMMIT"}, "condition": {"type": "ok", "step": n - 1}})
        steps.append({"stmt": {"sql": "ROLLBACK"},
                      "condition": {"type": "not", "cond": {"type": "ok", "step": n}}})
        res = self._post([{"type": "batch", "batch": {"steps": steps}}])[0]
        out = res["response"]["result"]
        errors = out.get("step_errors", [])
        for i, err in enumerate(errors):
            if err and 0 < i <= len(stmts):
                raise RemoteError(f"statement {i} failed, batch rolled back: "
                                  f"{err.get('message', err)}")
        results = out.get("step_results", [])[1:len(stmts) + 1]
        for r in results:
            self._tally(r)
        return [Cursor(r) for r in results]

    def executescript(self, script: str) -> None:
        """Run a schema file as one transaction. PRAGMAs are local-file concerns
        (WAL, busy_timeout) and are skipped."""
        stmts, buf = [], ""
        for line in script.splitlines(keepends=True):
            buf += line
            if sqlite3.complete_statement(buf):
                body = "\n".join(l for l in buf.splitlines()
                                 if not l.strip().startswith("--")).strip()
                if body and not body.upper().startswith("PRAGMA"):
                    stmts.append(body.rstrip(";").rstrip())
                buf = ""
        self.batch([(s, ()) for s in stmts])


def from_env(env=None, dotenv=None) -> Connection | None:
    """A connection from TURSO_DATABASE_URL and TURSO_AUTH_TOKEN, or None.

    `dotenv` names a KEY=VALUE file such as the .env.local that `vercel env pull`
    writes; the real environment wins over it."""
    env = dict(os.environ if env is None else env)
    if dotenv is not None and os.path.exists(dotenv):
        with open(dotenv, encoding="utf-8") as f:
            for line in f:
                key, sep, val = line.partition("=")
                if sep and not line.lstrip().startswith("#"):
                    env.setdefault(key.strip(), val.strip().strip('"'))
    url, token = env.get("TURSO_DATABASE_URL"), env.get("TURSO_AUTH_TOKEN")
    if not url or not token:
        return None
    return Connection(url, token)
