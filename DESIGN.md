# Design

Recorded from the built surface, not from intention. Source of truth:
`src/jobpipe/ui/index.html`.

## The world: a fab yield sheet

A job search at this scale is a yield problem. 39,941 open postings go in, 489
come out, and the interesting number is 1.22%. So the UI is built as a
semiconductor yield sheet rather than a job-board dashboard: a lot header with
die counts, a die map, bins with numbered reject classes, and a probe log for
the sources that failed.

This is not decoration over a table. The mapping is exact:

| Fab term | What it actually is |
|---|---|
| die | one open, non-duplicate posting |
| bin | Tier-1 reject class (TITLE, LOCATION, EXPERIENCE, CLEARANCE, GRAD WINDOW, STALE, GHOST) |
| yield | pass rate through Tier-1 |
| probe site down | an ATS source failing or returning HTTP 200 with zero rows |
| lot | the most recent poll |

What it deliberately is not: cards, avatars, company logos, a kanban board, or
a "matches for you" feed. None of those are true here — there is no matching
model yet, and the fit score column is empty until Phase 2.

## Color

Restrained ground, committed categorical color where the form demands it.

Dark is the default look on this machine and the palette is authored for both.
The ground is cleanroom gray-blue in light, near-black slate in dark; ink is
graphite. One saturated categorical bin palette carries the reject classes and
appears in exactly two places: the die map and the bin legend chips. Nowhere
else. Probe-card gold is reserved for a single control — "Open posting" — and
is never used for decoration.

```
--bin-pass #17914c   --bin-title #c0490c   --bin-location #1f52c9
--bin-experience #6d3ad4  --bin-phrase #bb1240  --bin-grad #0b7a8f
--bin-stale #8a6008  --bin-ghost #94198c
--gold #e0a200 (primary action only)   --sel #1f52c9 (selection only)
```

In the die map, non-pass bins draw at 50% alpha. At 87% title rejects, full
chroma on every bin buried the 1.2% that matters under an orange slab. The
PASS band is the only thing at full strength, which is the whole point of the
map.

## Type

Two system stacks, no downloads — the page must render with the machine
offline apart from the local server.

- `--font-ui` Segoe UI / system-ui, for labels, prose, and controls.
- `--font-mono` Cascadia Mono / Consolas, for every number, id, location, and
  status string. This is measurement data, not a "technical" costume.

Scale: 13px data / 15px section heads / 20px lot id and posting title / 24px
readout figures. Tabular numerals everywhere a column of numbers appears. All
uppercase micro-labels are mono at 11px with 0.09em tracking.

## Structure

Rules, not cards. Every region is separated by a 1px rule; there are no nested
containers and no shadows except on the one modal.

```
lot header      counts + yield at right, die map full width beneath
bin legend      doubles as the filter row; the legend IS the control
alert strip     only present when a source is actually down
workspace       list left, posting detail right, single column under 1000px
```

## Interaction

- The bin legend and the die map are the same filter. Clicking a cell in the
  map selects that bin.
- Triage writes immediately. Status buttons toggle (pressing the active one
  returns the job to untriaged); the note autosaves 600ms after typing stops.
- `/` focuses the search. Arrow keys move between rows, Enter opens one.
- Transitions are 140ms on background and border only. There is no page-load
  choreography; the tool loads into a task.

## States that exist

Skeleton sweep while loading, empty states that name the next command
(`python jobs.py ingest`), a server-unreachable state that names the restart
command, a per-save failure message on the triage row, hover and focus rings on
every control, and a dismissed-row treatment that greys the text without
hiding the row.

## Known open findings

The mechanical detector reports two, both left deliberately:

1. **flat-type-hierarchy** — it counts `html{font-size:16px}` as a type step
   next to the 15px section head. 16px is the rem base, not a size in use. The
   real ratio between used steps is 1.6:1, well past the 1.25 it asks for.
2. **em-dash-overuse** — 12 em dashes, of which 11 are the single-character
   null placeholder in empty data slots (`—` for "no value yet"), which is
   correct table typography, and one is in the machine-readable direction
   contract comment. Prose em dashes were removed.

## Contract

The direction contract is the HTML comment at the top of `<body>` in
`index.html`. Seed key `e17728ff`.
