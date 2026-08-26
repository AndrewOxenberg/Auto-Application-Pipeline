"""Small helpers shared across the pipeline."""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone

_TAG_RE = re.compile(r"<[^>]+>")
# Ashby postings sometimes embed a whole PNG as a data URI. One of them was
# 40KB of base64 in the middle of the description, which is pure noise in a
# scoring prompt and pure cost when that prompt goes to an API.
_DATA_URI_RE = re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{100,}",
                          re.I)
_WS_RE = re.compile(r"[ \t\r\f\v   ]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_description(text: str | None) -> str:
    """Drop embedded data URIs from a description.

    Ashby serves `descriptionPlain` with data URIs left in. One persona.ai
    posting was 40KB of base64 PNG in the middle of the text: noise in a
    scoring prompt, and money when that prompt goes to an API.
    """
    if not text:
        return ""
    return _DATA_URI_RE.sub("[embedded image removed]", text)


def strip_html(raw: str | None) -> str:
    """ATS descriptions arrive as escaped HTML. Flatten to plain text."""
    if not raw:
        return ""
    text = clean_description(html.unescape(raw))
    text = re.sub(r"<\s*(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return _BLANKS_RE.sub("\n\n", text).strip()


def job_id(ats_vendor: str, company_slug: str, req_id: str) -> str:
    key = f"{ats_vendor}|{company_slug}|{req_id}".lower()
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def content_hash(title: str, location: str, description: str) -> str:
    blob = f"{title}\x1f{location}\x1f{description}".lower()
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


_TITLE_NOISE = re.compile(
    r"\b(job|req|requisition|posting)?\s*#?\s*\d{3,}\b|\(.*?\)|\[.*?\]", re.I
)


def norm_title(title: str) -> str:
    t = _TITLE_NOISE.sub(" ", title or "")
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    t = re.sub(r"\b(i|ii|iii|iv|1|2|3)\b", " ", t)
    return " ".join(t.split())


_STATE_FIX = {
    "washington dc": "dc", "washington d c": "dc", "district of columbia": "dc",
    "new york city": "new york", "nyc": "new york", "sf": "san francisco",
    "usa": "", "united states": "", "us": "",
}


_SINGLE_LETTERS = re.compile(r"\b(?:[a-z] ){1,3}[a-z]\b")


def norm_location(loc: str) -> str:
    l = re.sub(r"[^a-z0-9 ]+", " ", (loc or "").lower())
    l = " ".join(l.split())
    # "d c" -> "dc": punctuation stripping shatters initialisms like D.C.
    l = _SINGLE_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), l)
    for k, v in _STATE_FIX.items():
        l = re.sub(rf"\b{re.escape(k)}\b", v, l)
    return " ".join(l.split())


def dedup_key(company: str, title: str, location: str) -> str:
    blob = f"{norm_title(company)}|{norm_title(title)}|{norm_location(location)}"
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def epoch_ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).replace(
            microsecond=0).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def epoch_s_to_iso(s) -> str | None:
    if not s:
        return None
    try:
        return datetime.fromtimestamp(int(s), timezone.utc).replace(
            microsecond=0).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def iso_normalize(value) -> str | None:
    """Accept the three date shapes the ATS APIs actually return."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return epoch_ms_to_iso(value) if value > 1e11 else epoch_s_to_iso(value)
    s = str(value).strip()
    if s.isdigit():
        return iso_normalize(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def days_since(iso: str | None) -> float | None:
    dt = iso_normalize(iso)
    if not dt:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(dt)).total_seconds() / 86400


def age_text(iso: str | None) -> str:
    """Hours under a day, days above it.

    Being early is the whole point of the pipeline, so a posting that went up
    two hours ago and one that went up twenty-two must not both read "0d".
    Every source stores a full timestamp, so the hours are real.
    """
    days = days_since(iso)
    if days is None:
        return "?"
    if days < 0:
        return "0h"          # a source with a clock ahead of ours
    hours = days * 24
    if hours < 1:
        return "<1h"
    if days < 1:
        return f"{int(hours)}h"
    return f"{int(days)}d"
