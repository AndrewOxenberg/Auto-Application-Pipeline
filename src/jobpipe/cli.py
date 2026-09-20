"""Command line entry point.

    jobs bootstrap          build config/companies.yaml from the GitHub lists
    jobs ingest             poll every configured source
    jobs filter             run the Tier-1 rules
    jobs list               ranked shortlist of what survived
    jobs show <id>          one posting in full
    jobs sources            per-source health, newest run first
    jobs rejects            what the filter killed, grouped by rule
    jobs stats              database summary
    jobs probe <v> <slug>   hit one endpoint and print what came back
    jobs serve              local web UI at http://127.0.0.1:8765
    jobs score              Tier-2 LLM scoring on Tier-1 survivors
    jobs push               send changes to the hosted site's store (feature 20)
    jobs pull               bring the site's triage and scores back
    jobs strip              drop descriptions nothing reads, then VACUUM
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

from . import bootstrap, db, dedup, filters, ingest, offline_score, remote, score, sync, web
from .http import get_json
from .config import companies, lists
from .sources import VENDORS, simplify
from .util import age_text


def _conn(args):
    return db.connect(args.db) if args.db else db.connect()


# -- commands -------------------------------------------------------------

def cmd_bootstrap(args) -> int:
    result = bootstrap.write(min_postings=args.min_postings)
    print(f"wrote {result['companies']} companies -> {result['path']}")
    unsupported = {k: v for k, v in result["vendors"].items()
                   if k not in bootstrap.SUPPORTED}
    print("postings by ATS:", json.dumps(result["vendors"], sort_keys=True))
    if unsupported:
        total = sum(unsupported.values())
        print(f"note: {total} postings sit on ATS vendors with no ingester "
              f"({', '.join(sorted(unsupported))}) and were skipped.")
    return 0


def cmd_ingest(args) -> int:
    conn = _conn(args)
    targets = []
    if args.source:
        vendor, _, slug = args.source.partition(":")
        targets.append((vendor, slug))
    else:
        if not args.lists_only:
            for c in companies():
                if c.get("ats") in VENDORS:
                    targets.append((c["ats"], c["slug"]))
                if args.limit and len(targets) >= args.limit:
                    break
        if not args.companies_only:
            for key in lists() or simplify.LISTS:
                targets.append((simplify.VENDOR, key))

    if not targets:
        print("nothing to ingest — run `jobs bootstrap` first", file=sys.stderr)
        return 1

    print(f"ingesting {len(targets)} sources with {args.workers} workers...")
    results = []
    for res in ingest.ingest_all(conn, targets, workers=args.workers,
                                 timeout=args.timeout):
        results.append(res)
        if not res.ok:
            print(f"  FAIL {res.source_key}: {res.error}")
        elif res.empty:
            print(f"  EMPTY {res.source_key}: 200 with 0 jobs — check the slug")
        elif args.verbose or res.inserted:
            print(f"  {res.source_key}: {res.fetched} fetched, "
                  f"{res.inserted} new, {res.updated} updated, {res.closed} closed")

    ok = sum(r.ok for r in results)
    print(f"\n{ok}/{len(results)} sources ok | "
          f"{sum(r.inserted for r in results)} new | "
          f"{sum(r.updated for r in results)} updated | "
          f"{sum(r.closed for r in results)} closed")

    d = dedup.rebuild(conn)
    print(f"dedup: {d['marked']} rows folded into {d['groups']} groups")
    counts = filters.apply(conn, only_new=not args.refilter)
    print(f"tier-1: {counts['pass']} pass / {counts['reject']} reject "
          f"of {counts['evaluated']} evaluated"
          + (f", {counts['kept']} kept (description stripped)" if counts["kept"] else ""))

    alerts = ingest.health(conn)
    if alerts:
        print(f"\n{len(alerts)} source health alerts — run `jobs sources --alerts`")
    return 0


def cmd_filter(args) -> int:
    counts = filters.apply(_conn(args), only_new=not args.all)
    print(f"tier-1: {counts['pass']} pass / {counts['reject']} reject "
          f"of {counts['evaluated']} evaluated"
          + (f", {counts['kept']} kept (description stripped)" if counts["kept"] else ""))
    return 0


def cmd_list(args) -> int:
    conn = _conn(args)
    where = ["is_open=1", "duplicate_of IS NULL"]
    params: list = []
    if not args.all:
        where.append("filter_verdict='pass'")
    if args.company:
        where.append("LOWER(company) LIKE ?")
        params.append(f"%{args.company.lower()}%")
    if args.new_since:
        where.append("first_seen_at >= ?")
        params.append(args.new_since)
    if args.min_score is not None:
        where.append("fit_score >= ?")
        params.append(args.min_score)

    rows = conn.execute(
        f"SELECT job_id, company, title, location, posted_at, first_seen_at, "
        f"ats_vendor, fit_score, url, repost_count FROM jobs "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY fit_score IS NULL, fit_score DESC, "
        f"COALESCE(posted_at, first_seen_at) DESC LIMIT ?",
        params + [args.limit]).fetchall()

    if not rows:
        print("nothing matches.")
        return 0

    print(f"{'id':<10} {'score':>5} {'age':>5}  {'company':<22} {'title':<44} location")
    print("-" * 118)
    for r in rows:
        score = "-" if r["fit_score"] is None else str(r["fit_score"])
        ghost = "*" if r["repost_count"] and r["repost_count"] > 1 else " "
        print(f"{r['job_id'][:8]}{ghost} {score:>5} {age_text(r['posted_at']):>5}  "
              f"{(r['company'] or '')[:22]:<22} {(r['title'] or '')[:44]:<44} "
              f"{(r['location'] or '')[:28]}")
    print(f"\n{len(rows)} shown. * = reposted, likely a ghost. "
          f"`jobs show <id>` for detail.")
    return 0


def cmd_show(args) -> int:
    conn = _conn(args)
    row = conn.execute(
        "SELECT * FROM jobs WHERE job_id LIKE ? LIMIT 1", (args.job_id + "%",)
    ).fetchone()
    if row is None:
        print(f"no job matching {args.job_id!r}", file=sys.stderr)
        return 1
    for k in ("company", "title", "location", "ats_vendor", "department",
              "employment_type", "url", "apply_url", "posted_at", "first_seen_at",
              "last_seen_at", "seen_count", "repost_count", "is_open",
              "filter_verdict", "filter_reason", "fit_score", "status",
              "duplicate_of"):
        if row[k] not in (None, ""):
            print(f"{k:<16} {row[k]}")
    desc = row["description_text"] or ""
    if desc and not args.no_body:
        print("\n" + "-" * 70)
        body = desc if args.full else desc[:2000] + ("\n... (--full for all)"
                                                     if len(desc) > 2000 else "")
        print(textwrap.fill(body, 88, replace_whitespace=False))
    return 0


def cmd_sources(args) -> int:
    conn = _conn(args)
    alerts = ingest.health(conn)
    if alerts:
        print(f"ALERTS ({len(alerts)}):")
        for a in alerts:
            print(f"  [{a['kind']:<7}] {a['source']:<32} {a['detail']}")
        print()
    if args.alerts:
        return 1 if alerts else 0

    rows = conn.execute(
        "SELECT source_key, ok, fetched, inserted, updated, http_status, "
        "duration_ms, run_at, error FROM source_runs "
        "WHERE id IN (SELECT MAX(id) FROM source_runs GROUP BY source_key) "
        "ORDER BY ok ASC, fetched ASC").fetchall()
    if not rows:
        print("no runs yet.")
        return 0
    print(f"{'source':<34} {'ok':>3} {'http':>5} {'jobs':>6} {'new':>5} {'ms':>6}  last run")
    print("-" * 96)
    for r in rows:
        print(f"{r['source_key'][:34]:<34} {('y' if r['ok'] else 'n'):>3} "
              f"{str(r['http_status'] or '-'):>5} {r['fetched']:>6} {r['inserted']:>5} "
              f"{r['duration_ms']:>6}  {r['run_at']}"
              + (f"  {r['error']}" if r["error"] else ""))
    return 0


def cmd_rejects(args) -> int:
    conn = _conn(args)
    rows = conn.execute(
        "SELECT filter_reason, COUNT(*) n FROM jobs "
        "WHERE is_open=1 AND filter_verdict='reject' "
        "GROUP BY filter_reason ORDER BY n DESC LIMIT ?", (args.limit,)).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE is_open=1 AND filter_verdict='reject'"
    ).fetchone()["n"]
    print(f"{total} open jobs rejected by Tier-1\n")
    for r in rows:
        print(f"{r['n']:>7}  {r['filter_reason']}")
    if args.sample:
        print(f"\nsample of rejects for rule {args.sample!r}:")
        for r in conn.execute(
                "SELECT company, title, location, filter_reason FROM jobs "
                "WHERE is_open=1 AND filter_reason LIKE ? LIMIT 15",
                (f"%{args.sample}%",)):
            print(f"  {r['company'][:20]:<20} {r['title'][:46]:<46} {r['location'][:24]}")
    return 0


def cmd_stats(args) -> int:
    conn = _conn(args)
    def q(sql):
        return conn.execute(sql).fetchone()[0]

    summary = [
        ("jobs total", "SELECT COUNT(*) FROM jobs"),
        ("  open", "SELECT COUNT(*) FROM jobs WHERE is_open=1"),
        ("  duplicates folded", "SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NOT NULL"),
        ("  tier-1 pass", "SELECT COUNT(*) FROM jobs WHERE is_open=1 AND "
                          "filter_verdict='pass' AND duplicate_of IS NULL"),
        ("  tier-1 reject", "SELECT COUNT(*) FROM jobs WHERE is_open=1 AND "
                            "filter_verdict='reject'"),
        ("  reposted 2+", "SELECT COUNT(*) FROM jobs WHERE repost_count>1"),
        ("  scored", "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"),
    ]
    for label, sql in summary:
        print(f"{label:<20}{q(sql)}")
    print("\nby vendor:")
    for r in conn.execute("SELECT ats_vendor, COUNT(*) n FROM jobs WHERE is_open=1 "
                          "GROUP BY ats_vendor ORDER BY n DESC"):
        print(f"  {r['ats_vendor']:<14} {r['n']}")
    print("\nby status:")
    for r in conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status "
                          "ORDER BY n DESC"):
        print(f"  {r['status']:<14} {r['n']}")
    return 0


def cmd_probe(args) -> int:
    """Hit one endpoint and report what actually came back. Use before writing
    or trusting a parser — the table in the plan doc is not evidence."""
    module = simplify if args.vendor == simplify.VENDOR else VENDORS.get(args.vendor)
    if module is None:
        print(f"unknown vendor {args.vendor!r}; known: "
              f"{', '.join([*VENDORS, simplify.VENDOR])}", file=sys.stderr)
        return 1
    url = module.url(args.slug)
    print(f"GET {url}")
    try:
        payload, status = get_json(url, timeout=args.timeout)
    except Exception as e:  # noqa: BLE001 - probe reports every failure shape
        print(f"FAILED: {e}")
        return 1
    jobs = module.parse(payload, args.slug)
    raw = len(payload.get("jobs", [])) if isinstance(payload, dict) else (
        len(payload) if isinstance(payload, list) else 0)
    print(f"http {status} | {raw} raw rows | {len(jobs)} parsed")
    if raw and not jobs:
        print("WARNING: rows returned but none parsed — the payload shape moved.")
    if not raw:
        print("WARNING: zero rows. A stale slug returns 200 + [] on Lever.")
    for j in jobs[:args.sample]:
        print(f"\n  {j['title']}\n    {j['location']} | posted {j['posted_at']}"
              f"\n    {j['url']}\n    {len(j['description_text'] or '')} chars of description")
    return 0


def cmd_score(args) -> int:
    conn = _conn(args)

    # The default since feature 14. No key, no network, no cost, and it runs
    # over the whole shortlist rather than whatever a budget allowed. --llm
    # opts back into the Batch API path below.
    if not args.llm:
        jobs = score.pending(conn, limit=args.limit, rescore=args.rescore)
        if not jobs:
            print("nothing to score. Every Tier-1 survivor already has a fit "
                  "score; use --rescore to redo them.")
            return 0
        run = offline_score.score_all(conn, jobs)
        print(f"scored {run.scored} job(s) with {offline_score.MODEL}")
        return 0

    if args.export_manual:
        jobs = score.pending(conn, limit=args.limit, rescore=args.rescore)
        if not jobs:
            print("nothing to export. Every Tier-1 survivor already has a fit score; "
                  "use --rescore to redo them.")
            return 0
        Path(args.export_manual).write_text(
            json.dumps(score.manual_export(jobs), indent=2), encoding="utf-8")
        print(f"exported {len(jobs)} jobs to {args.export_manual}")
        print("read that file, score each job against the rubric in "
              "`jobs.py score --show-rubric`, then import the results with "
              "`jobs score --import-manual <file>`.")
        return 0

    if args.show_rubric:
        for block in score.build_system():
            print(block["text"])
            print()
        return 0

    if args.import_manual:
        results = json.loads(Path(args.import_manual).read_text(encoding="utf-8"))
        job_ids = [r.get("job_id") for r in results if r.get("job_id")]
        hashes = {r["job_id"]: r["content_hash"] for r in conn.execute(
            f"SELECT job_id, content_hash FROM jobs WHERE job_id IN "
            f"({','.join('?' * len(job_ids))})", job_ids)} if job_ids else {}
        return _report(score.ingest_manual(conn, results, hashes))

    if args.collect:
        # Hashes come from the database rather than the batch: the point of
        # storing one is to know which posting text a score belongs to.
        hashes = {r["job_id"]: r["content_hash"] for r in
                  conn.execute("SELECT job_id, content_hash FROM jobs")}
        try:
            return _report(score.collect(conn, args.collect, hashes))
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 1

    if args.new:
        # Synchronous, not the Batch API: a dozen fresh postings should be
        # ranked in seconds, not in an hour. Silent no-op without a key so the
        # scheduled poll does not fail on a machine that has not set one.
        if not score.has_credentials():
            if not args.quiet:
                print("no Anthropic credentials; skipping scoring", file=sys.stderr)
            return 0
        fresh = score.pending(conn, limit=args.limit or 60)
        if not fresh:
            print("nothing new to score.")
            return 0
        est = score.estimate_cost(fresh)
        print(f"scoring {len(fresh)} new job(s) with {score.MODEL}, "
              f"about ${est['usd'] * 2:.2f} at full price")
        run = score.score_now(conn, fresh)
        return _report(run)

    jobs = score.pending(conn, limit=args.limit, rescore=args.rescore)
    if not jobs:
        print("nothing to score. Every Tier-1 survivor already has a fit score; "
              "use --rescore to redo them.")
        return 0

    est = score.estimate_cost(jobs)
    print(f"{est['jobs']} jobs to score with {score.MODEL} via the Batch API")
    print(f"  ~{est['input_tokens']:,} input + ~{est['output_tokens']:,} output tokens")
    print(f"  estimated cost: ${est['usd']:.2f}")
    if args.dry_run:
        print("\ndry run, nothing submitted. Sample request:\n")
        print(score.build_user(jobs[0])[:900])
        return 0
    if not args.yes:
        reply = input("submit? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("cancelled.")
            return 1

    hashes = {j["job_id"]: j["content_hash"] for j in jobs}
    try:
        batch_id = score.submit(conn, jobs)
    except RuntimeError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1

    print(f"\nbatch {batch_id} submitted")
    if args.no_wait:
        print(f"collect it later with: jobs score --collect {batch_id}")
        return 0

    print("waiting (most batches finish well inside an hour)...")
    score.wait(batch_id, on_tick=lambda b: print(
        f"  {b.processing_status}: {b.request_counts.processing} processing, "
        f"{b.request_counts.succeeded} done"))
    return _report(score.collect(conn, batch_id, hashes))


def _report(run) -> int:
    print(f"\nscored {run.scored}, errored {run.errored}")
    for err in run.errors[:10]:
        print(f"  {err}")
    if run.scored:
        print("\nrun `jobs list` for the ranked shortlist")
    return 0 if not run.errored else 1


def _site():
    """The hosted store, from TURSO_* in the environment or in .env.local (what
    `vercel env pull` writes). Totals only are printed: in GitHub Actions on a
    public repo, the log is public."""
    conn = remote.from_env(dotenv=db.ROOT / ".env.local")
    if conn is None:
        raise SystemExit("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are not set")
    return conn


def cmd_push(args) -> int:
    site = _site()
    s = sync.push(_conn(args), site, progress=(
        (lambda st: print(f"  {st['jobs']} jobs sent", flush=True)) if args.verbose else None))
    print(f"push: {s['jobs']} jobs ({s['new_jobs']} new), {s['events']} events, "
          f"{s['runs']} source runs; {s['rows_written']} rows written in "
          f"{s['requests']} requests" + (" [full push]" if s["full"] else ""))
    return 0


def cmd_pull(args) -> int:
    s = sync.pull(_conn(args), _site())
    moved = ", ".join(f"{k} {v}" for k, v in s.items() if k != "rows_read" and v)
    print(f"pull: {s['rows_read']} rows compared; " + (moved or "no changes"))
    return 0


def cmd_strip(args) -> int:
    n = sync.strip(_conn(args))
    print(f"strip: {n} descriptions dropped")
    return 0


def cmd_serve(args) -> int:
    web.serve(host=args.host, port=args.port,
              db_path=Path(args.db) if args.db else None,
              open_browser=not args.no_open,
              poll_on_start=not args.no_poll)
    return 0


# -- wiring ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobs", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="path to the SQLite file (default data/jobs.db)")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("bootstrap", help="generate config/companies.yaml")
    b.add_argument("--min-postings", type=int, default=1,
                   help="keep companies with at least this many live postings")
    b.set_defaults(func=cmd_bootstrap)

    i = sub.add_parser("ingest", help="poll sources and refresh the database")
    i.add_argument("--source", help="one source, as vendor:slug")
    i.add_argument("--limit", type=int, help="only the first N companies")
    i.add_argument("--workers", type=int, default=8)
    i.add_argument("--timeout", type=int, default=30)
    i.add_argument("--lists-only", action="store_true", help="skip companies.yaml")
    i.add_argument("--companies-only", action="store_true", help="skip the GitHub lists")
    i.add_argument("--refilter", action="store_true", help="re-run Tier-1 on everything")
    i.add_argument("-v", "--verbose", action="store_true")
    i.set_defaults(func=cmd_ingest)

    f = sub.add_parser("filter", help="run the Tier-1 rules")
    f.add_argument("--all", action="store_true", help="re-evaluate already-filtered jobs")
    f.set_defaults(func=cmd_filter)

    l = sub.add_parser("list", help="ranked shortlist")
    l.add_argument("--limit", type=int, default=40)
    l.add_argument("--all", action="store_true", help="include Tier-1 rejects")
    l.add_argument("--company")
    l.add_argument("--new-since", help="ISO date, e.g. 2026-08-24")
    l.add_argument("--min-score", type=int)
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="one posting in full")
    s.add_argument("job_id")
    s.add_argument("--full", action="store_true")
    s.add_argument("--no-body", action="store_true")
    s.set_defaults(func=cmd_show)

    h = sub.add_parser("sources", help="per-source health")
    h.add_argument("--alerts", action="store_true",
                   help="only alerts; exit 1 if any (for cron)")
    h.set_defaults(func=cmd_sources)

    r = sub.add_parser("rejects", help="what Tier-1 killed, by rule")
    r.add_argument("--limit", type=int, default=25)
    r.add_argument("--sample", help="show example jobs killed by a rule")
    r.set_defaults(func=cmd_rejects)

    st = sub.add_parser("stats", help="database summary")
    st.set_defaults(func=cmd_stats)

    pr = sub.add_parser("probe", help="hit one endpoint and report the shape")
    pr.add_argument("vendor")
    pr.add_argument("slug")
    pr.add_argument("--sample", type=int, default=2)
    pr.add_argument("--timeout", type=int, default=30)
    pr.set_defaults(func=cmd_probe)

    sc = sub.add_parser("score", help="Tier-2 scoring. Offline and free by "
                        "default; --llm uses the Batch API and costs money")
    sc.add_argument("--llm", action="store_true",
                    help="use the Anthropic Batch API instead of the offline scorer")
    sc.add_argument("--limit", type=int, help="only the N most recent survivors")
    sc.add_argument("--rescore", action="store_true",
                    help="re-score jobs that already have a fit score")
    sc.add_argument("--dry-run", action="store_true",
                    help="estimate cost and print one request, submit nothing")
    sc.add_argument("--no-wait", action="store_true",
                    help="submit and exit; collect later")
    sc.add_argument("--collect", metavar="BATCH_ID",
                    help="read a finished batch into the database")
    sc.add_argument("--new", action="store_true",
                    help="score only unscored survivors, synchronously and now")
    sc.add_argument("--quiet", action="store_true",
                    help="say nothing when no API key is configured")
    sc.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    sc.add_argument("--export-manual", metavar="FILE",
                    help="write pending jobs + prompts to FILE, to score without the API")
    sc.add_argument("--import-manual", metavar="FILE",
                    help="read hand-scored results from FILE into the database")
    sc.add_argument("--show-rubric", action="store_true",
                    help="print the scoring rubric used for both API and manual scoring")
    sc.set_defaults(func=cmd_score)

    sv = sub.add_parser("serve", help="local web UI")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--no-open", action="store_true", help="do not open a browser")
    sv.add_argument("--no-poll", action="store_true",
                    help="do not poll on startup (it is skipped anyway if one "
                         "finished in the last 30 minutes)")
    sv.set_defaults(func=cmd_serve)

    pu = sub.add_parser("push", help="send changes to the hosted site's store")
    pu.add_argument("-v", "--verbose", action="store_true", help="print progress")
    pu.set_defaults(func=cmd_push)
    pl = sub.add_parser("pull", help="bring the site's triage and scores back")
    pl.set_defaults(func=cmd_pull)
    sp = sub.add_parser("strip", help="drop descriptions nothing reads, then VACUUM")
    sp.set_defaults(func=cmd_strip)

    return p


def main(argv=None) -> int:
    # The Windows console defaults to cp1252, which turns every em dash and
    # accented company name into a "?" or a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
