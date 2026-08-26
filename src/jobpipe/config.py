"""YAML config loading. One place that knows where files live."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROFILE_DIR = ROOT / "profile"


def _load(path: Path):
    """Load a config file, falling back to its checked-in example.

    The real profile files hold a home address and phone number, so they are
    gitignored. Falling back to `<name>.example.<ext>` means a fresh clone runs
    and its tests pass before anyone has filled anything in.
    """
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    example = path.parent / f"{path.stem}.example{path.suffix}"
    if example.exists():
        return yaml.safe_load(example.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        f"missing config: {path}\n"
        f"Copy the example to get started:  cp {example} {path}"
    )


def using_example_profile() -> bool:
    """True when facts.yaml has not been created yet."""
    return not (PROFILE_DIR / "facts.yaml").exists()


@lru_cache(maxsize=None)
def rules() -> dict:
    return _load(CONFIG_DIR / "rules.yaml")


@lru_cache(maxsize=None)
def _company_file() -> dict:
    return _load(CONFIG_DIR / "companies.yaml") or {}


def companies() -> list[dict]:
    return _company_file().get("companies", [])


def lists() -> list[str]:
    return _company_file().get("lists", [])


@lru_cache(maxsize=None)
def facts() -> dict:
    return _load(PROFILE_DIR / "facts.yaml")


@lru_cache(maxsize=None)
def evidence() -> list[dict]:
    return _load(PROFILE_DIR / "evidence.yaml")
