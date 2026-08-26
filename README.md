# Auto Application Pipeline

Finds new-grad software jobs across hundreds of company job boards, filters out
the ~99% that don't fit you, ranks what's left, and fills in the application
form so you only have to read it and press Submit.

**It never submits anything.** That click stays yours, by design.

```
520 sources  →  42,000 postings  →  Tier-1 rules  →  ~300 shortlist  →  fill the form
                                       (free)         (ranked)          (you review)
```

---

## Why it works this way

Mass auto-apply is a trap. ATS systems flag people who blast every req at one
company, a large share of postings are ghosts that never hire anyone, and
LinkedIn bans automation accounts. Volume is not the win condition.

Being **early to the right posting with a grounded application** is. So this
tool automates discovery, filtering, ranking and form-filling, then stops dead
at the submit button.

Three rules it will not break:

1. **It never submits, accepts terms, ticks a consent box, or creates an account.**
2. **It never invents a fact about you.** Tailoring selects and rephrases from a
   file of things you have verified; it cannot add.
3. **It refuses to type** salary, SSN, date of birth, licence or passport
   numbers, bank details, passwords, or signatures. Those come back flagged.

---

## Install

Requires Python 3.10+ and Chrome.

```bash
git clone https://github.com/AndrewOxenberg/Auto-Application-Pipeline.git
cd Auto-Application-Pipeline

python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt     # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux
```

Then set up your profile:

```bash
cp profile/facts.example.yaml    profile/facts.yaml
cp profile/evidence.example.yaml profile/evidence.yaml
cp profile/narratives.example.md profile/narratives.md
mkdir -p profile/variants        # put your resume PDF in here
```

All four are gitignored. Until you create them the code falls back to the
example files, so a fresh clone runs and its tests pass immediately — you just
won't get *your* answers on forms.

**Fill in `profile/facts.yaml` before you apply to anything.** It is the source
of every answer the autofill types. Wrong data there means wrong data on a real
application.

Then build your target list and do a first poll (about three minutes):

```bash
.venv/Scripts/python.exe jobs.py bootstrap   # ~500 companies from public lists
.venv/Scripts/python.exe jobs.py ingest      # poll them all
.venv/Scripts/python.exe jobs.py serve       # open the UI
```

---

## Using it

```
jobs.py serve             local UI at http://127.0.0.1:8765   <- start here
jobs.py ingest            poll every source, dedup, run Tier-1
jobs.py list              the ranked shortlist
jobs.py show <id>         one posting in full
jobs.py score             Tier-2 LLM ranking (needs an API key)
jobs.py sources           per-source health
jobs.py rejects           what the filter killed, grouped by rule
jobs.py stats             database summary
jobs.py probe lever acme  hit one endpoint and report what came back
```

The UI is the main surface: the shortlist, the full posting, a **Run poll**
button, and per-job triage (shortlist / applied / pass, plus notes). Triage
writes to `jobs.status`, which the CLI reads back.

### Run it on a schedule

New-grad reqs collect hundreds of applicants inside 48 hours, so polling on a
timer is most of the value.

```powershell
.\scripts\install-task.ps1              # every 4 hours, Windows, no admin needed
.\scripts\install-task.ps1 -Hours 6
.\scripts\install-task.ps1 -Uninstall
```

Logs land in `data/logs/`. On macOS or Linux, use cron instead:

```
0 */4 * * * cd /path/to/repo && .venv/bin/python jobs.py ingest && .venv/bin/python jobs.py score --new --quiet
```

---

## The browser extension

The UI is served from `127.0.0.1`. An application form is on `greenhouse.io`.
Browser security means a page on one **cannot** touch a field on the other, so
the typing has to be done by something with browser-level privileges. That is
what `extension/` is.

**Install once, about a minute:**

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. **Load unpacked**, then select the `extension/` folder in this repo

Then in the UI, click **Auto Apply** on any job. The form opens in a new tab and
fills itself, including the resume upload. A banner reports what it filled, what
it skipped and why, and what is still blank.

If you land on a job description rather than the form, the extension waits and
fills as soon as you click Apply.

**What it will not do:** submit, accept terms, tick consent boxes, create an
account, or fill any of the refused fields listed above.

### If it does not fill

- Is `jobs.py serve` running? The extension reads the packet from it.
- Did you click Auto Apply within the last 10 minutes? The pending slot expires,
  so a form opened hours later is not filled behind your back.
