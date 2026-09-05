import json

import pytest

from productlens.evaluation.benchmark import (
    GateMeasurement,
    aggregate_reports,
    run_and_write_suite,
    run_suite,
)


def test_measurement_is_explicit_about_rate():
    assert GateMeasurement(1, 10, 9).first_pass_rate == 0.9


def test_aggregate_reports_preserves_denominator_and_requires_scale():
    report = {
        "benchmark_id": "b",
        "attempts_per_gate": 2,
        "requested_gates": [1, 2],
        "gates": [{"successes": 2}, {"successes": 1}],
        "support_envelope": {"environment": "fixture"},
    }
    aggregated = aggregate_reports([report, report])
    assert aggregated["attempts"] == 8
    assert aggregated["successes"] == 6
    assert aggregated["first_pass_rate"] == 0.75
    assert aggregated["eligible"] is False


@pytest.mark.asyncio
async def test_small_fixture_suite_cannot_claim_the_production_reliability_target(monkeypatch, tmp_path):
    async def fake_repeat(gate, attempts, root):
        return GateMeasurement(gate, attempts, attempts)

    monkeypatch.setattr("productlens.evaluation.benchmark.repeat_gate", fake_repeat)
    report = await run_suite(2, tmp_path, (1, 2))
    assert report["overall_first_pass_rate"] == 1.0
    assert report["reliability_target_eligible"] is False
    assert report["reliability_target_met"] is False


@pytest.mark.asyncio
async def test_benchmark_report_exposes_execution_metrics(monkeypatch, tmp_path):
    async def fake_repeat(gate, attempts, root):
        return GateMeasurement(
            gate, attempts, attempts, browser_actions=10, successful_actions=9,
            verified_actions=8, grounded_actions=7, repair_count=2, execution_duration_ms=400,
        )

    monkeypatch.setattr("productlens.evaluation.benchmark.repeat_gate", fake_repeat)
    report = await run_suite(2, tmp_path, (1,))
    assert report["metrics"] == {
        "task_success_rate": 1.0,
        "critical_action_success_rate": 0.9,
        "state_verification_rate": 0.8,
        "target_grounding_accuracy": 0.7,
        "average_repair_count": 1.0,
        "average_execution_time_ms": 200.0,
        "browser_actions": 10,
        "model_calls": 0,
    }


@pytest.mark.asyncio
async def test_benchmark_report_is_retained(monkeypatch, tmp_path):
    async def fake_suite(
        attempts, root, gates=(1, 2, 3, 4, 5, 6), on_measurement=None, completed_measurements=None
    ):
        return {"overall_first_pass_rate": 1.0, "attempts_per_gate": attempts}

    monkeypatch.setattr("productlens.evaluation.benchmark.run_suite", fake_suite)
    report = await run_and_write_suite(2, tmp_path)
    assert json.loads((tmp_path / "benchmark-report-all.json").read_text()) == report


@pytest.mark.asyncio
async def test_benchmark_checkpoints_after_every_completed_gate(monkeypatch, tmp_path):
    async def fake_repeat(gate, attempts, root):
        return GateMeasurement(gate, attempts, attempts)

    monkeypatch.setattr("productlens.evaluation.benchmark.repeat_gate", fake_repeat)
    checkpoints = []
    report = await run_suite(2, tmp_path, (1, 2), on_measurement=checkpoints.append)
    assert [item["completed_gates"] for item in checkpoints] == [[1], [1, 2]]
    assert report["complete"] is True


@pytest.mark.asyncio
async def test_benchmark_resumes_a_compatible_checkpoint_without_rerunning_completed_gates(
    monkeypatch, tmp_path
):
    (tmp_path / "benchmark-report-1-2.json").write_text(
        json.dumps(
            {
                "attempts_per_gate": 2,
                "gates": [{"gate": 1, "attempts": 2, "successes": 2}],
            }
        )
    )
    attempted: list[int] = []

    async def fake_repeat(gate, attempts, root):
        attempted.append(gate)
        return GateMeasurement(gate, attempts, attempts)

    monkeypatch.setattr("productlens.evaluation.benchmark.repeat_gate", fake_repeat)
    report = await run_and_write_suite(2, tmp_path, (1, 2))
    assert attempted == [2]
    assert report["completed_gates"] == [1, 2]
