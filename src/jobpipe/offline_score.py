"""Deterministic Tier-2 scoring. Feature 14.

Produces the same payload the LLM scorer produced, from the posting text alone,
with no network call and no key. The argument for believing rules can stand in
here is in the feature log: reading the 49 LLM-scored rows back, the model was
mostly grading level, degree, discipline and stack overlap. Those are all
extractable.

What is NOT claimed: that this reproduces a language model's judgement. It
reproduces its ranking closely enough to put the right postings at the top and
the right ones at the bottom, and `scripts/calibrate_score.py` is what holds
that claim to account against the 49.
"""
from __future__ import annotations

import json
import re
import sqlite3

from .config import evidence

MODEL = "offline-v1"

# -- extractors -----------------------------------------------------------

# Deliberately wider than filters._YEARS_RE, which requires the word
# "experience" within three tokens and therefore misses "8+ years in systems
# security" and "8+ years managing corporate fleets" - both of which are in the
# shortlist today, scored 5 and 8 by the LLM.
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus\b|or more\b|(?:\s*(?:-|to)\s*\d{1,2}))?\s*years?\b", re.I)

# A year count only counts as a requirement when the surrounding text is
# talking about the candidate. Without this, "For over 30 years, Acme has
# been..." reads as a thirty-year requirement.
_REQ_CUE = re.compile(
    r"experience|background|qualification|minimum|required|require|proven|"
    r"track record|hands[- ]on|professional|industry|working|managing|manage|"
    r"building|build|developing|develop|designing|design|leading|"
    r"expertise|proficien|demonstrated", re.I)
_HISTORY_CUE = re.compile(
    r"for (?:over|more than|nearly|almost)|founded|since \d{4}|"
    r"history|anniversary|celebrat|ago\b|past \d+ years|in business|"
    r"over the (?:next|last|past)|next \d+ years|age of|years old|"
    r"\d[- ]?year (?:degree|program|college|university|institution)|"
    r"four[- ]year|4[- ]year", re.I)

_DEGREE_PHD = re.compile(r"\bph\.?\s?d\b|\bdoctoral\b|\bdoctorate\b", re.I)
_DEGREE_MS = re.compile(
    r"\bm\.?s\.?\b(?!\s*(?:office|word|excel|teams))|\bmaster'?s\b|\bm\.?eng\b", re.I)
# "PhD preferred" or "MS or equivalent experience" is not a wall.
_DEGREE_SOFT = re.compile(r"preferred|nice to have|or equivalent|plus\b|bonus", re.I)
# Neither is "BS/MS/PhD in Computer Science" - that list requires a bachelor's.
_DEGREE_ALTERNATIVE = re.compile(
    r"\bb\.?s\.?\b|\bb\.?a\.?\b|bachelor|undergraduate degree", re.I)

_CLEARANCE = re.compile(
    r"security clearance|top secret|ts/sci|polygraph|"
    r"\bsecret\b(?!\s*sauce)|public trust|clearance (?:is )?required|"
    r"active clearance|eligible for.{0,25}clearance", re.I)

_SENIOR = re.compile(
    r"\bsenior\b|\bsr\.?\b|\bstaff\b|\bprincipal\b|\blead\b|\bmanager\b|"
    r"\bdirector\b|\bhead of\b|\barchitect\b|\bvp\b|\biii\b|\biv\b", re.I)
_MID = re.compile(r"\bii\b", re.I)
_ENTRY_TITLE = re.compile(
    r"\bnew ?grad\b|\bgraduate\b|\bentry[- ]level\b|\buniversity\b|\bcampus\b|"
    r"\bearly career\b|\bassociate\b|\bjunior\b|\bjr\.?\b|\brotational\b|"
    r"\bi\b|\b1\b", re.I)
# A named intake year, usually attached to a structured programme: "2027 EDGE
# Program", "New Grad - 2027 Start". Worth its own signal, because the title
# carries it even on rows that have no description at all.
_PROGRAM_TITLE = re.compile(r"\b20(?:2[6-9])\b|\bprogram(?:me)?\b|\bcohort\b", re.I)
_ENTRY_DESC = re.compile(
    r"new grad|recent graduate|entry[- ]level|early[- ]career|university hire|"
    r"campus hire|rotational program|0-2 years|0-3 years|1-2 years|"
    r"no experience required|final year|graduating (?:in )?20\d\d|class of 20\d\d",
    re.I)

