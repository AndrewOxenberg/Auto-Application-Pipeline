"""Thin JSON fetcher. stdlib only, so the ingest path has no dependencies."""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request

UA = "job-pipeline/0.1 (personal job search; one user, polite rate)"


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def get_json(url: str, timeout: int = 30, retries: int = 2, backoff: float = 1.5):
    """Return (payload, http_status). Raises FetchError after the last retry."""
    last: Exception | None = None
    status: int | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json",
                          "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8", "replace")), status
        except urllib.error.HTTPError as e:
            status = e.code
            last = FetchError(f"HTTP {e.code}", e.code)
            if e.code in (404, 401, 403, 410):
                break  # a wrong slug will not fix itself on retry
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last = FetchError(f"{type(e).__name__}: {e}", status)
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    raise FetchError(str(last), status)
