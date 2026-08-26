"""Greenhouse job boards API.

Verified live 2026-08-25 against boards-api.greenhouse.io/v1/boards/stripe.
Payload: {"jobs": [{id, title, absolute_url, location:{name}, content (escaped
HTML), updated_at, first_published, requisition_id, departments:[{name}], ...}]}
A wrong slug returns HTTP 404, not an empty array.
"""
from __future__ import annotations

from ..util import content_hash, dedup_key, iso_normalize, job_id, strip_html

VENDOR = "greenhouse"


def url(slug: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def _location(j: dict) -> str:
    loc = (j.get("location") or {}).get("name") or ""
    if not loc:
        offices = [o.get("name", "") for o in (j.get("offices") or [])]
        loc = ", ".join(x for x in offices if x and x != "No Office")
    return loc.strip()


def parse(payload, slug: str) -> list[dict]:
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    out = []
    for j in jobs:
        # Identity comes from the board post id, not requisition_id: one req can
        # be posted to many locations and they share a requisition_id, which
        # silently collapsed distinct postings into one row.
        post_id = str(j.get("id") or "")
        if not post_id:
            continue
        req = str(j.get("requisition_id") or post_id)
        title = (j.get("title") or "").strip()
        loc = _location(j)
        desc = strip_html(j.get("content"))
        company = j.get("company_name") or slug
        dept = ", ".join(d.get("name", "") for d in (j.get("departments") or []))
        out.append({
            "job_id": job_id(VENDOR, slug, post_id),
            "company": company,
            "company_slug": slug,
            "title": title,
            "location": loc,
            "remote_flag": int("remote" in loc.lower()),
            "req_id": req,
            "url": j.get("absolute_url"),
            "apply_url": j.get("absolute_url"),
            "ats_vendor": VENDOR,
            "department": dept or None,
            "employment_type": None,
            "description_text": desc,
            "posted_at": iso_normalize(j.get("first_published") or j.get("updated_at")),
            "content_hash": content_hash(title, loc, desc),
            "dedup_key": dedup_key(company, title, loc),
        })
    return out
