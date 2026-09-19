from pathlib import Path

from app.evaluation.acceptance_ledger import (
    REQUIRED_CONSECUTIVE_RUNS,
    evaluate_run_record,
    historical_run_ids,
    mvp_scenarios,
    refresh_ledger,
    scenario_progress,
)
from app.quality.outcomes import inspect_diagram_semantics
from app.contracts.models import DiagramConnector, DiagramNode, DiagramState


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _accepted_record(run_id: str, **overrides) -> dict:
    record = {
        "run_id": run_id,
        "certified_outcome_graph": True,
        "matched_form_fields": {"Name": "Demo Contact", "Phone": "5550101234"},
        "behavioral_qa_failures": [],
        "multimodal": {"status": "complete", "hard_failures": []},
        "valid_video": True,
        "video": {"hard_failures": []},
        "manual_review_passed": True,
        "generality_audit": True,
        "repair_count": 0,
    }
    record.update(overrides)
    return record


def test_mvp_targets_lock_three_scenarios_and_ten_consecutive_runs():
    scenarios = mvp_scenarios(BACKEND_ROOT)
    assert [item["name"] for item in scenarios] == [
        "SmartSevak Lead",
        "SmartSevak Booking",
        "Excalidraw Chat Architecture",
    ]
    assert all(item.get("required_consecutive_runs") == REQUIRED_CONSECUTIVE_RUNS for item in scenarios)
    assert all("credential_reference" not in str(item.get("objective", "")).lower() for item in scenarios)


def test_historical_diagnostics_are_never_accepted():
    historical = historical_run_ids(BACKEND_ROOT)
    assert {"8c578338", "5d5cbb1d", "90837ff3"} <= historical
    verdict = evaluate_run_record(_accepted_record("8c578338"), historical_ids=historical)
    assert verdict["accepted"] is False
    assert "historical_diagnostic_not_accepted" in verdict["failed_gates"]


def test_unavailable_multimodal_review_cannot_count_toward_signoff():
    verdict = evaluate_run_record(
        _accepted_record(
            "fresh-run",
            multimodal={
                "status": "complete",
                "hard_failures": ["MULTIMODAL_REVIEW_UNAVAILABLE:JSONDecodeError"],
            },
        )
    )
    assert verdict["accepted"] is False
    assert "usable_multimodal_report" in verdict["failed_gates"]


def test_generic_placeholder_fields_are_not_entity_witnesses():
    verdict = evaluate_run_record(
        _accepted_record(
            "fresh-run",
            matched_form_fields={"Branch": "Unassigned", "Assignee": "Default"},
        )
    )
    assert verdict["accepted"] is False
    assert "entity_or_diagram_witness" in verdict["failed_gates"]


def test_ten_consecutive_accepted_runs_are_required_for_signoff():
    accepted = [_accepted_record(f"run-{index}") for index in range(REQUIRED_CONSECUTIVE_RUNS)]
    interrupted = [*accepted[:4], _accepted_record("bad", valid_video=False), *accepted[4:]]
    assert scenario_progress(interrupted)["signoff_ready"] is False
    assert scenario_progress(accepted)["signoff_ready"] is True
    ledger = refresh_ledger(
        {
            "scenarios": {
                "SmartSevak Lead": {"records": accepted},
                "SmartSevak Booking": {"records": accepted},
                "Excalidraw Chat Architecture": {"records": accepted},
            }
        }
    )
    assert ledger["mvp_signoff"] is True


def test_incomplete_diagram_fails_semantic_verification():
    incomplete = DiagramState(
        nodes=[
            DiagramNode(
                id="client",
                label="Client",
                x=0.2,
                y=0.2,
                visual_evidence_ref="e1",
                label_evidence_ref="e1",
            )
        ]
    )
    assert inspect_diagram_semantics(incomplete) == ["DIAGRAM_SEMANTIC_VERIFICATION_FAILED"]
    complete = DiagramState(
        nodes=[
            DiagramNode(
                id="client",
                label="Client",
                x=0.2,
                y=0.2,
                visual_evidence_ref="e1",
                label_evidence_ref="e1",
            ),
            DiagramNode(
                id="api",
                label="API Gateway",
                x=0.6,
                y=0.2,
                visual_evidence_ref="e2",
                label_evidence_ref="e2",
            ),
        ],
        connectors=[
            DiagramConnector(
                id="c1",
                source_node_id="client",
                target_node_id="api",
                visual_evidence_ref="e3",
            )
        ],
        labels_verified=True,
        topology_verified=True,
    )
    assert inspect_diagram_semantics(complete, requested_labels=["Client", "API Gateway"]) == []
