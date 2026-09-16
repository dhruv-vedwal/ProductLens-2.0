"""Helpers for tests that inspect package source that used to be a single file."""
from __future__ import annotations

from pathlib import Path


def package_source(relative_package_dir: str) -> str:
    root = Path(relative_package_dir)
    parts = sorted(root.glob("*.py"))
    if not parts:
        raise FileNotFoundError(relative_package_dir)
    return "\n".join(path.read_text(encoding="utf-8") for path in parts)
