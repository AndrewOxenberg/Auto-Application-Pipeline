"""Ashby job board posting API.

Verified live 2026-08-25 against api.ashbyhq.com/posting-api/job-board/ramp.
Payload: {"jobs": [{id, title, department, team, employmentType, location,
secondaryLocations:[{location,...}], isRemote, isListed, publishedAt, jobUrl,
applyUrl, descriptionPlain, descriptionHtml, workplaceType}]}
"""
from __future__ import annotations

from ..util import (clean_description, content_hash, dedup_key, iso_normalize,
                    job_id, strip_html)

VENDOR = "ashby"


def url(slug: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"


def parse(payload, slug: str) -> list[dict]:
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    out = []
    for j in jobs:
        if j.get("isListed") is False:
            continue  # unlisted reqs are not publicly applicable
        req = str(j.get("id") or "")
        if not req:
            continue
        title = (j.get("title") or "").strip()
        locs = [j.get("location") or ""] + [
            s.get("location", "") for s in (j.get("secondaryLocations") or [])]
        loc = ", ".join(dict.fromkeys(x.strip() for x in locs if x and x.strip()))
        desc = clean_description(j.get("descriptionPlain")) or strip_html(
            j.get("descriptionHtml"))
        out.append({
            "job_id": job_id(VENDOR, slug, req),
            "company": slug,
            "company_slug": slug,
            "title": title,
            "location": loc,
            "remote_flag": int(bool(j.get("isRemote")) or "remote" in loc.lower()),
            "req_id": req,
            "url": j.get("jobUrl"),
            "apply_url": j.get("applyUrl") or j.get("jobUrl"),
            "ats_vendor": VENDOR,
            "department": j.get("department") or j.get("team"),
            "employment_type": j.get("employmentType"),
            "description_text": desc,
            "posted_at": iso_normalize(j.get("publishedAt")),
            "content_hash": content_hash(title, loc, desc),
            "dedup_key": dedup_key(slug, title, loc),
        })
    return out
