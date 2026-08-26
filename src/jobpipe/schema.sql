PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,   -- sha256(ats_vendor|company_slug|req_id)[:20]
    company          TEXT NOT NULL,
    company_slug     TEXT NOT NULL,
    title            TEXT NOT NULL,
    location         TEXT,
    remote_flag      INTEGER DEFAULT 0,
    req_id           TEXT,
    url              TEXT,
    apply_url        TEXT,
    ats_vendor       TEXT NOT NULL,
    department       TEXT,
    employment_type  TEXT,
    description_text TEXT,
    posted_at        TEXT,               -- ISO8601 UTC, from the source
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    seen_count       INTEGER DEFAULT 1,  -- ingest runs this req appeared in
    repost_count     INTEGER DEFAULT 0,  -- times it vanished and came back (ghost signal)
    is_open          INTEGER DEFAULT 1,  -- present in the most recent run for its source
    content_hash     TEXT,               -- scoring-cache invalidation
    dedup_key        TEXT,               -- sha1(company|norm_title|norm_location)
    duplicate_of     TEXT,               -- job_id of the canonical row, NULL if canonical
    status           TEXT DEFAULT 'discovered',
    filter_verdict   TEXT,               -- pass | reject
    filter_reason    TEXT,               -- which Tier-1 rule killed it
    fit_score        INTEGER,            -- Tier-2, Phase 2
    scored_at        TEXT,
    score_json       TEXT,
    notes            TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_dedup    ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS idx_jobs_company  ON jobs(company_slug);
CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_open     ON jobs(is_open, filter_verdict);
CREATE INDEX IF NOT EXISTS idx_jobs_seen     ON jobs(first_seen_at);

-- Covering index for the UI's bin tallies. Every row carries kilobytes of
-- description_text, so an unindexed COUNT(*) has to page the whole table in;
-- with this the counts are answered from the index alone.
CREATE INDEX IF NOT EXISTS idx_jobs_bins ON jobs(
    is_open, duplicate_of, filter_verdict, filter_reason, status, seen_count);

-- One row per (source, run). Drives the silent-breakage health check.
CREATE TABLE IF NOT EXISTS source_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key   TEXT NOT NULL,          -- "greenhouse:stripe"
    ats_vendor   TEXT NOT NULL,
    company_slug TEXT NOT NULL,
    run_at       TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    http_status  INTEGER,
    fetched      INTEGER DEFAULT 0,      -- rows returned by the endpoint
    inserted     INTEGER DEFAULT 0,
    updated      INTEGER DEFAULT 0,
    duration_ms  INTEGER,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_source ON source_runs(source_key, id DESC);

-- Append-only audit of state-machine transitions (Stage 6 hooks into this).
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL,
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id DESC);
