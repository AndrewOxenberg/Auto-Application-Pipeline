# Product

<!-- impeccable:product-schema 1 -->

## Platform

web (localhost only; never deployed, never multi-user)

## Stack

Python standard library plus PyYAML, and one self-contained HTML page. The
`anthropic` SDK is needed only for Tier-2 scoring. Chosen because a personal
tool that needs `npm install` to look at a database stops being run.

## What this is

A personal job-application pipeline for one person on one machine. It polls ATS
APIs, dedups, filters and ranks postings, then fills in application forms for
review.

It deliberately does not submit. Mass auto-apply is a trap: ATS dedup flags
multi-apply, and a large share of postings are ghosts. The thesis is that being
early to the right posting with a grounded application beats volume.

## Primary user and job

One user, on their own machine, most likely in the morning. The job: **triage
today's postings down to the two or three worth an hour of real effort**, then
get out. Not browsing. Not research. A daily pass over a list that is mostly
noise, looking for the few rows that are not.

Sessions are short and repeated. The same list is seen again tomorrow with
fifteen new rows in it, so "what changed since yesterday" matters more than
"what exists".

## The numbers that shape the interface

From a representative run, measured rather than estimated:

- ~42,000 postings in the database, ~40,000 open after dedup
- ~300 survive the Tier-1 filter, roughly 0.8%
- ~2,400 folded as cross-source duplicates
- a full poll takes about 150 seconds and finds ~15 genuinely new postings
- ~520 sources, a handful of which are broken or stale at any time

A very large haystack, a small shortlist, and a background health problem that
silently corrupts the shortlist if nobody looks at it.

## Terminology

- **Tier-1** — the free deterministic rules filter. Every rejection records the
  rule that fired.
- **fit score** — Tier-2 LLM score, 0-100.
- **ghost job** — a posting that closed and came back, tracked as `repost_count`.
- **source health** — a source is "failing" (HTTP error) or "empty" (HTTP 200
  with zero rows, which is how a stale slug fails). Two consecutive bad runs
  before it alerts.
- **fill packet** — every field on one application form, with the answer from
  `profile/facts.yaml` and a status per field.

## What the UI must do

1. Browse and filter the shortlist.
2. Read a full posting without leaving for the CLI.
3. Triage: mark a job interested / applied / dismissed, and write a note.
4. Surface source health, because a broken source is invisible in the job list
   by definition.
5. Fill an application form on request, and never submit it.

## Constraints that must survive

- **No submit button, ever.** Human-gated submission is the central thesis, not
  an implementation detail.
- Localhost, single user, no auth, no telemetry. The page must work with the
  machine offline apart from the local server.
- Personal data lives in `profile/`, which is gitignored. Nothing in the
  repository should contain a real name, address, or phone number.
- Empty and null states are normal, not edge cases: fit scores are null until
  Tier-2 runs, and Tier-2 needs an API key the user may not have.
