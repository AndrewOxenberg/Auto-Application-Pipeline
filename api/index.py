"""Vercel entrypoint for the hosted site (feature 20). Locally, use jobs.py.

Vercel's Python runtime loads the top-level `app` from api/*.py, a plain WSGI
callable here. vercel.json rewrites every path to this one function, which
sees the original path. Deploy with scripts/deploy-site.ps1, which uploads only
what the site needs: never profile/, data/ or config/.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpipe.hosted import app  # noqa: E402,F401
