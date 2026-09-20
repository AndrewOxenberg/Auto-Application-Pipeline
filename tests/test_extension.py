"""Runs the extension's JS tests as part of the one test command.

The Apply-click guards live in JavaScript because that is where the code runs,
but a test suite nobody remembers to invoke is not a test suite. This shells
out to node so `python -m unittest discover -s tests` covers both halves, and
skips cleanly on a machine without node rather than failing.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class ExtensionJS(unittest.TestCase):
    def test_apply_click_suite_passes(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("test_apply_click.mjs"))],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         f"\n{result.stdout}\n{result.stderr}")
        self.assertIn("0 failed", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
