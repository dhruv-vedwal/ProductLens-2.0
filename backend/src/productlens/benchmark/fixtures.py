from __future__ import annotations

from pathlib import Path


def fixture_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "productlens-test-htmls"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("Cannot locate mandatory productlens-test-htmls fixture suite")


def file_url(*parts: str) -> str:
    return (fixture_root().joinpath(*parts)).resolve().as_uri()
