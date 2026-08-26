"""Entry point: python jobs.py <command>"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from jobpipe.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
