"""Source registry.

Every ingester exposes:
    VENDOR: str
    url(slug) -> str
    parse(payload, slug) -> list[dict]   # canonical job dicts

Field shapes below were verified against live endpoints on 2026-08-25, not
copied from documentation. Re-run `jobs probe <vendor> <slug>` before trusting
a parser after any breakage.
"""
from . import ashby, greenhouse, lever, simplify

VENDORS = {
    greenhouse.VENDOR: greenhouse,
    lever.VENDOR: lever,
    ashby.VENDOR: ashby,
}

__all__ = ["VENDORS", "greenhouse", "lever", "ashby", "simplify"]
