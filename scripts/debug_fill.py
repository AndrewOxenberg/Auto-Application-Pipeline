"""Emit a self-contained JS blob that fills one real application form.

The blob is the shipping fill engine plus that job's packet plus the resume
bytes, so pasting it into a live form's console exercises exactly the code the
extension runs. Used to debug against real ATS pages, since an unpacked
extension cannot be loaded from an automated browser session.

    python scripts/debug_fill.py <job_id_prefix>       -> prints the blob
    python scripts/debug_fill.py --list [n]            -> candidate jobs
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobpipe import apply, db  # noqa: E402


def candidates(limit: int = 12) -> list[dict]:
    conn = db.connect()
    rows = conn.execute(
        "SELECT job_id, company, title, ats_vendor, apply_url, url FROM jobs "
        "WHERE is_open=1 AND duplicate_of IS NULL AND filter_verdict='pass' "
        "AND ats_vendor IN ('greenhouse','lever','ashby') "
        "GROUP BY ats_vendor, company ORDER BY ats_vendor, RANDOM() LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def blob(job_id_prefix: str, with_resume: bool = True) -> str:
    conn = db.connect()
    row = conn.execute(
        "SELECT * FROM jobs WHERE job_id LIKE ? LIMIT 1", (job_id_prefix + "%",)
    ).fetchone()
    if row is None:
        raise SystemExit(f"no job matching {job_id_prefix!r}")

    packet = apply.build(dict(row)).to_dict()
    # The engine reads options off the DOM, never from the packet, so they are
    # dead weight in a blob that has to be pasted into a console by hand.
    packet["fields"] = [
        {k: v for k, v in f.items() if k not in ("options", "note")}
        for f in packet["fields"]
        if f["status"] in ("filled", "skipped")
    ]
    engine = (ROOT / "extension" / "fill-engine.js").read_text(encoding="utf-8")
    # Strip comments and blank lines; the logic is unchanged and this is only
    # ever pasted, never shipped.
    engine = "\n".join(
        line for line in engine.split("\n")
        if line.strip() and not line.strip().startswith("//")
    )

    resume_js = "null"
    if with_resume == "stub":
        # A 700-byte valid PDF. The DataTransfer path is identical whatever the
        # bytes are, and inlining the real 81KB resume as base64 makes the blob
        # too large to paste through a tool call.
        stub = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
                b"trailer<</Root 1 0 R>>\n%%EOF\n")
        b64 = base64.b64encode(stub).decode()
        resume_js = "Uint8Array.from(atob('" + b64 + "'), c => c.charCodeAt(0))"
    elif with_resume:
        f = apply.facts()
        rel = (f.get("documents") or {}).get("resume")
        path = ROOT / rel if rel else None
        if path and path.exists():
            b64 = base64.b64encode(path.read_bytes()).decode()
            resume_js = (
                "Uint8Array.from(atob('" + b64 + "'), c => c.charCodeAt(0))"
            )

    return f"""
{engine}
(async () => {{
  const packet = {json.dumps(packet)};
  const report = await window.__jobpipe.fill(packet, {{
    resumeBytes: {resume_js},
    resumeName: "resume.pdf"
  }});
  window.__jobpipeReport = {{
    company: packet.company,
    vendor: packet.ats_vendor,
    source: packet.source,
    answers: packet.fields.filter(f => f.status === "filled").length,
    filled: report.filled,
    missed: report.missed,
    skipped: report.skipped,
    resume: report.resume,
    controls: report.seen.length
  }};
  return window.__jobpipeReport;
}})();
"""


if __name__ == "__main__":
    # The engine and packet both carry non-cp1252 characters; the Windows
    # console default encoding cannot print them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 12
        for j in candidates(n):
            print(f"{j['job_id'][:10]}  {j['ats_vendor']:<11} {j['company'][:24]:<24} "
                  f"{j['title'][:38]:<38} {j['apply_url'] or j['url']}")
    elif len(sys.argv) > 1:
        mode = "stub" if "--stub-resume" in sys.argv else True
        print(blob(sys.argv[1], with_resume=mode))
    else:
        raise SystemExit(__doc__)
