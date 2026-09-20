"""Hold the offline scorer to account against the 49 LLM-scored rows.

Not a test. A test would have to assert a threshold, and the useful output
here is the shape of the disagreement: which postings the rules put in the
wrong half, and why. Run it after touching any weight in offline_score.

    .venv/Scripts/python.exe scripts/calibrate_score.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpipe import offline_score  # noqa: E402


def spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation, computed here so the tool stays stdlib-only."""
    def ranks(vs):
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        r = [0.0] * len(vs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            shared = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = shared
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def main() -> int:
    db = Path(__file__).resolve().parents[1] / "data" / "jobs.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT job_id, company, title, location, remote_flag, employment_type, "
        "repost_count, description_text, fit_score, score_json "
        "FROM jobs WHERE fit_score IS NOT NULL").fetchall()

    pairs = []
    for r in rows:
        payload = json.loads(r["score_json"])
        # Once the rescore has run, the LLM number lives under _prev_llm.
        llm = payload.get("_prev_llm", {}).get("fit_score")
        if llm is None:
            llm = r["fit_score"] if payload.get("_model") != offline_score.MODEL else None
        if llm is None:
            continue
        mine = offline_score.score_job(dict(r))
        pairs.append((llm, mine["fit_score"], r["title"], mine))

    if not pairs:
        print("no LLM-scored rows to calibrate against.")
        return 1

    llm = [p[0] for p in pairs]
    off = [p[1] for p in pairs]
    n = len(pairs)
    mae = sum(abs(a - b) for a, b in zip(llm, off)) / n

    print(f"n = {n}")
    print(f"spearman rank correlation  {spearman(llm, off):+.3f}")
    print(f"mean absolute error        {mae:.1f} points")
    print(f"llm    mean {sum(llm)/n:5.1f}  min {min(llm):3}  max {max(llm):3}")
    print(f"offline mean {sum(off)/n:5.1f}  min {min(off):3}  max {max(off):3}")

    # The number that actually matters: does it agree on which half a posting
    # belongs in? A ranking that sorts the top correctly can be wrong by ten
    # points everywhere and still do its job.
    median_llm = sorted(llm)[n // 2]
    median_off = sorted(off)[n // 2]
    agree = sum(1 for a, b in zip(llm, off)
                if (a >= median_llm) == (b >= median_off))
    print(f"same half of the list      {agree}/{n}  ({100*agree/n:.0f}%)")

    print("\nworst disagreements")
    for a, b, title, mine in sorted(pairs, key=lambda p: -abs(p[0] - p[1]))[:10]:
        sig = ", ".join(f"{s['label']} {s['delta']:+d}" for s in mine["_signals"]) or "none"
        print(f"  llm {a:3}  offline {b:3}  {title[:44]:44} [{sig[:78]}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
