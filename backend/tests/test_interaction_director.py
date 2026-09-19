import pytest

from app.contracts.models import Affordance, InteractionIntent, InteractionSnapshot
from app.interaction import InteractionDirector, InteractionKernel


def _snapshot() -> InteractionSnapshot:
    return InteractionSnapshot(
        id="snapshot-1",
        url="https://example.test/app",
        title="Example",
        visible_text="A useful product area",
        visible_affordances=[
            Affordance(
                id="affordance-safe",
                label="Open details",
                role="button",
                method="click",
                geometry={"x": 20, "y": 30, "width": 160, "height": 40},
                evidence_refs=["snapshot-1"],
                confidence=0.95,
            ),
            Affordance(
                id="affordance-disabled",
                label="Disabled action",
                role="button",
                method="click",
                enabled=False,
                evidence_refs=["snapshot-1"],
                confidence=0.99,
            ),
        ],
    )


def test_director_only_proposes_visible_enabled_grounded_controls():
    director = InteractionDirector(InteractionKernel(run_id="run", objective="Inspect details"))
    intent = InteractionIntent(objective="Inspect details", audience_value="Understand the result")
    director.register_objective(intent)
    snapshot = _snapshot()
    affordances = director.observe(snapshot)

    assert [item.id for item in affordances] == ["affordance-safe"]
    candidate = director.candidate_for_affordance(
        intent_id=intent.id,
        snapshot=snapshot,
        affordance=affordances[0],
    )
    assert candidate.action.target.label == "Open details"
    assert director.select([candidate]).id == candidate.id


def test_director_rejects_unobserved_snapshot():
    director = InteractionDirector(InteractionKernel(run_id="run", objective="Inspect details"))
    intent = InteractionIntent(objective="Inspect details", audience_value="Understand the result")
    director.register_objective(intent)
    with pytest.raises(ValueError, match="must be observed"):
        director.candidate_for_affordance(
            intent_id=intent.id,
            snapshot=_snapshot(),
            affordance=_snapshot().visible_affordances[0],
        )


def test_production_engine_uses_director_selection_and_behavior_adapters():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "app"
        / "execution"
        / "engine.py"
    ).read_text(encoding="utf-8")
    assert "self.interaction_director.select(candidates)" in source
    assert "self.behavior_adapters.execute(" in source
    assert "from app.evaluation.witnesses import specific_entity_fields" in source