# A title can clear the Tier-1 allow list and still not be a software job.
_NOT_SOFTWARE = re.compile(
    r"model based systems|\bmechanical\b|\belectrical\b|\bcivil\b|"
    r"\bindustrial engineer\b|\bfield engineer\b|\bsales\b|\bmarketing\b|"
    r"\brecruit|\bcustomer success\b|\bit support\b|\bhelp ?desk\b|"
    r"\bsystem administrator\b|\bsysadmin\b|\bit systems\b|\bmanufacturing\b|"
    r"\bquality engineer\b|\bhardware engineer\b|\bproduct manager\b|"
    r"\bsystems engineer", re.I)
_IS_SOFTWARE = re.compile(
    r"\bsoftware\b|\bdeveloper\b|\bprogrammer\b|\bfull ?stack\b|\bbackend\b|"
    r"\bfrontend\b|\bdata (?:scien|engineer|analy)|\bmachine learning\b|"
    r"\bplatform engineer\b|\binfrastructure\b|\bsecurity engineer\b|"
    r"\bcomputer scien|\bapplication develop|\bsre\b|\bdevops\b", re.I)

# The rubric names these as permanent gaps. One list, so the rule and the
# message can never drift apart.
KNOWN_GAPS = {
    "Go": r"\bgolang\b|\bgo\b(?=\s+(?:programming|language|developer|experience))",
    "Kotlin": r"\bkotlin\b",
    "Scala": r"\bscala\b",
    "Kubernetes": r"\bkubernetes\b|\bk8s\b",
    "production cloud deployment": (
        r"\bterraform\b|\bhelm charts?\b|\bcloudformation\b|"
        r"production (?:aws|gcp|azure|cloud)"),
}

_VARIANT_TAGS = {
    "systems": {"systems", "backend", "realtime", "algorithms", "interop",
                "standards", "testing", "ownership"},
    "data": {"data", "sql", "analytics", "statistics", "research"},
    "fullstack": {"fullstack", "typescript", "tooling", "developer-experience"},
    "ai-tooling": {"ai-tooling"},
}

_VARIANT_CUES = {
    "systems": (r"\bsystems?\b|\bbackend\b|\bdistributed\b|\breal[- ]?time\b|"
                r"\binfrastructure\b|\bembedded\b|\bc\+\+|\brust\b|"
                r"\blow[- ]level\b|\bperformance\b|\bplatform\b|\bapi\b"),
    "data": (r"\bdata\b|\bsql\b|\banalytics\b|\bstatistic|\bpandas\b|"
             r"\bsnowflake\b|\bwarehouse\b|\betl\b|\bdashboard\b|\bresearch\b"),
    "fullstack": (r"\bfull ?stack\b|\bfront ?end\b|\breact\b|\btypescript\b|"
                  r"\bjavascript\b|\bweb\b|\bcss\b|\bnode\b"),
    "ai-tooling": (r"\bllm\b|\bagent\b|\bprompt\b|\bmachine learning\b|"
                   r"\bgenerative\b|\bmodel inference\b"),
}

# Languages and tools he can actually claim, read off the evidence records.
_STACK = {
    "Python": r"\bpython\b", "Java": r"\bjava\b(?!script)", "SQL": r"\bsql\b",
    "TypeScript": r"\btypescript\b", "JavaScript": r"\bjavascript\b",
    "React": r"\breact\b", "C++": r"\bc\+\+", "C#": r"\bc#",
    "Rust": r"\brust\b", "OCaml": r"\bocaml\b", "Linux": r"\blinux\b",
    "Git": r"\bgit\b", "Pandas": r"\bpandas\b", "MATLAB": r"\bmatlab\b",
}

_REFERRALS = {
    "UMD": r"university of maryland|\bumd\b|college park",
    "NIST": r"\bnist\b|national institute of standards",
    "Moore": r"\bmoore\b",
}

