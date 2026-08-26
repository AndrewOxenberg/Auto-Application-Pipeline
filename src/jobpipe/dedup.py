"""Cross-source dedup.

The same req shows up on the company board, the GitHub list, and an aggregator.
Exact dedup already happened on job_id at insert. This pass collapses the fuzzy
case: same (company, normalized title, normalized location) from different
vendors. The canonical row is the one from the real ATS with the earliest
first_seen_at, because that is the row with a usable description and apply URL.
"""
from __future__ import annotations

import sqlite3

# An aggregator row is a pointer, not a posting. Prefer a direct ATS row.
VENDOR_RANK = {"greenhouse": 0, "lever": 0, "ashby": 0, "simplify": 5}


def rebuild(conn: sqlite3.Connection) -> dict:
    conn.execute("UPDATE jobs SET duplicate_of=NULL")
    rows = conn.execute(
        "SELECT job_id, dedup_key, ats_vendor, first_seen_at FROM jobs "
        "WHERE is_open=1 AND dedup_key IS NOT NULL"
    ).fetchall()

    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["dedup_key"], []).append(r)

    marked = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (VENDOR_RANK.get(r["ats_vendor"], 9),
                                    r["first_seen_at"]))
        canonical = members[0]["job_id"]
        for dup in members[1:]:
            conn.execute("UPDATE jobs SET duplicate_of=? WHERE job_id=?",
                         (canonical, dup["job_id"]))
            marked += 1
    conn.commit()
    return {"groups": sum(1 for m in groups.values() if len(m) > 1), "marked": marked}
