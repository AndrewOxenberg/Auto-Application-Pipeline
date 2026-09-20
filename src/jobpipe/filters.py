"""Tier-1 deterministic filter.

Nothing here calls an LLM. Every rejection names the rule that fired, so a rule
that quietly eats good postings is visible instead of invisible — that is the
most expensive kind of bug in this system.
"""
from __future__ import annotations

import re
import sqlite3

from .config import rules
from .util import content_hash, days_since, norm_location

# "5+ years", "5-7 years", "minimum of 5 years of relevant experience"
_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*(?:or more\s*)?years?"
    r"(?:\s+of)?(?:\s+[\w-]+){0,3}?\s+experience", re.I)
_GRAD_RE = re.compile(r"\b20(2[4-9]|3[0-2])\b")


def _compiled(patterns):
    return [re.compile(p, re.I) for p in patterns]


class Tier1:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or rules()
        t = self.cfg.get("title", {})
        self.allow = _compiled(t.get("allow", []))
        self.deny = _compiled(t.get("deny", []))
        self.hard = [p.lower() for p in self.cfg.get("hard_reject_phrases", [])]
        emp = self.cfg.get("employment", {})
        self.no_interns = emp.get("exclude_internships", False)
        self.intern_title = _compiled(emp.get("internship_title_patterns", []))
        self.intern_types = {t.lower() for t in
                             emp.get("internship_employment_types", [])}
        refs = self.cfg.get("references", {})
        self.no_refs = refs.get("exclude_reference_required", False)
        self.ref_patterns = _compiled(refs.get("patterns", []))
        self.max_years = self.cfg.get("experience", {}).get("max_years_required", 2)
        loc = self.cfg.get("location", {})
        self.loc_allow = [s.lower() for s in loc.get("allow_substrings", [])]
        self.loc_deny = [s.lower() for s in loc.get("deny_substrings", [])]
        self.loc_remote = loc.get("allow_remote", True)
        self.loc_unknown = loc.get("keep_unknown", True)
        self.max_days = self.cfg.get("posting_age", {}).get("max_days")
        gw = self.cfg.get("grad_window", {})
        self.grad_lo, self.grad_hi = gw.get("earliest"), gw.get("latest")
        self.max_reposts = self.cfg.get("ghost_jobs", {}).get("max_reposts", 3)

    # -- individual rules -------------------------------------------------
    def _title(self, title: str) -> str | None:
        for p in self.deny:
            if p.search(title):
                return f"title-deny:{p.pattern}"
        if self.allow and not any(p.search(title) for p in self.allow):
            return "title-not-allowed"
        return None

    def _internship(self, title: str, employment_type: str | None) -> str | None:
        """Graduation is May 2027, so a summer 2027 internship lands after it.

        Two independent signals: the title, and whatever the ATS put in
        employment_type. Either one is enough, because plenty of boards ship a
        clean "Software Engineer" title under an Internship employment type.
        """
        if not self.no_interns:
            return None
        for p in self.intern_title:
            if p.search(title):
                return f"internship:title:{p.pattern}"
        etype = (employment_type or "").strip().lower()
        if etype and etype in self.intern_types:
            return f"internship:type:{etype}"
        return None

    def _references(self, desc: str) -> str | None:
        """Postings that want references at the application stage.

        There are none on file. The patterns live in config and are narrow by
        design: a bare "references" match would kill any posting that mentions
        circular references or a reference architecture.
        """
        if not self.no_refs or not desc:
            return None
        for p in self.ref_patterns:
            m = p.search(desc)
            if m:
                return f"references:{' '.join(m.group(0).lower().split())[:32]}"
        return None

    def _years(self, desc: str) -> str | None:
        for m in _YEARS_RE.finditer(desc or ""):
            try:
                yrs = int(m.group(1))
            except ValueError:
                continue
            if yrs > self.max_years:
                return f"yoe:{yrs}y-required"
        return None

    def _phrases(self, desc: str) -> str | None:
        low = (desc or "").lower()
        for phrase in self.hard:
            if phrase in low:
                return f"phrase:{phrase}"
        return None

    @staticmethod
    def _matches(norm: str, tokens: set[str], substrings: list[str]) -> str | None:
        """Single words match whole tokens; multi-word entries match as phrases.
        Without the token rule, "ca" matches "canada" and the filter leaks."""
        for sub in substrings:
            if (sub in norm) if " " in sub else (sub in tokens):
                return sub
        return None

    def _location(self, location: str, remote_flag: int) -> str | None:
        norm = norm_location(location)
        if not norm:
            return None if self.loc_unknown else "location-unknown"
        tokens = set(norm.split())
        # An allowed city wins first: multi-site postings routinely list
        # "Cambridge MA, Seattle WA, Toronto ON" and one foreign office in the
        # list should not disqualify the US ones.
        if self.loc_allow and self._matches(norm, tokens, self.loc_allow):
            return None
        hit = self._matches(norm, tokens, self.loc_deny)
        if hit:
            return f"location-deny:{hit}"
        if remote_flag and self.loc_remote:
            return None
        if not self.loc_allow:
            return None
        return f"location:{norm[:40]}"

    def _age(self, posted_at: str | None) -> str | None:
        if not self.max_days or not posted_at:
            return None
        age = days_since(posted_at)
        if age is not None and age > self.max_days:
            return f"stale:{int(age)}d"
        return None

    def _grad(self, title: str, desc: str) -> str | None:
        """Reject only when the posting names grad years and none of them fit."""
        if not self.grad_lo:
            return None
        blob = f"{title}\n{(desc or '')[:4000]}"
        years = {int(f"20{m.group(1)}") for m in _GRAD_RE.finditer(blob)}
        if not years:
            return None
        lo, hi = int(self.grad_lo[:4]), int(self.grad_hi[:4])
        if any(lo <= y <= hi for y in years):
            return None
        return f"grad-window:{sorted(years)}"

    def _ghost(self, repost_count: int) -> str | None:
        if repost_count and repost_count > self.max_reposts:
            return f"ghost:{repost_count}-reposts"
        return None

    # -- entry point ------------------------------------------------------
    def evaluate(self, job: dict) -> tuple[str, str | None]:
        title = job.get("title") or ""
        desc = job.get("description_text") or ""
        for reason in (
            self._title(title),
            self._internship(title, job.get("employment_type")),
            self._phrases(desc),
            self._references(desc),
            self._years(desc),
            self._grad(title, desc),
            self._location(job.get("location") or "", job.get("remote_flag") or 0),
            self._age(job.get("posted_at")),
            self._ghost(job.get("repost_count") or 0),
        ):
            if reason:
                return "reject", reason
        return "pass", None


