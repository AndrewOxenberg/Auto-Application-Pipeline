"""SimplifyJobs GitHub lists.

Verified live 2026-08-25: both repos serve
  raw.githubusercontent.com/SimplifyJobs/<repo>/dev/.github/scripts/listings.json
New-Grad-Positions returned 18,962 rows, Summer2027-Internships 14,720.

Record: {id, source, company_name, company_url, title, locations[], url,
active, is_visible, date_posted (epoch s), date_updated, sponsorship,
degrees[], category, terms[] (internships only)}

Two jobs here: it is a discovery source in its own right, and its `url` field
encodes company -> ATS vendor + slug, which is how companies.yaml gets
bootstrapped instead of hand-typed.
"""
from __future__ import annotations

import re

from ..util import content_hash, dedup_key, iso_normalize, job_id

VENDOR = "simplify"

LISTS = {
    "newgrad": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "intern2027": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
}

_SLUG_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([^/?#]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([^/?#]+)", re.I)),
    ("workday", re.compile(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", re.I)),
]


def detect_ats(job_url: str) -> tuple[str, str] | None:
    """('greenhouse', 'stripe') from a posting URL, or None if unrecognized."""
    if not job_url:
        return None
    for vendor, pattern in _SLUG_PATTERNS:
        m = pattern.search(job_url)
        if m and m.groups():
            slug = m.group(1).strip("/")
            if slug and slug.lower() not in {"en-us", "www"}:
                return vendor, slug
    return None


def url(list_key: str) -> str:
    return LISTS[list_key]


def parse(payload, list_key: str) -> list[dict]:
    if not isinstance(payload, list):
        return []
    out = []
    for j in payload:
        if not j.get("active") or not j.get("is_visible"):
            continue
        req = str(j.get("id") or "")
        if not req:
            continue
        company = (j.get("company_name") or "").strip()
        title = (j.get("title") or "").strip()
        loc = ", ".join(j.get("locations") or [])
        posting_url = j.get("url") or ""
        ats = detect_ats(posting_url)
        terms = ", ".join(j.get("terms") or [])
        desc = " | ".join(x for x in [
            f"category: {j.get('category')}" if j.get("category") else "",
            f"terms: {terms}" if terms else "",
            f"sponsorship: {j.get('sponsorship')}" if j.get("sponsorship") else "",
            f"degrees: {', '.join(j.get('degrees') or [])}" if j.get("degrees") else "",
        ] if x)
        out.append({
            "job_id": job_id(VENDOR, list_key, req),
            "company": company,
            "company_slug": list_key,
            "title": title,
            "location": loc,
            "remote_flag": int("remote" in loc.lower()),
            "req_id": req,
            "url": posting_url,
            "apply_url": posting_url,
            "ats_vendor": VENDOR,
            "department": ats[0] if ats else None,   # detected downstream ATS
            "employment_type": "Internship" if list_key.startswith("intern") else "Full-time",
            # Aggregator rows carry no description. Tier-2 must fetch from the
            # real ATS before scoring, so leave this thin and honest.
            "description_text": desc,
            "posted_at": iso_normalize(j.get("date_posted")),
            "content_hash": content_hash(title, loc, desc),
            "dedup_key": dedup_key(company, title, loc),
        })
    return out
