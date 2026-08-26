"""Lever postings API.

Verified live 2026-08-25 against api.lever.co/v0/postings/palantir.
Payload: a bare JSON array of {id, text, hostedUrl, applyUrl, createdAt (epoch
ms), categories:{commitment, location, team, allLocations}, descriptionPlain,
additionalPlain, workplaceType}.

Failure mode to watch: a stale slug can return HTTP 200 with `[]` (confirmed
with slug "plaid"). That is why the health check treats an empty payload as a
warning, not a success.
"""
from __future__ import annotations

from ..util import (clean_description, content_hash, dedup_key, iso_normalize,
                    job_id)

VENDOR = "lever"


def url(slug: str) -> str:
    return f"https://api.lever.co/v0/postings/{slug}?mode=json"


def parse(payload, slug: str) -> list[dict]:
    if not isinstance(payload, list):
        return []
    out = []
    for j in payload:
        req = str(j.get("id") or "")
        if not req:
            continue
        cats = j.get("categories") or {}
        title = (j.get("text") or "").strip()
        locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        loc = ", ".join(x for x in locs if x)
        desc = "\n\n".join(x for x in (j.get("descriptionPlain"),
                                       j.get("additionalPlain")) if x)
        workplace = (j.get("workplaceType") or "").lower()
        out.append({
            "job_id": job_id(VENDOR, slug, req),
            "company": slug,
            "company_slug": slug,
            "title": title,
            "location": loc,
            "remote_flag": int(workplace == "remote" or "remote" in loc.lower()),
            "req_id": req,
            "url": j.get("hostedUrl"),
            "apply_url": j.get("applyUrl") or j.get("hostedUrl"),
            "ats_vendor": VENDOR,
            "department": cats.get("team"),
            "employment_type": cats.get("commitment"),
            "description_text": desc,
            "posted_at": iso_normalize(j.get("createdAt")),
            "content_hash": content_hash(title, loc, desc),
            "dedup_key": dedup_key(slug, title, loc),
        })
    return out