def _stripped(r) -> bool:
    """True when the description was dropped to keep the hosted engine file small
    (feature 20), as opposed to a posting that never had one. content_hash covers
    title, location and description exactly as stored, so a genuinely empty
    description still hashes to the empty form."""
    if r["description_text"] is not None or not r["content_hash"]:
        return False
    return r["content_hash"] not in (content_hash(r["title"], r["location"], ""),
                                     content_hash(r["title"], r["location"], None))


def apply(conn: sqlite3.Connection, only_new: bool = False) -> dict:
    t1 = Tier1()
    where = "WHERE is_open=1" + (" AND filter_verdict IS NULL" if only_new else "")
    rows = conn.execute(
        f"SELECT job_id, title, description_text, location, remote_flag, posted_at, "
        f"repost_count, employment_type, content_hash, filter_verdict FROM jobs {where}"
    ).fetchall()
    counts = {"pass": 0, "reject": 0, "kept": 0}
    for r in rows:
        # A stripped row judged without its text would lose every description
        # rule. Keep its verdict; the next fetch restores the text.
        if r["filter_verdict"] is not None and _stripped(r):
            counts["kept"] += 1
            continue
        verdict, reason = t1.evaluate(dict(r))
        counts[verdict] += 1
        conn.execute("UPDATE jobs SET filter_verdict=?, filter_reason=? WHERE job_id=?",
                     (verdict, reason, r["job_id"]))
    conn.commit()
    counts["evaluated"] = len(rows) - counts["kept"]
    return counts