# The signal that turned out to dominate the LLM's mid-and-low scores. Read
# back, almost every rationale between 16 and 45 says a version of "specialized
# domain with nothing matching in his profile" - quantum compilers, NetSuite
# ERP, flight software, Workday HCM, knowledge-graph ML, night-shift warehouse
# analytics. None of those are bad jobs; they are just far from his evidence,
# and a filter that cannot see the distance ranks them like any other req.
#
# Only the single worst match applies. Summing would bury "embedded flight
# software DSP" three times over for what is really one mismatch.
SPECIALIST_DOMAINS = [
    ("quantum computing", r"\bquantum\b|\bphotonic|\bqubit", -28),
    ("an ERP/CRM platform", r"\bnetsuite\b|\bworkday\b|\bsap\b|\bsalesforce\b|"
                            r"\bservicenow\b|\bpeoplesoft\b|\boracle e-?business\b|"
                            r"\bsharepoint\b|\bmainframe\b|\bcobol\b|\bdynamics 365\b|"
                            r"\bhcm\b|\bepic systems\b", -25),
    # "warehouse" alone used to be in here and matched "data warehouse", which
    # is a database, not a building. It took the highest-scoring posting in the
    # calibration set from 78 to 60. Fourth time this project has shipped a
    # substring collision - see `ca`/`canada` in README.
    ("shift or warehouse operations", r"night shift|\bshift [a-d]\b|swing shift|"
                                      r"warehouse (?:associate|worker|floor|operations)|"
                                      r"distribution cent|on-call rotation", -20),
    ("embedded or signal processing", r"\bdsp\b|signal processing|\bfirmware\b|"
                                      r"\brf\b|avionics|flight software|\bfpga\b|"
                                      r"\bverilog\b|\bvhdl\b|bootloader|safety-critical", -18),
    ("ops rather than software", r"\bdevops\b|\bsre\b|site reliability|"
                                 r"system administrat|\bsysadmin\b|infrastructure operations|"
                                 r"\bhelpdesk\b|desktop support", -18),
    ("a game engine he has not used", r"\bunity\b|\bunreal\b|\bgodot\b", -12),
    ("research-level ML", r"knowledge graph|\bnlp research\b|computer vision|"
                          r"\bdeep learning research\b|publications? in", -12),
    ("defense or intelligence work that usually gates on clearance",
     r"data exploitation|intelligence community|\bl3harris\b|\bleidos\b|"
     r"\bbooz allen\b|\bsaic\b|\bperaton\b|\bmitre\b|\braytheon\b|\bnorthrop\b|"
     r"\bgeneral dynamics\b|national laborator|defense contract", -12),
]

# "3-month temporary engagement" pulled several otherwise-good new-grad reqs
# down into the 35-45 band.
_TEMP = re.compile(
    r"\b\d{1,2}[- ]month (?:temporary|contract|engagement)|temporary engagement|"
    r"\bcontract(?:-| )to(?:-| )hire\b|\bfixed[- ]term\b|\btemporary position\b|"
    r"\bseasonal\b", re.I)

# A title can say nothing while the body describes a senior role. The C++ Core
# Data req scored 18 precisely because its own body called it senior.
#
# Every phrase here has to be about the role. An earlier draft matched a bare
# "senior engineers" and fired on "mentorship from our Co-Founder and senior
# engineers" - a sentence offering him mentorship, scored as if he had to
# provide it. It took a posting the LLM gave 76 down to 39.
_SENIOR_BODY = re.compile(
    r"senior[- ]level(?: or above)?|this (?:is|role is) a senior|"
    r"at a senior level|\bmentor(?:ing)? (?:junior|other|less experienced)|"
    r"lead a team|set technical direction|own the (?:architecture|roadmap)|"
    r"proven (?:production )?track record|"
    # A role title in the body, e.g. "Senior Software Core Data engineer". The
    # lookbehinds are what keep it off "mentorship from our Co-Founder and
    # senior engineers", which is a perk, not a requirement.
    r"(?<!and )(?<!from )(?<!with )(?<!our )(?<!by )senior [a-z ]{0,24}engineer",
    re.I)

# A certification he does not hold and cannot get quickly.
_CERT = re.compile(
    r"\b8570\b|\biat level\b|\bsecurity\+\b|\bcissp\b|\bccna\b|\bcompTIA\b|"
    r"\bpmp\b|\baws certified\b|\bazure certified\b", re.I)


