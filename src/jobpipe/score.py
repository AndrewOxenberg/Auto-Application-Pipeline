"""Tier-2 LLM scoring. Stage 3 of the plan.

Runs only on Tier-1 survivors. Three things keep the bill small:

1. The Batch API, which is half price.
2. Prompt caching on the system block, which carries the whole profile and is
   byte-identical across every request in a run.
3. A `content_hash` cache in SQLite, so a req is never scored twice unless its
   posting text actually changed.

Model is Claude Haiku 4.5, named in the plan as "plenty for triage". Note it is
a pre-4.6 model: `output_config.effort` errors on it and adaptive thinking is
not available, so neither is used here. A triage classifier does not need
thinking anyway.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from .config import evidence, facts
from .util import now_iso

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 900

# Kept small on purpose. Every field has to earn its output tokens, and the
# fields here are the ones the plan's Stage 3 example asks for.
SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "fit_score": {
            "type": "integer",
            "description": "0-100. 70+ means worth an application today.",
        },
        "rationale": {
            "type": "string",
            "description": "Two sentences at most. Concrete, not encouraging.",
        },
        "resume_variant": {
            "type": "string",
            "enum": ["systems", "data", "fullstack", "ai-tooling"],
        },
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "ids from the evidence list that this application should lead with",
        },
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What the posting wants that he does not have. Be blunt.",
        },
        "red_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Reasons to skip: vague scope, no comp band, disguised senior role.",
        },
        "effort": {"type": "string", "enum": ["low", "medium", "high"]},
        "referral_path": {
            "type": "string",
            "description": "UMD, NIST, Moore, fraternity, or 'none' if nothing connects.",
        },
    },
    "required": ["fit_score", "rationale", "resume_variant", "evidence_ids",
                 "gaps", "red_flags", "effort", "referral_path"],
    "additionalProperties": False,
}


def build_system() -> list[dict]:
    """The stable prefix: who the candidate is and how to grade.

    Everything volatile lives in the user turn, so this block is byte-identical
    across every request in a batch and caches cleanly.
    """
    f = facts()
    ed, wa, av = f["education"], f["work_authorization"], f["availability"]
    records = [
        {"id": e["id"], "org": e.get("org"), "tags": e.get("tags", []),
         "claim": " ".join((e.get("claim") or "").split())}
        for e in evidence()
    ]

    profile = (
        f"{ed['degree']} in {ed['major']}, {ed['school']}. "
        f"GPA {ed['gpa']}. Graduates {ed['graduation_date']}. "
        f"{wa['citizenship']}, needs no sponsorship, holds no security clearance. "
        f"Available {av['earliest_start_date']}. "
        f"Targets: {', '.join(av['preferred_locations'])}."
    )

    rubric = """You are triaging job postings for one new-grad software engineer.
Score how well each posting fits him and how worthwhile applying would be.

Grade against the evidence below and nothing else. If the posting wants
something that is not in the evidence, that is a gap, not something to assume.

Scoring anchors, use the whole range:
  85-100  Strong new-grad fit. Stack overlaps his evidence, location works,
          and the req is genuinely entry level.
  70-84   Worth applying. Some overlap, no disqualifier.
  50-69   Marginal. Real gaps, or the role is only loosely software.
  25-49   Weak. Wants experience he lacks, or the title oversells the level.
  0-24    Do not apply. Wrong level, wrong field, or a disguised senior req.

Be strict. A shortlist where everything scores 80 is useless. Most postings
that clear a keyword filter are still a poor fit, and saying so is the job.

Hard rules:
- He has no security clearance and cannot get one quickly. A posting requiring
  one scores under 20 regardless of fit.
