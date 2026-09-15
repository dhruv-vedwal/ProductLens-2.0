from pathlib import Path

import pytest

from productlens.evaluation.concurrency import run_claim_benchmark


@pytest.mark.parametrize("runs", [1, 10, 50, 100])
def test_durable_claims_are_unique_at_supported_concurrency_levels(tmp_path: Path, runs: int):
    report = run_claim_benchmark(tmp_path / f"claims-{runs}.sqlite3", runs=runs)
    assert report["all_claimed_once"] is True
    assert report["duplicate_claims"] == 0
