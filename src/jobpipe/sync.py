"""Engine file <-> hosted site store. Feature 20.

The engine is the local SQLite file the ingest runs on. The site store is Turso,
which the hosted UI reads. Every poll rewrites ~45,000 rows in the engine, so
pushing it wholesale would spend Turso's monthly write allowance in days. Push
sends only rows whose site-visible columns changed, found by a per-row hash kept
in the engine's `sync_state` table.

Who owns what:
  - posting fields and scores belong to the engine. The scheduled poll runs the
    offline scorer, so a fit score is made there like any other field;
  - status and notes belong to the site: push writes them only when inserting a
    row the site has never seen, and pull copies the site's values back.

The engine never deletes jobs, so push never deletes either. `site_meta.epoch`
guards against a store that was reset: a mismatch forces a full push.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from . import db

SITE_OWNED = ("status", "notes")
# Changes on every poll for every open row. Refreshed whenever a row is pushed
# for another reason, but never a reason to push on their own.
CHURN = ("seen_count", "last_seen_at")
KEEP_RUNS = 3   # per source; ingest.health looks at the last 2

LOCAL_SQL = """
CREATE TABLE IF NOT EXISTS sync_state (
    job_id TEXT PRIMARY KEY,
    hash   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT);
"""
REMOTE_META_SQL = "CREATE TABLE IF NOT EXISTS site_meta (key TEXT PRIMARY KEY, value TEXT)"

# One HTTP request carries at most this much, so a batch of long descriptions
# stays a sane size.
BATCH_BYTES = 900_000
BATCH_STMTS = 250


def _columns(conn) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]


def _visible(row) -> bool:
    return row["is_open"] == 1 and row["duplicate_of"] is None


def row_hash(row, hashed: list[str]) -> str:
    # The UI flags NEW on seen_count == 1, so the step from 1 to 2 is a change
    # worth one push per posting. Higher counts are churn.
    vals = [row[c] for c in hashed] + [_visible(row), min(row["seen_count"] or 0, 2)]
    return hashlib.sha1(json.dumps(vals, default=str).encode()).hexdigest()[:20]


def _meta(conn, table: str, key: str):
    r = conn.execute(f"SELECT value FROM {table} WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def _set_meta(conn, table: str, key: str, value) -> None:
    conn.execute(f"INSERT INTO {table} (key, value) VALUES (?, ?) "
                 f"ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _batches(stmts):
    """Group (sql, params, tag) triples into size-bounded batches."""
    batch, size = [], 0
    for sql, params, tag in stmts:
        n = len(sql) + sum(len(str(p)) for p in params)
        if batch and (size + n > BATCH_BYTES or len(batch) >= BATCH_STMTS):
            yield batch
            batch, size = [], 0
        batch.append((sql, params, tag))
        size += n
    if batch:
        yield batch


def _ensure(engine: sqlite3.Connection, site) -> bool:
    """Schemas on both sides. Returns True when the site needs a full push."""
    engine.executescript(LOCAL_SQL)
    site.executescript(db.SCHEMA.read_text(encoding="utf-8"))
    site.execute(REMOTE_META_SQL)
    site.commit()
    # schema.sql only ever CREATEs, so a column added to it later would exist in
    # the engine but not the site, and every push would fail. Add what's missing.
    for table in ("jobs", "events", "source_runs"):
        have = {r[1] for r in site.execute(f"PRAGMA table_info({table})")}
        for r in engine.execute(f"PRAGMA table_info({table})"):
            if r[1] not in have:
                decl = f" {r[2]}" if r[2] else ""
                default = f" DEFAULT {r[4]}" if r[4] is not None else ""
                site.execute(f"ALTER TABLE {table} ADD COLUMN {r[1]}{decl}{default}")
    site.commit()
    remote_epoch = _meta(site, "site_meta", "epoch")
    if remote_epoch is None:
        remote_epoch = uuid.uuid4().hex
        _set_meta(site, "site_meta", "epoch", remote_epoch)
        site.commit()
    if _meta(engine, "sync_meta", "epoch") != remote_epoch:
        engine.execute("DELETE FROM sync_state")
        engine.execute("DELETE FROM sync_meta")
        _set_meta(engine, "sync_meta", "epoch", remote_epoch)
        engine.commit()
        return True
    return False


def _changed_jobs(engine, cols: list[str]) -> list[tuple]:
    """Pass 1: hash every row without its description. Cheap, and it never holds
    a read open while pass 2 writes sync_state."""
    hashed = [c for c in cols if c not in ("description_text", *SITE_OWNED, *CHURN)]
    state = dict(engine.execute("SELECT job_id, hash FROM sync_state").fetchall())
    out = []
    for row in engine.execute(f"SELECT {', '.join(hashed)}, seen_count FROM jobs").fetchall():
        h = row_hash(row, hashed)
        prev = state.get(row["job_id"])
        if prev == h:
            continue
        out.append((row["job_id"], h, prev is None))
    return out


def _job_statements(engine, cols: list[str], changed: list[tuple], chunk: int = 200):
    """Pass 2: fetch changed rows in chunks, descriptions included, as upserts."""
    insert_cols = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    posting = [c for c in cols if c not in ("job_id", "description_text", *SITE_OWNED)]
    base_set = ", ".join(f"{c}=excluded.{c}" for c in posting)
    # Open rows keep the site's copy of the text when the engine's was stripped;
    # closed and duplicate rows drop theirs, since no list reaches them.
    desc_set = ("description_text=CASE WHEN excluded.is_open=1 AND excluded.duplicate_of "
                "IS NULL THEN COALESCE(excluded.description_text, jobs.description_text) "
                "ELSE NULL END")
    sql = (f"INSERT INTO jobs ({insert_cols}) VALUES ({marks}) "
           f"ON CONFLICT(job_id) DO UPDATE SET {base_set}, {desc_set}")
    desc_i = cols.index("description_text")

    for i in range(0, len(changed), chunk):
        part = changed[i:i + chunk]
        rows = {r["job_id"]: r for r in engine.execute(
            f"SELECT {insert_cols} FROM jobs WHERE job_id IN "
            f"({','.join('?' for _ in part)})", [t[0] for t in part])}
        for tag in part:
            row = rows[tag[0]]
            vals = [row[c] for c in cols]
            if not _visible(row):
                vals[desc_i] = None
            yield sql, vals, tag


def push(engine: sqlite3.Connection, site, progress=None) -> dict:
    """Send the engine's changes to the site store. Safe to rerun after a failure:
    state only advances for batches the site confirmed."""
    full = _ensure(engine, site)
    cols = _columns(engine)
    stats = {"full": full, "jobs": 0, "new_jobs": 0, "events": 0, "runs": 0}
    w0 = site.rows_written

    changed = _changed_jobs(engine, cols)
    for batch in _batches(_job_statements(engine, cols, changed)):
        site.batch([(sql, params) for sql, params, _ in batch])
        engine.executemany(
            "INSERT INTO sync_state (job_id, hash) VALUES (?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET hash=excluded.hash",
            [(t[0], t[1]) for _, _, t in batch])
        engine.commit()
        stats["jobs"] += len(batch)
        stats["new_jobs"] += sum(1 for _, _, t in batch if t[2])
        if progress:
            progress(stats)

    # Events: the engine's ids are its own, the site mints its own for triage.
    last = int(_meta(engine, "sync_meta", "last_event_id") or 0)
    rows = engine.execute("SELECT id, job_id, at, kind, detail FROM events WHERE id > ? "
                          "ORDER BY id", (last,)).fetchall()
    stmts = [("INSERT INTO events (job_id, at, kind, detail) VALUES (?,?,?,?)",
              [r["job_id"], r["at"], r["kind"], r["detail"]], r["id"]) for r in rows]
    for batch in _batches(stmts):
        site.batch([(sql, params) for sql, params, _ in batch])
        _set_meta(engine, "sync_meta", "last_event_id", batch[-1][2])
        engine.commit()
        stats["events"] += len(batch)

    # Source runs: only the engine writes them, so its ids carry over, and the
    # site keeps just the last few per source.
    last = int(_meta(engine, "sync_meta", "last_run_id") or 0)
    rcols = [r[1] for r in engine.execute("PRAGMA table_info(source_runs)")]
    rows = engine.execute(
        f"SELECT {', '.join(rcols)} FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY "
        f"source_key ORDER BY id DESC) AS rn FROM source_runs) "
        f"WHERE rn <= ? AND id > ? ORDER BY id", (KEEP_RUNS, last)).fetchall()
    ins = (f"INSERT OR REPLACE INTO source_runs ({', '.join(rcols)}) "
           f"VALUES ({', '.join('?' for _ in rcols)})")
    stmts = [(ins, list(r), r["id"]) for r in rows]
    for batch in _batches(stmts):
        site.batch([(sql, params) for sql, params, _ in batch])
        _set_meta(engine, "sync_meta", "last_run_id", batch[-1][2])
        engine.commit()
        stats["runs"] += len(batch)
    if rows:
        site.batch([(
            "DELETE FROM source_runs WHERE id NOT IN (SELECT id FROM (SELECT id, "
            "ROW_NUMBER() OVER (PARTITION BY source_key ORDER BY id DESC) rn "
            "FROM source_runs) WHERE rn <= ?)", (KEEP_RUNS,))])

    stats["rows_written"] = site.rows_written - w0
    stats["requests"] = site.requests
    return stats


def pull(engine: sqlite3.Connection, site, page: int = 5000) -> dict:
    """Copy the site's triage into the engine, before an ingest, so rules like
    the per-company cap see decisions made on the phone."""
    _ensure(engine, site)
    cols = SITE_OWNED
    changed = {c: 0 for c in cols}
    rows_seen, after = 0, ""
    while True:
        rows = site.execute(
            f"SELECT job_id, {', '.join(cols)} FROM jobs WHERE job_id > ? "
            f"ORDER BY job_id LIMIT ?", (after, page)).fetchall()
        if not rows:
            break
        after = rows[-1]["job_id"]
        rows_seen += len(rows)
        mine = {r["job_id"]: r for r in engine.execute(
            f"SELECT job_id, {', '.join(cols)} FROM jobs WHERE job_id IN "
            f"({','.join('?' for _ in rows)})", [r["job_id"] for r in rows])}
        for r in rows:
            local = mine.get(r["job_id"])
            if local is None:
                continue
            diff = [c for c in cols if local[c] != r[c]]
            if not diff:
                continue
            engine.execute(
                f"UPDATE jobs SET {', '.join(f'{c}=?' for c in diff)} WHERE job_id=?",
                [r[c] for c in diff] + [r["job_id"]])
            for c in diff:
                changed[c] += 1
        engine.commit()
    return {"rows_read": rows_seen, **changed}


def strip(engine: sqlite3.Connection) -> int:
    """Drop descriptions nothing will read before the next fetch rewrites them:
    rejects, closed rows and duplicates. Takes the engine file from ~314 MB to
    ~52 MB. filters.apply knows to keep a stripped row's verdict."""
    cur = engine.execute(
        "UPDATE jobs SET description_text=NULL WHERE description_text IS NOT NULL "
        "AND (filter_verdict='reject' OR is_open=0 OR duplicate_of IS NOT NULL)")
    engine.commit()
    engine.execute("VACUUM")
    return cur.rowcount