- "New grad" in a title does not make a req entry level. Read the requirements.
- Only cite evidence ids that exist in the list. Never invent an accomplishment.
- No Go, Kotlin, Scala, or production cloud deployment. Those are always gaps.
- referral_path names a real connection or "none". Do not guess at one."""

    return [
        {"type": "text", "text": rubric},
        {"type": "text", "text": f"CANDIDATE\n{profile}"},
        {
            "type": "text",
            "text": "EVIDENCE\n" + json.dumps(records, indent=None, sort_keys=True),
            # Last block of the stable prefix, so the breakpoint sits here.
            "cache_control": {"type": "ephemeral"},
        },
    ]


def build_user(job: dict, description_chars: int = 6000) -> str:
    """The volatile half. Truncation is stated, never silent."""
    desc = (job.get("description_text") or "").strip()
    truncated = len(desc) > description_chars
    if truncated:
        desc = desc[:description_chars] + "\n[description truncated for scoring]"
    lines = [
        f"COMPANY: {job.get('company')}",
        f"TITLE: {job.get('title')}",
        f"LOCATION: {job.get('location') or 'not stated'}",
        f"TYPE: {job.get('employment_type') or 'not stated'}",
        f"POSTED: {job.get('posted_at') or 'not stated'}",
        f"SOURCE: {job.get('ats_vendor')}",
    ]
    if job.get("repost_count"):
        lines.append(f"REPOSTS: {job['repost_count']} (closed and relisted)")
    if not desc:
        lines.append(
            "DESCRIPTION: none. This row came from an aggregator that ships no "
            "posting text. Score on title, company and location only, cap the "
            "score at 60, and say in the rationale that the text was missing.")
    else:
        lines.append("DESCRIPTION:\n" + desc)
    return "\n".join(lines)


@dataclass
class ScoreRun:
    batch_id: str | None = None
    requested: int = 0
    scored: int = 0
    errored: int = 0
    skipped_cached: int = 0
    errors: list = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def pending(conn: sqlite3.Connection, limit: int | None = None,
            rescore: bool = False) -> list[dict]:
    """Tier-1 survivors that need a score.

    A row is pending when it has never been scored, or when its posting text
    changed since it was (score_json stores the hash it was scored against).
    """
    where = ["is_open=1", "duplicate_of IS NULL", "filter_verdict='pass'"]
    if not rescore:
        where.append("fit_score IS NULL")
    sql = (f"SELECT job_id, company, title, location, employment_type, posted_at, "
           f"ats_vendor, repost_count, description_text, content_hash "
           f"FROM jobs WHERE {' AND '.join(where)} "
           f"ORDER BY COALESCE(posted_at, first_seen_at) DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql)]


def has_credentials() -> bool:
    """Cheap check so auto-scoring can no-op instead of raising every poll."""
    try:
        import anthropic
    except ImportError:
        return False
    try:
        client = anthropic.Anthropic()
    except Exception:  # noqa: BLE001 - any construction failure means no key
        return False
    return bool(client.api_key or client.auth_token)


def _client():
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "the anthropic SDK is not installed. "
            "Run: .venv\\Scripts\\python.exe -m pip install anthropic"
        ) from e
    client = anthropic.Anthropic()
    # The SDK resolves credentials lazily and only complains at request time,
    # with a bare TypeError from deep in the header builder. Check up front so
    # the failure arrives before a batch is half-built.
    if not (client.api_key or client.auth_token):
        raise RuntimeError(
            "no Anthropic credentials found.\n\n"
            "Tier-2 scoring calls the API and needs a key of its own. A Claude\n"
            "Code subscription does not carry over. Create one at\n"
            "https://console.anthropic.com/settings/keys then set it:\n\n"
            '  setx ANTHROPIC_API_KEY "sk-ant-..."\n\n'
            "Open a new terminal afterwards; setx only affects new ones.\n"
            "Every other command works without it."
        )
    return client


def submit(conn: sqlite3.Connection, jobs: list[dict]) -> str:
    """Create the batch and return its id."""
    client = _client()  # first, so a missing SDK reports the friendly error
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    system = build_system()
    batch = client.messages.batches.create(
        requests=[
            Request(
                custom_id=job["job_id"],
                params=MessageCreateParamsNonStreaming(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    messages=[{"role": "user", "content": build_user(job)}],
                    output_config={"format": {"type": "json_schema",
                                              "schema": SCORE_SCHEMA}},
                ),
            )
            for job in jobs
        ]
    )
    return batch.id


def _write_score(conn: sqlite3.Connection, job_id: str, payload: dict,
                  content_hash: str | None, model: str, run: ScoreRun) -> None:
    from . import db

    # Record which posting text this score belongs to, so a later edit to
    # the posting invalidates it instead of silently keeping a stale score.
    payload["_scored_content_hash"] = content_hash
    payload["_model"] = model
    conn.execute(
        "UPDATE jobs SET fit_score=?, scored_at=?, score_json=? WHERE job_id=?",
        (int(payload.get("fit_score", 0)), now_iso(),
         json.dumps(payload, sort_keys=True), job_id),
    )
    db.log_event(conn, job_id, "scored", f"{payload.get('fit_score')} ({model})")
    run.scored += 1


def collect(conn: sqlite3.Connection, batch_id: str,
            hashes: dict[str, str]) -> ScoreRun:
    """Read a finished batch into the database.

    Results come back in any order, so everything is keyed by custom_id.
    """
    client = _client()
    run = ScoreRun(batch_id=batch_id)

    for result in client.messages.batches.results(batch_id):
        job_id = result.custom_id
        kind = result.result.type
        if kind != "succeeded":
            run.errored += 1
            detail = getattr(getattr(result.result, "error", None), "type", kind)
            run.errors.append(f"{job_id[:8]}: {detail}")
            continue

        message = result.result.message
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            run.errored += 1
            run.errors.append(f"{job_id[:8]}: unparseable JSON ({e})")
            continue

        _write_score(conn, job_id, payload, hashes.get(job_id), MODEL, run)

    conn.commit()
    return run


def validate_result(payload: dict, valid_evidence_ids: set[str]) -> list[str]:
    """Same grounding discipline a batch response gets, applied to a
    hand-scored one. Returns an empty list when the payload is clean."""
    errors = []
    missing = set(SCORE_SCHEMA["required"]) - payload.keys()
    if missing:
        errors.append(f"missing fields: {sorted(missing)}")
        return errors  # nothing below is safe to check without them

    score = payload["fit_score"]
    if not isinstance(score, int) or not (0 <= score <= 100):
        errors.append(f"fit_score {score!r} is not an integer 0-100")
    if payload["resume_variant"] not in SCORE_SCHEMA["properties"]["resume_variant"]["enum"]:
        errors.append(f"resume_variant {payload['resume_variant']!r} is not one of the enum")
    if payload["effort"] not in SCORE_SCHEMA["properties"]["effort"]["enum"]:
        errors.append(f"effort {payload['effort']!r} is not one of the enum")
    bad_ids = set(payload.get("evidence_ids", [])) - valid_evidence_ids
    if bad_ids:
        errors.append(f"evidence_ids not in profile/evidence.yaml: {sorted(bad_ids)}")
    return errors


def manual_export(jobs: list[dict]) -> list[dict]:
    """One entry per pending job: enough to score it without the API.

    `prompt` is the exact user turn `submit()` would have sent; the system
    rubric (rules, evidence, hard caps) is not repeated per job because it is
    fixed for the whole batch — read it once with `build_system()`.
    """
    return [{"job_id": j["job_id"], "content_hash": j.get("content_hash"),
             "prompt": build_user(j)} for j in jobs]


def ingest_manual(conn: sqlite3.Connection, results: list[dict],
                   hashes: dict[str, str]) -> ScoreRun:
    """Write scores that were produced without calling the API — e.g. scored
    directly by a Claude Code session reading `manual_export()` output.

    Each entry in `results` is {"job_id": ..., **the SCORE_SCHEMA fields}.
    Validated the same way a batch result is, so a hand-scored run cannot
    silently poison the database with an out-of-range score or an invented
    evidence id.
    """
    run = ScoreRun()
    valid_ids = {e["id"] for e in evidence()}
    for r in results:
        job_id = r.get("job_id")
        if not job_id:
            run.errored += 1
            run.errors.append("result missing job_id")
            continue
        payload = {k: v for k, v in r.items() if k != "job_id"}
        errs = validate_result(payload, valid_ids)
        if errs:
            run.errored += 1
            run.errors.append(f"{job_id[:8]}: {'; '.join(errs)}")
            continue
        _write_score(conn, job_id, payload, hashes.get(job_id), "manual", run)
    conn.commit()
    return run


def score_now(conn: sqlite3.Connection, jobs: list[dict], workers: int = 4,
              on_each=None) -> ScoreRun:
    """Score a handful of jobs synchronously, right now.

    The Batch API is half price but takes minutes to an hour, which is the
    wrong trade for the dozen or two postings a single poll turns up — being
    early is the whole point. Full price on twenty Haiku calls is about a
    third of a cent. The backlog still goes through the batch path.
    """
    from concurrent.futures import ThreadPoolExecutor

    from . import db as _db

    run = ScoreRun(requested=len(jobs))
    if not jobs:
        return run

    client = _client()
    system = build_system()

    def one(job: dict):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": build_user(job)}],
                output_config={"format": {"type": "json_schema",
                                          "schema": SCORE_SCHEMA}},
            )
            text = next((b.text for b in message.content if b.type == "text"), "")
            return job, json.loads(text), None
        except Exception as e:  # noqa: BLE001 - one bad job must not stop the rest
            return job, None, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for job, payload, error in pool.map(one, jobs):
            if error or payload is None:
                run.errored += 1
                run.errors.append(f"{job['job_id'][:8]}: {error}")
                continue
            payload["_scored_content_hash"] = job.get("content_hash")
            payload["_model"] = MODEL
            conn.execute(
                "UPDATE jobs SET fit_score=?, scored_at=?, score_json=? "
                "WHERE job_id=?",
                (int(payload.get("fit_score", 0)), now_iso(),
                 json.dumps(payload, sort_keys=True), job["job_id"]),
            )
            _db.log_event(conn, job["job_id"], "scored",
                          f"{payload.get('fit_score')} ({MODEL})")
            run.scored += 1
            if on_each:
                on_each(job, payload)
    conn.commit()
    return run


def score_pending(conn: sqlite3.Connection, limit: int = 60,
                  on_each=None) -> ScoreRun:
    """Score whatever is unscored right now. Safe to call after every ingest.

    Returns an empty run when there is no API key, so the poll does not fail
    just because scoring is not configured.
    """
    run = ScoreRun()
    if not has_credentials():
        run.errors.append("no Anthropic credentials; skipping scoring")
        return run
    jobs = pending(conn, limit=limit)
    if not jobs:
        return run
    return score_now(conn, jobs, on_each=on_each)


def wait(batch_id: str, poll_seconds: int = 20, timeout_seconds: int = 86400,
         on_tick=None) -> str:
    """Block until the batch ends. Returns the terminal processing_status."""
    client = _client()
    deadline = time.monotonic() + timeout_seconds
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return batch.processing_status
        if time.monotonic() > deadline:
            raise TimeoutError(f"batch {batch_id} still {batch.processing_status}")
        if on_tick:
            on_tick(batch)
        time.sleep(poll_seconds)


def estimate_cost(jobs: list[dict]) -> dict:
    """Rough pre-flight estimate so a run never surprises anyone.

    Haiku 4.5 is $1.00 per 1M input and $5.00 per 1M output; the Batch API is
    half of both. Characters over four is a crude token proxy, deliberately
    generous rather than flattering.
    """
    system_chars = sum(len(b["text"]) for b in build_system())
    user_chars = sum(len(build_user(j)) for j in jobs)
    n = len(jobs)
    # The cached system prefix is written once and read n-1 times, at roughly
    # 1.25x to write and 0.1x to read.
    sys_tokens = system_chars / 4
    in_tokens = (sys_tokens * 1.25) + (sys_tokens * 0.1 * max(n - 1, 0)) + user_chars / 4
    out_tokens = n * 260
    cost = (in_tokens / 1e6) * 1.00 * 0.5 + (out_tokens / 1e6) * 5.00 * 0.5
    return {"jobs": n, "input_tokens": int(in_tokens),
            "output_tokens": int(out_tokens), "usd": round(cost, 4)}
