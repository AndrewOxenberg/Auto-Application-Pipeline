"""Ingest orchestration: fetch -> parse -> upsert -> close-missing -> log run.

Fetching is parallel, writing is not. SQLite allows exactly one writer, and
twelve threads racing on a single file produced `database is locked` on the
first full run. The network is the slow part, so parallel fetch plus a serial
writer keeps the speed and drops the contention.
"""
from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable

from . import db
from .http import FetchError, get_json
from .sources import VENDORS, simplify


@dataclass
class RunResult:
    source_key: str
    vendor: str = ""
    slug: str = ""
    ok: bool = False
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    closed: int = 0
    http_status: int | None = None
    duration_ms: int = 0
    error: str | None = None
    jobs: list = field(default_factory=list, repr=False)

    @property
    def empty(self) -> bool:
        """HTTP 200 with zero rows. The silent-breakage signature."""
        return self.ok and self.fetched == 0


def _module(vendor: str):
    return simplify if vendor == simplify.VENDOR else VENDORS.get(vendor)


def fetch_source(vendor: str, slug: str, timeout: int = 30) -> RunResult:
    """Network + parse only. Safe to run in a thread; touches no database."""
    key = f"{vendor}:{slug}"
    module = _module(vendor)
    if module is None:
        return RunResult(key, vendor, slug, error=f"unknown vendor {vendor!r}")

    started = time.monotonic()
    try:
        payload, status = get_json(module.url(slug), timeout=timeout)
    except FetchError as e:
        return RunResult(key, vendor, slug, http_status=e.status, error=str(e),
                         duration_ms=int((time.monotonic() - started) * 1000))
    except KeyError:
        return RunResult(key, vendor, slug, error=f"unknown list {slug!r}")

    try:
        jobs = module.parse(payload, slug)
    except Exception as e:  # noqa: BLE001 - a shape change must not kill the run
        return RunResult(key, vendor, slug, http_status=status,
                         error=f"parse failed: {type(e).__name__}: {e}",
                         duration_ms=int((time.monotonic() - started) * 1000))

    return RunResult(key, vendor, slug, ok=True, fetched=len(jobs), http_status=status,
                     duration_ms=int((time.monotonic() - started) * 1000), jobs=jobs)


def persist(conn: sqlite3.Connection, res: RunResult) -> RunResult:
    """Write one fetched source into the database. Main thread only."""
    if res.ok:
        for job in res.jobs:
            verdict = db.upsert_job(conn, job)
            res.inserted += verdict == "inserted"
            res.updated += verdict == "updated"
        # Deliberately skip close-missing on an empty payload: a stale slug
        # returning 200 + [] would otherwise close every job for this company.
        if res.jobs:
            res.closed = db.close_missing(conn, res.source_key,
                                          {j["job_id"] for j in res.jobs})
    db.record_run(conn, source_key=res.source_key, ats_vendor=res.vendor,
                  company_slug=res.slug, ok=int(res.ok), http_status=res.http_status,
                  fetched=res.fetched, inserted=res.inserted, updated=res.updated,
                  duration_ms=res.duration_ms, error=res.error)
    conn.commit()
    res.jobs = []  # a full board is megabytes; do not hold them all in memory
    return res


def ingest_source(conn: sqlite3.Connection, vendor: str, slug: str,
                  timeout: int = 30) -> RunResult:
    return persist(conn, fetch_source(vendor, slug, timeout=timeout))


def ingest_all(conn: sqlite3.Connection, targets: Iterable[tuple[str, str]],
               workers: int = 8, timeout: int = 30):
    """Yield each RunResult as it lands, already persisted."""
    targets = list(targets)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_source, v, s, timeout) for v, s in targets]
        for fut in futures:
            yield persist(conn, fut.result())


_HEALTH_SQL = """
WITH ranked AS (
    SELECT source_key, ok, fetched, error, run_at,
           ROW_NUMBER() OVER (PARTITION BY source_key ORDER BY id DESC) AS rn
    FROM source_runs
)
SELECT source_key,
       COUNT(*)                                  AS runs,
       SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END)   AS failed,
       SUM(CASE WHEN fetched = 0 THEN 1 ELSE 0 END) AS zero,
       MIN(run_at)                               AS since,
       MAX(CASE WHEN rn = 1 THEN error END)      AS last_error
FROM ranked
WHERE rn <= ?
GROUP BY source_key
HAVING runs = ?
"""


def health(conn: sqlite3.Connection, window: int = 2) -> list[dict]:
    """Sources that failed or returned zero rows on the last `window` runs.

    One windowed query rather than two per source: at 528 sources the per-source
    version cost over a thousand round trips and made the web UI's first paint
    visibly late.
    """
    alerts = []
    for r in conn.execute(_HEALTH_SQL, (window, window)):
        if r["failed"] == r["runs"]:
            alerts.append({"source": r["source_key"], "kind": "failing",
                           "detail": r["last_error"], "since": r["since"]})
        elif r["zero"] == r["runs"]:
            alerts.append({"source": r["source_key"], "kind": "empty",
                           "detail": "HTTP 200 with 0 jobs — slug likely stale",
                           "since": r["since"]})
    return sorted(alerts, key=lambda a: a["source"])