def _window(text: str, start: int, end: int, pad: int = 70) -> str:
    return text[max(0, start - pad):end + pad]


def max_years_required(desc: str) -> int | None:
    """Largest year count that reads as a requirement on the candidate.

    None when the text names no such number. Company-history phrasing is
    excluded, which is the whole reason this reads a context window rather
    than the match alone.
    """
    best = None
    for m in _YEARS.finditer(desc or ""):
        ctx = _window(desc, m.start(), m.end())
        if _HISTORY_CUE.search(ctx) or not _REQ_CUE.search(ctx):
            continue
        try:
            yrs = int(m.group(1))
        except ValueError:
            continue
        if yrs > 20:  # a 25-year requirement is boilerplate, not a requirement
            continue
        best = yrs if best is None else max(best, yrs)
    return best


def degree_required(desc: str) -> str | None:
    """'phd', 'ms', or None. A soft mention nearby downgrades it to None.

    The list case is the one that matters. "BS/MS/PhD in Computer Science"
    names a PhD and requires a bachelor's, and reading it as a doctorate wall
    was the single worst error in the first calibration pass - it took a
    Software Engineer Early Career req the LLM scored 58 down to 15.
    """
    for label, pattern in (("phd", _DEGREE_PHD), ("ms", _DEGREE_MS)):
        for m in pattern.finditer(desc or ""):
            ctx = _window(desc, m.start(), m.end(), 60)
            if _DEGREE_SOFT.search(ctx) or _DEGREE_ALTERNATIVE.search(ctx):
                continue
            return label
    return None


def parse_aggregator_meta(desc: str) -> dict:
    """Simplify rows carry a metadata line instead of posting text.

        category: AI/ML/Data | sponsorship: Other | degrees: Bachelor's, PhD

    It is structured, so it gets parsed as structure rather than regexed as
    prose. `degrees` is the load-bearing field: read across the 34 thin rows in
    the calibration set, PhD alone scored 10/15/18, "Bachelor's, PhD" scored 25
    and "Bachelor's, Master's, PhD" scored 50. A bachelor's anywhere in the
    list means the door is open to him.
    """
    meta = {}
    for part in (desc or "").split("|"):
        key, sep, value = part.partition(":")
        if sep:
            meta[key.strip().lower()] = value.strip()
    return meta


def degree_floor(meta: dict) -> str | None:
    """The lowest degree an aggregator row will accept: None, 'ms' or 'phd'."""
    degrees = (meta.get("degrees") or "").lower()
    if not degrees:
        return None
    if re.search(r"bachelor|associate", degrees):
        return None
    if "master" in degrees:
        return "ms"
    if re.search(r"ph\.?d", degrees):
        return "phd"
    return None


def specialist_domain(title: str, desc: str) -> tuple[str, int] | None:
    """The worst domain mismatch, or None. Only one ever applies."""
    blob = f"{title}\n{desc or ''}"
    worst = None
    for label, pattern, penalty in SPECIALIST_DOMAINS:
        if re.search(pattern, blob, re.I) and (worst is None or penalty < worst[1]):
            worst = (label, penalty)
    return worst


def stack_overlap(desc: str) -> list[str]:
    low = desc or ""
    return [name for name, pat in _STACK.items() if re.search(pat, low, re.I)]


def detect_gaps(desc: str) -> list[str]:
    low = desc or ""
    return [name for name, pat in KNOWN_GAPS.items() if re.search(pat, low, re.I)]


def pick_variant(title: str, desc: str) -> str:
    blob = f"{title} {desc or ''}"
    hits = {v: len(re.findall(pat, blob, re.I)) for v, pat in _VARIANT_CUES.items()}
    best = max(hits, key=lambda k: hits[k])
    return best if hits[best] else "systems"


