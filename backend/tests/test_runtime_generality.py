"""Regression guard: supplied acceptance sites must never steer runtime logic."""

from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "src" / "productlens"

# These are deliberately exact acceptance-fixture identifiers. Generic words
# such as "portfolio", "today", or "progress" are valid product concepts and
# must not be banned from a general-purpose walkthrough engine.
FORBIDDEN_RUNTIME_IDENTIFIERS = {
    "dhruv-sys.vercel.app",
    "study-plan-red.vercel.app",
    "chit-chat connect",
    "sociopedia",
    "promptrouter",
    "calmprep",
    "cookplan ai",
    "smartsevak",
    "datansh",
    "two sum",
    "group anagrams",
}


def test_runtime_contains_no_supplied_site_identifiers():
    offenders: dict[str, list[str]] = {}
    for source in RUNTIME_ROOT.rglob("*.py"):
        text = source.read_text(encoding="utf-8").lower()
        matches = sorted(
            identifier for identifier in FORBIDDEN_RUNTIME_IDENTIFIERS if identifier in text
        )
        if matches:
            offenders[str(source.relative_to(RUNTIME_ROOT))] = matches
    assert offenders == {}


def test_generic_planning_contains_no_benchmark_fixture_operation_module():
    """Lead/booking fixture choreography belongs to benchmarks, never planning."""
    planning_root = RUNTIME_ROOT / "planning"
    assert not (planning_root / "domain_operations.py").exists()
    assert not (planning_root / "service.py").exists()
    assert not (planning_root / "goal_analysis.py").exists()
    assert not (RUNTIME_ROOT / "discovery" / "targeted.py").exists()
    imports = "\n".join(path.read_text(encoding="utf-8") for path in planning_root.rglob("*.py"))
    assert "domain_fixture_operations" not in imports
    assert "fixture_planning" not in imports
