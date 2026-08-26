"""SQLite access. One connection, explicit transactions, no ORM."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .util import now_iso

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "jobs.db"
SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # The scheduled task and a refresh clicked in the UI are separate processes
    # writing the same file. Without this, whichever loses the race dies with
    # "database is locked" instead of waiting its turn.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def log_event(conn: sqlite3.Connection, job_id: str, kind: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO events (job_id, at, kind, detail) VALUES (?,?,?,?)",
        (job_id, now_iso(), kind, detail),
    )


UPSERT_COLUMNS = (
    "company company_slug title location remote_flag req_id url apply_url "
    "ats_vendor department employment_type description_text posted_at content_hash "
    "dedup_key"
).split()


def upsert_job(conn: sqlite3.Connection, job: dict) -> str:
    """Insert a job or refresh an existing one. Returns 'inserted' | 'updated'."""
    now = now_iso()
    row = conn.execute(
        "SELECT job_id, is_open, seen_count, repost_count, content_hash "
        "FROM jobs WHERE job_id=?", (job["job_id"],)
    ).fetchone()

    if row is None:
        cols = ["job_id", *UPSERT_COLUMNS, "first_seen_at", "last_seen_at"]
        vals = [job["job_id"]] + [job.get(c) for c in UPSERT_COLUMNS] + [now, now]
        conn.execute(
            f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
        log_event(conn, job["job_id"], "discovered", job.get("url", ""))
        return "inserted"

    # A req that closed and came back is the ghost-job signal.
    reposted = 1 if row["is_open"] == 0 else 0
    sets = ", ".join(f"{c}=?" for c in UPSERT_COLUMNS)
    conn.execute(
        f"UPDATE jobs SET {sets}, last_seen_at=?, seen_count=seen_count+1, "
        f"repost_count=repost_count+?, is_open=1 WHERE job_id=?",
        [job.get(c) for c in UPSERT_COLUMNS] + [now, reposted, job["job_id"]],
    )
    if reposted:
        log_event(conn, job["job_id"], "reposted", f"repost #{row['repost_count'] + 1}")
    if row["content_hash"] != job.get("content_hash"):
        # Invalidate the Tier-2 cache: the posting text actually changed.
        conn.execute(
            "UPDATE jobs SET fit_score=NULL, scored_at=NULL, score_json=NULL "
            "WHERE job_id=?", (job["job_id"],))
    return "updated"


def close_missing(conn: sqlite3.Connection, source_key: str, seen_ids: set[str]) -> int:
    """Mark rows from this source that the endpoint no longer returns."""
    vendor, _, slug = source_key.partition(":")
    rows = conn.execute(
        "SELECT job_id FROM jobs WHERE ats_vendor=? AND company_slug=? AND is_open=1",
        (vendor, slug),
    ).fetchall()
    gone = [r["job_id"] for r in rows if r["job_id"] not in seen_ids]
    for jid in gone:
        conn.execute("UPDATE jobs SET is_open=0 WHERE job_id=?", (jid,))
        log_event(conn, jid, "closed", source_key)
    return len(gone)


def record_run(conn: sqlite3.Connection, **kw) -> None:
    kw.setdefault("run_at", now_iso())
    cols = list(kw)
    conn.execute(
        f"INSERT INTO source_runs ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [kw[c] for c in cols],
    )