def pick_evidence(variant: str, desc: str, limit: int = 3) -> list[str]:
    """Top records by tag overlap with the chosen variant, then keyword hits.

    Only ever returns ids that exist in evidence.yaml - the grounding rule the
    LLM was instructed to follow is a filter here rather than an instruction.
    """
    want = _VARIANT_TAGS.get(variant, set())
    low = (desc or "").lower()
    ranked = []
    for e in evidence():
        tags = set(e.get("tags") or [])
        if tags & {"skills", "education"}:
            continue
        claim = (e.get("claim") or "").lower()
        keyword = sum(1 for w in set(re.findall(r"[a-z+#]{5,}", claim)) if w in low)
        ranked.append((len(tags & want) * 10 + keyword, e["id"]))
    ranked.sort(reverse=True)
    return [eid for rank, eid in ranked[:limit] if rank > 0]


def referral_path(company: str, desc: str) -> str:
    blob = f"{company or ''} {desc or ''}"
    for name, pat in _REFERRALS.items():
        if re.search(pat, blob, re.I):
            return name
    return "none"


# -- the model ------------------------------------------------------------

def score_job(job: dict) -> dict:
    """One posting in, one LLM-shaped payload out.

    Signals accumulate as (label, delta) pairs so the rationale can say what
    actually moved the number, instead of describing the job back at you.
    """
    title = job.get("title") or ""
    desc = (job.get("description_text") or "").strip()
    blob = f"{title}\n{desc}"
    signals: list[tuple[str, int]] = []
    gaps: list[str] = []
    red_flags: list[str] = []

    score = 50
    # Below this there is no posting text, only an aggregator's metadata line
    # ("category: Software | sponsorship: Other | degrees: PhD"). Reading that
    # as prose is how a Google early-career req the LLM scored 58 came out at
    # 13: `degrees: PhD` lists the levels the posting accepts, and the rules
    # read it as a doctorate requirement. Thin rows are scored on title,
    # company and location only, which is what the LLM was told to do too.
    thin = len(desc) < 200
    body = "" if thin else desc

    # level, off the title first
    if _SENIOR.search(title):
        signals.append(("the title is not an entry-level req", -35))
        red_flags.append("title names a senior or lead level")
    elif _ENTRY_TITLE.search(title):
        signals.append(("entry-level title", 3))
    if _PROGRAM_TITLE.search(title) and not _SENIOR.search(title):
        signals.append(("a structured programme with a named intake year", 8))
    if _MID.search(title) and not _SENIOR.search(title):
        signals.append(("second-level req", -10))
    if _ENTRY_DESC.search(body):
        signals.append(("written for a new grad", 10))
    if _SENIOR_BODY.search(body) and not _SENIOR.search(title):
        signals.append(("the body describes a senior role under a junior title", -20))
        red_flags.append("unqualified title, but the body reads senior")

    # experience
    yrs = max_years_required(body)
    if yrs is not None:
        if yrs <= 1:
            signals.append((f"asks for {yrs} year of experience", 10))
        elif yrs <= 3:
            signals.append((f"asks for {yrs} years, which is reachable", 0))
        elif yrs <= 5:
            signals.append((f"asks for {yrs} years", -20))
            red_flags.append(f"{yrs} years of experience wanted")
        else:
            signals.append((f"asks for {yrs} years", -35))
            red_flags.append(
                f"{yrs} years wanted; a senior req wearing a junior title"
                if _ENTRY_TITLE.search(title)
                else f"{yrs} years of experience wanted")
        if yrs > 3:
            gaps.append(f"wants {yrs} years of experience; he graduates May 2027")

    # degree. On a thin row this comes from the structured metadata line; on a
    # real posting it is read out of the prose.
    deg = degree_floor(parse_aggregator_meta(desc)) if thin else degree_required(body)
    if deg == "phd":
        signals.append(("requires a PhD", -40))
        gaps.append("requires a PhD; he has a BS")
        red_flags.append("PhD degree requirement")
    elif deg == "ms":
        signals.append(("requires a master's", -15))
        gaps.append("requires a master's; he has a BS")
    elif re.search(r"\bph\.?\s?d\b|\bmaster'?s\b", title, re.I):
        # The title alone is enough; aggregator rows carry no description.
        signals.append(("an advanced degree is named in the title", -35))
        gaps.append("advanced degree named in the title")

    # discipline
    if _NOT_SOFTWARE.search(title) and not _IS_SOFTWARE.search(title):
        signals.append(("this is not a software discipline", -25))
        red_flags.append("title names a non-software engineering discipline")

    # domain distance, the signal that dominates the LLM's 16-45 band
    domain = specialist_domain(title, body)
    if domain:
        label, penalty = domain
        signals.append((f"the work is {label}", penalty))
        gaps.append(f"{label}: nothing in his evidence matches it")

    if _TEMP.search(body):
        signals.append(("a temporary engagement, not a permanent req", -15))
        red_flags.append("temporary or fixed-term engagement")

    if _CERT.search(body):
        signals.append(("wants a certification he does not hold", -12))
        gaps.append("posting names a professional certification he does not hold")

    # stack overlap with the evidence
    stack = stack_overlap(body)
    if stack:
        signals.append((f"stack overlap ({', '.join(stack[:4])})",
                        min(9, 2 * len(stack))))

    # Permanent gaps named by the rubric. These cost points as well as being
    # listed: "2+ years of hands-on production AWS" is a real obstacle, and an
    # earlier draft recorded it as a gap while leaving the score untouched.
    found = detect_gaps(body)
    for g in found:
        gaps.append(f"wants {g}, which is not in his evidence")
    if found:
        signals.append((f"wants {found[0]}", max(-12, -6 * len(found))))

    # location
    loc = (job.get("location") or "").lower()
    if job.get("remote_flag"):
        signals.append(("remote", 3))
    elif re.search(r"\b(md|maryland|dc|washington|va|virginia|ny|new york)\b", loc):
        signals.append(("the location works", 5))

    # ghosts
    if (job.get("repost_count") or 0) >= 2:
        signals.append(("relisted repeatedly", -5))
        red_flags.append(f"closed and relisted {job['repost_count']} times")

    for _label, delta in signals:
        score += delta

    # -- hard rules, carried over from the LLM rubric verbatim -------------
    capped = None
    if _CLEARANCE.search(blob):
        score = min(score, 19)
        capped = "clearance"
        gaps.append("requires a security clearance he does not hold")
        red_flags.append("security clearance required")
    if thin:
        # Capped because the rubric says so, and docked a little on top: a req
        # nobody can read is a worse bet than an equivalent one you can.
        score = min(score - 5, 60)
        capped = capped or "thin"

    score = max(0, min(100, score))
    variant = pick_variant(title, desc)
    moved = [label for label, delta in signals if delta]

    if capped == "clearance":
        lead = ("Requires a security clearance he does not hold, which caps "
                "this regardless of the rest.")
    elif not moved:
        lead = "Nothing in the posting moves it either way, so it sits at the midpoint."
    elif len(moved) == 1:
        lead = moved[0][0].upper() + moved[0][1:] + "."
    else:
        lead = f"{moved[0][0].upper() + moved[0][1:]}, and {moved[1]}."
    tail = ("No description text, so this is scored on title, company and "
            "location only and capped at 60." if thin
            else (f"Also: {'; '.join(moved[2:5])}." if len(moved) > 2 else ""))

    return {
        "fit_score": int(score),
        "rationale": " ".join(x for x in (lead, tail) if x).strip(),
        "resume_variant": variant,
        "evidence_ids": pick_evidence(variant, desc),
        "gaps": gaps,
        "red_flags": red_flags,
        "effort": "low" if thin else ("high" if len(desc) > 6000 else "medium"),
        "referral_path": referral_path(job.get("company"), desc),
        "_signals": [{"label": label, "delta": delta} for label, delta in signals],
    }


def score_all(conn: sqlite3.Connection, jobs: list[dict], run=None):
    """Score a list of pending jobs and write them. Mirrors score.score_now."""
    from . import score as _score

    run = run or _score.ScoreRun()
    run.requested = len(jobs)
    for job in jobs:
        payload = score_job(job)
        # Keep an LLM score that is being replaced, so a rescore is reversible.
        prev = job.get("score_json")
        if prev:
            try:
                old = json.loads(prev)
                if old.get("_model") and old["_model"] != MODEL:
                    payload["_prev_llm"] = old
            except (json.JSONDecodeError, TypeError):
                pass
        _score._write_score(conn, job["job_id"], payload,
                            job.get("content_hash"), MODEL, run)
    conn.commit()
    return run
