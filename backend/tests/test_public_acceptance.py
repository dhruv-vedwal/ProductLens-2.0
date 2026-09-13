import json
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.run_public_acceptance import _accepted_record, _refresh_record_from_artifacts, _target_sets


def test_acceptance_manifest_counts_only_deliverable_attempts():
    base = {"status": "COMPLETE", "deliverable": True, "final_video": "artifacts/runs/demo/final/demo.mp4", "hard_failures": []}
    assert _accepted_record(base)
    assert not _accepted_record({**base, "deliverable": False})
    assert not _accepted_record({**base, "final_video": None})
    assert not _accepted_record({**base, "hard_failures": ["CROP"]})


def test_acceptance_targets_are_distinct_and_benchmark_sites_are_data_only():
    """Keep the eight-site acceptance matrix generic and free of runtime recipes."""
    path = Path(__file__).parents[1] / "validation" / "public-targets.json"
    primary, fallbacks = _target_sets(path)
    urls = [str(item["url"]) for item in [*primary, *fallbacks]]
    assert len(primary) >= 8
    assert len(urls) == len(set(urls))
    assert all(url.startswith(("https://", "http://")) for url in urls)
    # Credentials are represented only as opaque references in the matrix.
    assert all("password" not in item and "email" not in item for item in [*primary, *fallbacks])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["selection_policy"]


def test_repaired_attempt_is_promoted_from_durable_delivery_report():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "final").mkdir()
        (root / "qa").mkdir()
        (root / "final" / "demo.mp4").write_bytes(b"mp4")
        (root / "qa" / "delivery-report.json").write_text(
            json.dumps({"deliverable": True, "hard_failures": []}), encoding="utf-8"
        )
        refreshed = _refresh_record_from_artifacts(
            {"status": "FAILED", "artifact_root": str(root), "final_video": None}
        )
        assert _accepted_record(refreshed)
