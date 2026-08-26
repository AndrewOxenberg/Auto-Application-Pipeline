"""Application fill packets. Stage 5 Lane A, scoped to fill-and-stop.

Produces a mapping of {form field -> value} for one posting, from
`profile/facts.yaml`. Nothing here opens a browser or touches an application;
it builds the packet and reports what it could not answer. Claude in Chrome
does the typing, reading the packet from the local API.

There is no submit path in this module and there will not be one. That click
stays human.

Greenhouse form shape verified live 2026-08-26 against
boards-api.greenhouse.io/v1/boards/spacex/jobs/8719858002?questions=true:
`questions` is a list of {label, required, fields:[{name, type, values}]}, with
types input_text, input_file, textarea and multi_value_single_select. The
per-job question ids are opaque and company-specific
(`question_37828394002`), so every rule below matches on the human label.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from .config import ROOT, facts
from .http import FetchError, get_json

# Fields we refuse to fill, whatever the form calls them. Each is either the
# user's stated policy or something no automation should ever type.
NEVER_FILL = [
    (re.compile(r"salary|compensation|pay\s*rate|desired\s*pay|expected\s*pay", re.I),
     "policy: leave salary blank unless the form refuses to submit"),
    (re.compile(r"\bssn\b|social\s*security|tax\s*id", re.I),
     "never automate a government identifier"),
    (re.compile(r"password|passcode", re.I), "never automate credentials"),
    (re.compile(r"date\s*of\s*birth|\bdob\b", re.I), "not on file, and sensitive"),
    (re.compile(r"driver.?s?\s*licen[cs]e|passport", re.I),
     "never automate a government identifier"),
    (re.compile(r"bank|routing|account\s*number|card\s*number", re.I),
     "never automate financial details"),
    # "Initials" is an attestation field. "Middle Initial" is not, and the
    # first version of this pattern refused to fill it.
    (re.compile(r"^(?!.*\bmiddle\b).*(?:signature|e-?sign|\binitials?\b)", re.I),
     "a signature is an attestation; it stays human"),
    # "I acknowledge that I have reviewed the compensation range" is an
    # attestation about something a human read. Ticking it automatically would
    # be asserting something that did not happen.
    (re.compile(r"i\s+(acknowledge|certify|agree|consent|attest|confirm)\b", re.I),
     "an acknowledgement is a statement by you; tick it yourself"),
    (re.compile(r"terms\s*(and|&)\s*conditions|privacy\s*policy", re.I),
     "consent is never automated"),
]


def _yesno(value: bool) -> str:
    return "Yes" if value else "No"





def _rules(f: dict) -> list[tuple[re.Pattern, object, str]]:
    """(label pattern, value, note). First match wins, so order matters."""
    i, a, ed = f["identity"], f["address"], f["education"]
    wa, av, ca = f["work_authorization"], f["availability"], f["common_answers"]
    eeo, docs = f["eeo"], f["documents"]

    def p(pattern):
        return re.compile(pattern, re.I)

    return [
        # -- questions that a broader rule below would otherwise mis-answer --
        # "Can you perform the essential functions ... with or without
        # reasonable accommodation" contains the word "accommodation", and the
        # accommodation rule answered it "No". The correct answer is Yes, and a
        # wrong answer here is worse than a blank one.
        (p(r"perform\s+(all\s+of\s+)?the\s+essential\s+functions"), "Yes", ""),
        # \b matters: without it "graduate" matches inside "Undergraduate" and
        # a real undergraduate GPA gets answered "Not applicable".
        (p(r"gpa.*\b(graduate|masters|doctorate|phd)\b|"
           r"\b(graduate|masters|doctorate|phd)\b.*gpa"),
         ["Not applicable", "Other/Not Applicable", "N/A", "Do not recall"],
         "no graduate degree"),

        # -- documents, before any name rule can eat "resume" --
        (p(r"resume|cv\b|curriculum"), docs["resume"], "file upload"),
        (p(r"cover\s*letter"), docs["cover_letter"], "none on file"),
        (p(r"transcript"), docs["transcript"], "none on file"),

        # -- name --
        (p(r"^\s*(legal\s+)?first\s*name|given\s*name"), i["legal_first_name"], ""),
        (p(r"^\s*(legal\s+)?last\s*name|family\s*name|surname"), i["legal_last_name"], ""),
        (p(r"preferred\s*(first\s*)?name|nickname|goes\s*by"), i["preferred_name"], ""),
        (p(r"middle\s*(initial|name)"), i["legal_middle_initial"],
         "initial only; a form wanting the full middle name is unanswered"),
        (p(r"\bsuffix\b"), i["suffix"], "none"),
        (p(r"pronoun"), i["pronouns"], ""),
        (p(r"full\s*name|^\s*name\s*$"),
         f"{i['legal_first_name']} {i['legal_last_name']}", ""),

        # -- contact --
        (p(r"e-?mail"), i["email"], ""),
        (p(r"phone|mobile|\bcell\b"), i["phone"], ""),
        (p(r"linked\s*in"), i["linkedin"], ""),
        (p(r"git\s*hub"), i["github"], ""),
        (p(r"portfolio|personal\s*(web)?site|\bwebsite\b|\burl\b"),
         i["portfolio"], "none on file"),

        # -- export control, BEFORE the citizenship rule --
        # Some boards ask "are you a citizen, national, or resident of Cuba,
        # Iran, North Korea, Syria...". The citizenship rule answered it with
        # the applicant's citizenship,
        # which is not an option and is not what the question asks.
        (p(r"export\s*control|"
           r"citizen.*national.*resident.*(following|any of these)"),
         ["None/Not applicable", "None", "Not applicable"],
         "US citizen, none of the listed countries"),
        (p(r"immigration\s*and\s*residency\s*status"),
         ["None/Not applicable", "I am a U.S. Citizen or Legal Permanent Resident.",
          "Not applicable"],
         "follows from answering None above"),
        # "Type N/A if not applicable" means type N/A, not a citizenship.
        (p(r"type\s*n/?\s*a\s*if\s*not\s*applicable|"
           r"indicate\s*the\s*applicable\s*countr"),
         ["N/A"], "not applicable; US citizen"),

        # -- restrictive agreements, phrased many ways --
        (p(r"non-?compete|"
           r"agreement.*(restrict|prevent|limit).*(ability|work|employment)|"
           r"(restrict|prevent).*accept\s*this\s*offer"),
         [_yesno(ca["non_compete"])], ""),

        # -- work authorization, BEFORE the address block --
        # Some boards ask "authorized to work in the city/country where this
        # position is located?", and the address rules answered it with the
        # applicant's city. A label about working somewhere is never an
        # address field.
        (p(r"legally\s*(authorized|entitled)|authoriz\w*\s*to\s*work|"
           r"work\s*authoriz|eligible\s*to\s*work"),
         ["Yes", "I am authorized to work in the United States for any employer",
          "Authorized to work for any employer",
          _yesno(wa["authorized_to_work_us"])],
         ""),
        (p(r"sponsor"),
         ["No", "I do not require sponsorship",
          _yesno(wa["requires_sponsorship_now"])],
         "answers 'do you require sponsorship'; a reversed question needs checking"),
        (p(r"clearance|\bts/sci\b|polygraph"),
         ["Never held a clearance", "None", wa["security_clearance"]], "none held"),
        (p(r"citizenship|citizen\b"), wa["citizenship"], ""),
        (p(r"visa\s*(status|transfer)"), _yesno(ca["requires_visa_transfer"]), ""),

        # -- education dates, before the generic start-date rule --
        # Greenhouse splits these into four required controls per school row.
        (p(r"end\s*date\s*month|graduation\s*month"), ed.get("end_month"), ""),
        (p(r"end\s*date\s*year|graduation\s*year"), ed.get("end_year"), ""),
        (p(r"start\s*date\s*month"), ed.get("start_month"), ""),
        (p(r"start\s*date\s*year"), ed.get("start_year"), ""),

        # -- screening questions seen on real boards --
        (p(r"(over|at\s*least)\s*18|18\s*years\s*of\s*age|age\s*of\s*majority"),
         ["Yes"], "born 2005 or earlier"),
        (p(r"nonimmigrant\s*visa|\bf-?1\b|\bcpt\b|\bopt\b|stem\s*opt"),
         ["No"], "US citizen, no visa involved"),
        (p(r"(work|worked).*(for|with).*(dealer|partner|supplier|vendor|"
           r"competitor|customer)"),
         ["No"], ""),
        (p(r"in-?office\s*policy|days\s*per\s*week\s*in\s*(the\s*)?office|"
           r"onsite\s*requirement|hybrid\s*(policy|schedule)"),
         ["Yes"], "willing to work onsite; check this one on the page"),
        (p(r"drug\s*(screen|test)|background\s*check"),
         ["Yes"], ""),

        # -- education level, before "degree" can be read as a plain text field --
        (p(r"highest\s*(level\s*of\s*)?(education|degree)"),
         ["Bachelor's Degree", "Bachelors Degree", "Bachelor of Science",
          "Bachelor"],
         "degree in progress"),

        # -- prior employment here --
        (p(r"(been\s*)?(employed|worked)\s*(by|at|for|with)\s+\w+\s*before|"
           r"(previously|ever).*(employed|worked)"),
         [_yesno(ca["previously_employed_here"])], ""),

        # -- address --
        (p(r"street|address\s*line\s*1|^\s*address"), a["line1"], ""),
        # Word boundaries are load-bearing: bare `unit` matched "United States"
        # and swallowed the work-authorization question.
        (p(r"address\s*line\s*2|\bapt\b|\bsuite\b|\bunit\b"), a["line2"], "none"),
        # A geocoded location picker, not a plain city box. It must carry the
        # state: a bare city name will match the wrong state's version of it.
        (p(r"location\s*\(?\s*city|city\s*/\s*(state|region)|"
           r"current\s*location|where\s*are\s*you\s*(located|based)"),
         [a.get("city_state"), a.get("city_state_long"), a["city"]],
         "state included so the picker cannot choose the wrong city"),
        (p(r"\bcity\b|\btown\b"), a["city"], ""),
        (p(r"\bstate\b|province|region"), a["state"], ""),
        (p(r"zip|postal"), a["postal_code"], ""),
        (p(r"country"), a["country"], ""),

        # -- education --
        (p(r"school|university|college|institution"),
         [ed.get("school_display"), ed["school"]],
         "campus named; pickers often list several campuses per university"),
        (p(r"degree"), ed["degree"], ""),
        (p(r"discipline|major|field\s*of\s*study|concentration"), ed["major"], ""),
        (p(r"\bgpa\b|grade\s*point"), ed["gpa"], ""),
        (p(r"(graduation|grad)\s*(date|year)|expected\s*grad|end\s*date"),
         ed["graduation_date"], ""),
        (p(r"education\s*start|start\s*date.*(school|university)"),
         ed["start_date"], ""),

        # -- work authorization --
        # -- availability --
        (p(r"(available|availability|earliest).*(start|date)|start\s*date"),
         av["earliest_start_date"], ""),
        (p(r"relocat"), _yesno(av["willing_to_relocate"]), ""),
        (p(r"travel"), _yesno(ca["willing_to_travel"]), ""),
        (p(r"notice\s*period"), ca["notice_period"], ""),
        (p(r"remote|hybrid|onsite|work\s*arrangement"),
         ", ".join(av["work_arrangement"]), "check against the form's options"),

        # -- common yes/no --
        (p(r"how\s*did\s*you\s*(first\s*)?hear|referral\s*source|lead\s*source"),
         ca["how_did_you_hear"], ""),
        # Grounded in previously_employed_here: false, not an assumption.
        (p(r"employment\s*history"), ["None", "N/A"],
         "no prior employment at this company"),
        (p(r"related\s*to.*(employee|anyone)|know\s*anyone"),
         _yesno(ca["related_to_employee"]), ""),
        (p(r"non-?compete"), _yesno(ca["non_compete"]), ""),
        (p(r"felony|convicted|criminal"), _yesno(ca["felony_conviction"]), ""),
        (p(r"currently\s*employed"), _yesno(ca["currently_employed"]), ""),
        (p(r"current\s*(employer|company)"), ca["current_employer"], "none"),
        (p(r"assessment|coding\s*challenge"),
         _yesno(ca["willing_to_complete_assessment"]), ""),
        (p(r"accommodation"), ca["accommodation_request"], ""),
        (p(r"language|fluen|bilingual"),
         ", ".join(l["language"] for l in f["languages"]), ""),

        # -- EEO. Answered by choice; every one of these may be declined. --
        # Option wording varies between boards, so each rule carries the common
        # phrasings and the first one the form actually offers wins.
        (p(r"veteran"),
         [eeo["veteran_status"], "I am not a protected veteran",
          "Not a protected veteran", "No"], "EEO"),
        (p(r"disability"),
         [eeo["disability_status"],
          "No, I do not have a disability and have not had one in the past",
          "No, I don't have a disability, or a history/record of having a disability",
          "No"], "EEO"),
        (p(r"hispanic|latino"),
         [eeo["hispanic_latino"], "No", "Not Hispanic or Latino"], "EEO"),
        (p(r"\brace\b|ethnic"),
         [eeo["race_ethnicity"], "White", "White (Not Hispanic or Latino)"], "EEO"),
        (p(r"gender|\bsex\b"), [eeo["gender"], "Male", "Man"], "EEO"),
    ]


def best_option(value, options: list[str]) -> str | None:
    """Pick the closest option a select actually offers.

    Never invents a value: if nothing reasonably matches, the field comes back
    unanswered rather than filled with something the form does not accept.
    """
    if value is None or not options:
        return None
    want = str(value).strip().lower()
    for opt in options:                       # exact
        if opt.strip().lower() == want:
            return opt
    for opt in options:                       # containment either way
        low = opt.strip().lower()
        if want and (want in low or low in want):
            return opt
    want_words = {w for w in re.split(r"\W+", want) if len(w) > 3}
    best, best_score = None, 0
    for opt in options:
        words = {w for w in re.split(r"\W+", opt.lower()) if len(w) > 3}
        score = len(want_words & words)
        if score > best_score:
            best, best_score = opt, score
    return best


@dataclass
class Field:
    label: str
    name: str | None = None
    type: str = "input_text"
    required: bool = False
    options: list = dc_field(default_factory=list)
    value: object = None
    note: str = ""
    status: str = "unanswered"   # filled | unanswered | skipped | review


_LEADING_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)?)")


def nearest_numeric_option(value, options: list[str]) -> str | None:
    """For selects that are really a number picker, like GPA bands.

    Some boards offer "3.9 out of 4.0", "3.8 out of 4.0" and so on.
    String matching gets this wrong; arithmetic gets it right.
    """
    try:
        want = float(str(value))
    except (TypeError, ValueError):
        return None
    numeric = []
    for opt in options:
        m = _LEADING_NUMBER.match(opt)
        if m:
            numeric.append((abs(float(m.group(1)) - want), opt))
    if not numeric:
        return None
    return min(numeric)[1]


def _pick(value, options: list[str]) -> tuple[str | None, bool]:
    """Return (option, was_exact). `value` may be a list of candidate phrasings."""
    candidates = value if isinstance(value, list) else [value]
    for cand in candidates:
        if cand in (None, "", []):
            continue
        picked = best_option(cand, options)
        if picked is not None:
            exact = picked.strip().lower() == str(cand).strip().lower()
            return picked, exact
    numeric = nearest_numeric_option(
        candidates[0] if candidates else None, options)
    if numeric is not None:
        return numeric, False
    return None, False


def resolve(fld: Field, rules) -> Field:
    """Answer one field, or explain why it was left alone."""
    for pattern, reason in NEVER_FILL:
        if pattern.search(fld.label):
            fld.status, fld.note = "skipped", reason
            return fld

    for pattern, value, note in rules:
        if not pattern.search(fld.label):
            continue

        first = value[0] if isinstance(value, list) and value else value
        if (value in (None, "", []) or first in (None, "", [])):
            fld.status = "unanswered"
            fld.note = note or "nothing on file"
            return fld

        # A resume that arrives as a paste-the-text box is not the upload field.
        looks_like_path = isinstance(first, str) and first.endswith(
            (".pdf", ".doc", ".docx"))
        if looks_like_path and fld.type != "input_file":
            fld.status = "unanswered"
            fld.note = "paste-the-text variant; the file upload covers this"
            return fld
        if fld.type == "input_file" and not looks_like_path:
            fld.status = "unanswered"
            fld.note = "upload field with no file on file"
            return fld

        if fld.options:
            picked, exact = _pick(value, fld.options)
            if picked is None:
                fld.status = "review"
                fld.note = f"no option matches {first!r}"
                return fld
            fld.value, fld.status = picked, "filled"
            fld.note = note if exact else f"closest option to {first!r}"
            return fld

        fld.value, fld.status, fld.note = first, "filled", note
        return fld

    fld.status = "unanswered"
    fld.note = "no rule matches this label"
    return fld


# ---------------------------------------------------------------- Greenhouse

def greenhouse_form(slug: str, job_id: str, timeout: int = 30) -> list[Field]:
    """Read a Greenhouse application form. Verified live; see module docstring."""
    url = (f"https://boards-api.greenhouse.io/v1/boards/{slug}"
           f"/jobs/{job_id}?questions=true")
    payload, _ = get_json(url, timeout=timeout)
    out: list[Field] = []
    for q in payload.get("questions", []) or []:
        label = (q.get("label") or "").strip()
        for f in q.get("fields", []) or []:
            out.append(Field(
                label=label,
                name=f.get("name"),
                type=f.get("type") or "input_text",
                required=bool(q.get("required")),
                options=[v.get("label") for v in (f.get("values") or [])
                         if v.get("label")],
            ))
    # Demographic questions are optional everywhere and decline is always valid.
    for q in (payload.get("demographic_questions") or {}).get("questions", []) or []:
        out.append(Field(
            label=(q.get("label") or "").strip(),
            name=f"demographic_{q.get('id')}",
            type="multi_value_single_select",
            required=bool(q.get("required")),
            options=[a.get("label") for a in (q.get("answer_options") or [])
                     if a.get("label")],
        ))
    return out


def form_url(url: str | None) -> str | None:
    """Point at the application form rather than the job description.

    Aggregator rows link to the posting, and Lever and Ashby put the form on a
    separate path. The extension copes with landing on the description now, but
    opening the form directly saves a click.
    """
    if not url:
        return url
    base = url.split("#")[0].rstrip("/")
    low = base.lower()
    if "jobs.lever.co" in low and not low.endswith("/apply"):
        return base + "/apply"
    if "ashbyhq.com" in low and not low.endswith("/application"):
        return base + "/application"
    return url


_GH_POST_ID = re.compile(r"(?:gh_jid=|/jobs/)(\d{5,})")


def greenhouse_post_id(job: dict) -> str | None:
    """The board post id the questions endpoint wants.

    `req_id` holds Greenhouse's `requisition_id`, which is shared across every
    city a req is posted to and is NOT what /jobs/{id} accepts — passing it
    returns 404. The board post id is the one in the posting URL, and it is
    what job_id was hashed from.
    """
    for candidate in (job.get("url"), job.get("apply_url")):
        if candidate:
            m = _GH_POST_ID.search(candidate)
            if m:
                return m.group(1)
    return None


FORM_READERS = {"greenhouse": greenhouse_form}


@dataclass
class Packet:
    job_id: str
    company: str = ""
    title: str = ""
    apply_url: str = ""
    ats_vendor: str = ""
    source: str = "unknown"     # api | dom-required
    fields: list = dc_field(default_factory=list)
    resume_path: str | None = None
    error: str | None = None

    def counts(self) -> dict:
        out = {"filled": 0, "unanswered": 0, "skipped": 0, "review": 0}
        for f in self.fields:
            out[f.status] = out.get(f.status, 0) + 1
        out["total"] = len(self.fields)
        out["required_unanswered"] = sum(
            1 for f in self.fields if f.required and f.status != "filled")
        return out

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id, "company": self.company, "title": self.title,
            "apply_url": self.apply_url, "ats_vendor": self.ats_vendor,
            "source": self.source, "error": self.error,
            "resume_path": self.resume_path,
            "counts": self.counts(),
            "never_submit": True,
            "fields": [{"label": f.label, "name": f.name, "type": f.type,
                        "required": f.required, "options": f.options,
                        "value": f.value, "status": f.status, "note": f.note}
                       for f in self.fields],
        }


def build(job: dict, timeout: int = 30) -> Packet:
    """Everything known about filling one application."""
    f = facts()
    resume = f["documents"]["resume"]
    resume_abs = (ROOT / resume) if resume else None
    packet = Packet(
        job_id=job["job_id"], company=job.get("company") or "",
        title=job.get("title") or "",
        apply_url=form_url(job.get("apply_url") or job.get("url")) or "",
        ats_vendor=job.get("ats_vendor") or "",
        resume_path=str(resume_abs) if resume_abs and resume_abs.exists() else None,
    )
    if resume and not (resume_abs and resume_abs.exists()):
        packet.error = f"resume missing at {resume}"

    reader = FORM_READERS.get(packet.ats_vendor)
    if reader is None:
        # Lever and Ashby render their forms client-side and publish no field
        # schema. Claude reads the rendered page instead; the values below are
        # still the answers, they just have to be matched to labels in the DOM.
        packet.source = "dom-required"
        packet.fields = [resolve(Field(label=lbl), _rules(f))
                         for lbl in COMMON_LABELS]
        return packet

    external_id = greenhouse_post_id(job) if packet.ats_vendor == "greenhouse" \
        else job.get("req_id")
    if not external_id:
        packet.source = "dom-required"
        packet.error = "could not find the board post id in the posting URL"
        packet.fields = [resolve(Field(label=lbl), _rules(f))
                         for lbl in COMMON_LABELS]
        return packet

    try:
        fields = reader(job["company_slug"], external_id, timeout=timeout)
        packet.source = "api"
    except (FetchError, KeyError, TypeError) as e:
        packet.source = "dom-required"
        packet.error = f"form fetch failed ({e}); fall back to reading the page"
        fields = [Field(label=lbl) for lbl in COMMON_LABELS]

    rules = _rules(f)
    packet.fields = [resolve(fld, rules) for fld in fields]

    # Greenhouse's questions API lists the job's custom questions but not the
    # standard fields its own page renders — Country, Location, School, Degree
    # and the whole EEO block were all missing when this was filled by hand.
    # Append answers for anything the API did not mention so the extension has
    # them when it meets those fields on the page.
    seen = {_norm_label(fld.label) for fld in packet.fields}
    for label in COMMON_LABELS:
        if _norm_label(label) in seen:
            continue
        extra = resolve(Field(label=label), rules)
        if extra.status == "filled":
            extra.note = (extra.note + "; not in the API, expected on the page").strip("; ")
            packet.fields.append(extra)
    return packet


def _norm_label(label: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (label or "").lower()).split())


# Used when no field schema is available. These are the labels that appear on
# essentially every ATS application, so Claude has answers ready before it
# reads the page.
#: Labels present on essentially every ATS application. Used as the whole field
#: list when no schema is available, and as a top-up when one is.
COMMON_LABELS = [
    # Greenhouse's questions API omits the standard fields its rendered form
    # adds (Country, Location, School, Degree). They belong here so the packet
    # has answers ready regardless of which path found the form.
    # Lever asks for one "Full name" field, not First/Last. Without this the
    # required name field on every Lever form went unanswered.
    "Full Name",
    "First Name", "Last Name", "Preferred Name", "Middle Initial", "Email", "Phone",
    "Resume/CV", "Cover Letter", "LinkedIn Profile", "GitHub",
    "Website / Portfolio", "Address", "City", "State", "Zip / Postal Code",
    "Country", "Location (City)", "School", "Degree", "Discipline / Major", "GPA",
    "Start date month", "Start date year", "End date month", "End date year",
    "Expected Graduation Date", "Earliest Start Date",
    "Are you legally authorized to work in the United States?",
    "Will you now or in the future require sponsorship?",
    "How did you hear about this job?", "Are you willing to travel?",
    "Are you willing to relocate?", "Pronouns",
    "Gender", "Race / Ethnicity", "Veteran Status", "Disability Status",
]


def instructions(packet: Packet) -> str:
    """The prompt handed to Claude in Chrome. The submit ban is stated twice."""
    c = packet.counts()
    return "\n".join([
        f"Fill in this job application. DO NOT SUBMIT IT.",
        "",
        f"Job: {packet.title} at {packet.company}",
        f"Form: {packet.apply_url}",
        f"Field data: http://127.0.0.1:8765/api/job/{packet.job_id}/fillpacket",
        "",
        "Rules:",
        "1. Never click Submit, Apply, Send, or any final action. Stop when the "
        "form is filled and take a screenshot.",
        "2. Use only the values in the packet. If a field has no value, leave it "
        "empty and list it for me rather than guessing.",
        "3. Fields marked `skipped` are deliberate. Do not fill them, and do not "
        "work around them.",
        "4. Upload the resume from the `resume_path` in the packet.",
        "5. Do not accept terms, tick consent boxes, or create an account.",
        "",
        f"{c['filled']} of {c['total']} fields have answers. "
        f"{c['required_unanswered']} required fields do not.",
    ])