- `chrome://extensions` -> this extension -> **service worker** -> Console.
- The UI keeps a manual fallback: expand "Manual fallback" under the fill packet
  for a prompt you can hand to an AI browser assistant instead.

`python scripts/debug_fill.py <job_id> --stub-resume` prints the shipping fill
engine plus that job's packet as one blob to paste into a form's console. That
is how the engine was debugged against live forms; these boards' CSP blocks
script injection and localhost fetches, so pasting is the only way in.

---

## Tailoring for a new resume

Two files drive everything the tool says about you.

**`profile/facts.yaml`** holds fixed-value answers: name, contact, address,
education, work authorisation, availability, EEO, common yes/no questions, and
the path to your resume PDF. Change your resume, change `documents.resume`.

**`profile/evidence.yaml`** holds your accomplishments as atomic tagged records.
This is the file that makes generated material safe. Each record looks like:

```yaml
- id: example-realtime-system
  org: Personal Project
  role: Lead Developer
  period: "2026-01 – present"
  tags: [systems, python, realtime, testing, backend]
  claim: >
    Built a real-time data pipeline in Python and FastAPI ingesting 50-80
    events per second across 100+ tracked sources.
  metrics: {events_per_sec: "50-80", sources: "100+"}
  verified: true
```

### The rule tailoring must not break

**Tailoring selects and rephrases. It never adds.**

- Every bullet in a generated resume maps to an `id` in `evidence.yaml`. No id,
  no bullet.
- Keywords from the job description decide **which** evidence appears, in what
  order, and which metrics surface. They never decide what the claim says.
- Rephrasing may use the posting's vocabulary for something already true. It may
  not introduce a technology, a scale, or a responsibility that is not in the
  record.
- If a posting wants Kubernetes and your evidence has none, that is a **gap**.
  `narratives.md` has a "Known gaps" section precisely so a draft cannot quietly
  claim it.

### Swapping in a new resume

1. Drop the PDF in `profile/variants/` and point `documents.resume` at it.
2. Update `evidence.yaml` so every bullet on the new resume exists as a record.
   If a bullet is not in there, tailoring cannot cite it and scoring cannot
   credit it.
3. Re-tag. The `tags` field drives which variant gets selected for which job, so
   keep them consistent (`systems`, `data`, `fullstack`, `ai-tooling`).
4. `jobs.py score --rescore` to re-rank against the new record.

**Honest status:** resume bullet-rewriting is designed but not built. Doing it
properly needs the LaTeX or Typst source your PDF was built from, or a rebuild
from `evidence.yaml` into a template; editing a compiled PDF reflows badly and
is not worth it. Today the tool uploads one fixed PDF, and the variant machinery
is scaffolded around `evidence.yaml` tags.

---

## Tuning what gets through

`config/rules.yaml` is the whole filtering strategy, and it ships configured for
one person's search. **Edit it before trusting the shortlist.** After any change:

```bash
jobs.py filter --all     # re-evaluate everything, no refetch
jobs.py rejects          # confirm the rules kill what you meant
jobs.py rejects --sample "location:"
```

What you will want to change:

| Block | What it does |
|---|---|
| `title.allow` / `title.deny` | Which job titles count. Ships tuned for new-grad SWE. |
| `location.allow_substrings` | Your regions. **Ships with a US mid-Atlantic list.** |
| `location.deny_substrings` | Places you cannot work; foreign offices by default. |
| `grad_window` | Your graduation window. |
| `experience.max_years_required` | Rejects "5+ years" postings. |
| `employment.exclude_internships` | On by default. |
| `references` | Rejects postings demanding references. |

**Location matching is token-based, not substring.** A single word matches a
whole token; anything with a space matches as a phrase. This matters: `ca` as a
substring matches `canada`. Ambiguous city names are listed state-qualified
(`vienna va`, not `vienna`), because a bare city name will match the wrong one.

---

## Tier-2 scoring (optional, costs money)

Ranks the shortlist 0-100 with a rationale, gaps, red flags, and which evidence
to lead with. Needs an Anthropic API key:

```bash
setx ANTHROPIC_API_KEY "sk-ant-..."     # Windows, then open a new terminal
export ANTHROPIC_API_KEY="sk-ant-..."   # macOS/Linux
```

```bash
jobs.py score --dry-run    # cost estimate, submits nothing
jobs.py score              # the backlog, via the Batch API at half price
jobs.py score --new        # just the unscored, synchronously
```

Roughly **$0.30 for 300 postings** using Claude Haiku, and under a cent per poll
after that. Once a key is set, every poll scores what it finds automatically.

Without a key everything else still works; scoring quietly no-ops. There is also
a keyless path: `jobs.py score --export-manual out.json` writes the prompts, you
or any assistant score them, and `--import-manual` reads them back.
`jobs.py score --show-rubric` prints the rubric.

---

## Layout

```
profile/           facts, evidence, narratives (yours; gitignored)
  *.example.*      committed templates - copy these
  variants/        your resume PDF
config/
  rules.yaml       the whole filtering strategy - edit this
  companies.yaml   generated by `jobs bootstrap`
extension/         the Chrome autofill extension
  fill-engine.js   all the form-filling logic
src/jobpipe/
  sources/         one module per ATS, each with a verified payload shape
  ingest.py        parallel fetch, serial write, health check
  filters.py       Tier-1 rules
  apply.py         builds the fill packet
  score.py         Tier-2 scoring
  web.py           local server + JSON API
  ui/index.html    the whole UI, no build step
scripts/           poll.ps1, install-task.ps1, debug_fill.py
tests/             76 offline tests, no network
```

---

## Things learned the hard way

Worth knowing before you change anything.

**A stale ATS slug returns HTTP 200 with an empty array**, not an error.
Confirmed on Lever. So `close_missing` is skipped on an empty payload, "empty"
is reported separately from "failing", and health only alerts after two
consecutive bad runs.

**Greenhouse's `requisition_id` is not unique** — one req posted to four cities
shares it across four board posts. Identity comes from the board post `id`.

**Substring matching will bite you repeatedly.** `ca` matched `canada`. `unit`
matched "**Unit**ed States". `graduate` matched "Under**graduate**". `initials`
blocked "Middle **Initial**". Every one of those shipped as a bug first. Use
word boundaries.

**Typeahead dropdowns do not open on synthetic `input` events**, and clicking an
option only highlights it — the value clears on blur. Selection has to be made
with the keyboard and confirmed after a blur.

**A form that looks filled and submits empty is worse than a blank one.** Every
fill path verifies the value survived losing focus.

**Fetch in parallel, write serially.** Twelve threads on one SQLite file gives
you `database is locked`.

---

## Setting this up for yourself

If you use an AI coding assistant, this prompt will get you configured:

> I've cloned https://github.com/AndrewOxenberg/Auto-Application-Pipeline and
> want to set it up for my own job search. Please:
>
> 1. Create the venv and install `requirements.txt`.
> 2. Copy `profile/facts.example.yaml` to `profile/facts.yaml`,
>    `profile/evidence.example.yaml` to `profile/evidence.yaml`, and
>    `profile/narratives.example.md` to `profile/narratives.md`.
> 3. Ask me for the details `facts.yaml` needs: legal name, contact, address,
>    school, degree, major, GPA, graduation date, work authorisation, earliest
>    start date, and how I want EEO questions answered. Don't guess any of it,
>    and don't put a salary number anywhere.
> 4. Read my resume (I'll attach it) and turn every bullet into an atomic record
>    in `evidence.yaml`, with tags and metrics. Include only what the resume
>    actually claims — do not embellish. Then ask me which claims I can defend in
>    an interview, and set `verified: true` only for those. Put the PDF in
>    `profile/variants/` and point `documents.resume` at it.
> 5. Rewrite `config/rules.yaml` for me: my target titles, the regions I can work
>    in (mine are: ___), my graduation window, and whether to exclude
>    internships. Explain what each rule will reject so I can sanity-check it.
> 6. Run `jobs.py bootstrap`, then `jobs.py ingest`, then show me
>    `jobs.py rejects` and tell me whether the filter is killing anything it
>    shouldn't.
> 7. Walk me through loading `extension/` into Chrome as an unpacked extension.
>
> Never fill in a value I haven't given you, and never make the tool submit an
> application.

---

## Notes

Personal tooling, shared as-is. The polling is deliberately gentle: public JSON
endpoints, no scraping, and no LinkedIn or Indeed — that is a ToS violation with
aggressive bot detection, and the collateral is the account you actually need.

Read what it fills before you submit. It is a fast, careful assistant, not a
substitute for your judgement about where you apply.
