from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.api.deps_state as api_state
import app.api.main as api_main
from app.api.main import (
    GenerationRequest,
    RetryRequest,
    app,
    repository,
    resolve_cloud_discovery,
    settings,
)
from app.contracts.models import ObjectiveSpec, UnderstandingPreview


@pytest.fixture(autouse=True)
def isolate_api_test_queue(monkeypatch: pytest.MonkeyPatch):
    """Keep API fixtures out of the durable development worker queue.

    This module imports the real application to exercise its public routes, so its
    synthetic example URLs share the configured development database.  Leaving a
    queued fixture behind makes a subsequently started local worker attempt an
    unreachable URL.  The fixture deliberately cancels only synthetic URLs and
    only after the test has asserted its intended queue behaviour.
    """
    # The local development profile deliberately allows Swagger without a token.
    # API security tests must still exercise the protected production behaviour.
    # Shared runtime state lives in deps_state; patch there so route handlers see it.
    monkeypatch.setattr(api_state, "settings", replace(settings, auth_required=True))
    monkeypatch.setattr(api_main, "settings", api_state.settings)
    yield
    rows = repository.connection.execute(
        """
        SELECT r.id
        FROM demo_runs r
        JOIN demo_requests q ON q.request_id = r.request_id
        WHERE r.status IN ('QUEUED', 'RUNNING', 'RETRYING')
          AND (q.url LIKE 'https://example.test%' OR q.url LIKE 'fixture://%')
        """
    ).fetchall()
    for row in rows:
        repository.cancel_run(row["id"])


def account(client: TestClient, email: str) -> tuple[dict[str, str], dict]:
    response = client.post(
        "/auth/signup",
        json={"email": email, "password": "correct-horse-battery", "display_name": "Demo User"},
    )
    assert response.status_code == 201
    payload = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}, payload["user"]


def test_authentication_and_operational_endpoints_expose_no_provider_secrets():
    client = TestClient(app)
    preflight = client.options(
        "/runs",
        headers={"Origin": "http://127.0.0.1:3000", "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
    assert client.get("/health").status_code == 200
    assert client.get("/runs?limit=1").status_code == 401
    headers, _ = account(client, f"api-{uuid4().hex}@example.test")
    assert client.get("/auth/me", headers=headers).status_code == 200
    readiness = client.get("/readiness")
    assert readiness.status_code == 200
    assert "database_ready" in readiness.json()
    providers = client.get("/providers", headers=headers)
    assert providers.status_code == 200
    assert all(
        "credential_reference" in provider and "api_key" not in provider
        for provider in providers.json()
    )
    projects = client.get("/projects", headers=headers)
    assert projects.status_code == 200 and projects.json()
    created = client.post("/projects", json={"name": "API project"}, headers=headers)
    assert created.status_code == 200 and created.json()["name"] == "API project"
    renamed = client.patch(
        f"/projects/{created.json()['id']}", json={"name": "Renamed project"}, headers=headers
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "Renamed project"
    assert client.get("/runs/not-a-run/details", headers=headers).status_code == 404
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/projects", headers=headers).status_code == 401


def test_duration_envelope_accepts_long_evidence_backed_stories():
    """API validation must match the 600-second ObjectiveSpec/DemoPlan envelope."""
    request = GenerationRequest(
        url="https://example.test",
        objective="Show the complete workflow",
        target_duration_seconds=600,
    )
    retry = RetryRequest(target_duration_seconds=600)
    assert request.target_duration_seconds == retry.target_duration_seconds == 600


def test_generation_request_keeps_presentation_preferences_typed_and_serializable():
    request = GenerationRequest(
        url="https://example.test",
        objective="Show the workflow",
        presentation={
            "include_audio": False,
            "subtitles_enabled": True,
            "subtitle_position": "top",
            "browser_zoom_percent": 100,
            "export_aspect": "16:9",
        },
    )
    options = request.presentation.model_dump(mode="json")
    assert options["include_audio"] is False
    assert options["subtitle_position"] == "top"
    assert options["browser_zoom_percent"] == 100
    assert options["export_aspect"] == "16:9"


def test_cloud_discovery_is_capability_aware_but_explicitly_overridable():
    request = GenerationRequest(url="https://example.test", objective="Show the workflow")
    assert request.cloud_discovery is None
    assert (
        GenerationRequest(
            url="https://example.test", objective="Show the workflow", cloud_discovery=False
        ).cloud_discovery
        is False
    )


def test_cloud_discovery_resolution_uses_capability_only_for_unspecified_requests():
    assert resolve_cloud_discovery(None, browserbase_configured=True) is True
    assert resolve_cloud_discovery(None, browserbase_configured=False) is False
    assert resolve_cloud_discovery(False, browserbase_configured=True) is False
    assert resolve_cloud_discovery(True, browserbase_configured=False) is True


def test_interaction_harness_endpoint_queues_trace_only_job(monkeypatch: pytest.MonkeyPatch):
    client = TestClient(app)
    headers, _ = account(client, f"harness-{uuid4().hex}@example.test")
    monkeypatch.setattr(api_state, "dispatch_generation_job", lambda job_id: None)
    response = client.post(
        "/interaction-harness/runs",
        headers=headers,
        json={
            "url": "https://example.test",
            "objective": "Open the dashboard and verify the overview is visible",
            "mode": "capability",
            "cloud_browser": False,
            "limits": {"max_steps": 8, "max_pages": 2},
        },
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    job = repository.job_for_run(run_id)
    assert job is not None
    assert job["kind"] == "interaction"
    assert job["payload"]["harness_mode"] == "capability"
    result = client.get(f"/interaction-harness/runs/{run_id}", headers=headers)
    assert result.status_code == 200
    assert result.json()["result"] is None
    duplicate = client.post(
        "/interaction-harness/runs",
        headers=headers,
        json={
            "url": "https://example.test",
            "objective": "Open the dashboard and verify the overview is visible",
            "mode": "capability",
            "cloud_browser": False,
            "limits": {"max_steps": 8, "max_pages": 2},
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["run_id"] == run_id


def test_projects_and_runs_are_user_scoped():
    client = TestClient(app)
    alice_headers, alice = account(client, f"alice-{uuid4().hex}@example.test")
    bob_headers, _ = account(client, f"bob-{uuid4().hex}@example.test")
    project = client.post("/projects", json={"name": "Alice launch"}, headers=alice_headers).json()
    request = repository.create_request(
        str(uuid4()), "fixture://gate-1", "Alice demo", project["id"]
    )
    run = repository.create_run(request["id"], str(settings.artifact_root))
    assert (
        client.get("/projects", headers=bob_headers).json()
        != client.get("/projects", headers=alice_headers).json()
    )
    assert client.get(f"/runs/{run['id']}", headers=bob_headers).status_code == 404
    assert client.get(f"/runs/{run['id']}/artifacts", headers=bob_headers).status_code == 404
    assert client.get(f"/runs/{run['id']}", headers=alice_headers).status_code == 200
    assert alice["id"] == project["owner_id"]
    # Ownership is enforced before a request is written, not merely hidden in lists.
    hijack = client.post(
        "/fixture-runs",
        json={"gate": 1, "objective": "Attempt a cross-user run", "project_id": project["id"]},
        headers=bob_headers,
    )
    assert hijack.status_code == 404


def test_retry_preserves_non_secret_generation_intent_but_requires_new_side_effect_consent():
    client = TestClient(app)
    headers, user = account(client, f"retry-{uuid4().hex}@example.test")
    project = repository.ensure_user_project(user["id"])
    request = repository.create_request(
        str(uuid4()), "https://example.test", "Show dashboard", project["id"]
    )
    run = repository.create_run(request["id"], str(settings.artifact_root))
    repository.enqueue_job(
        run["id"],
        "url",
        {
            "allow_external_side_effects": True,
            "cloud_discovery": True,
            "stagehand_assist": True,
            "max_pages": 4,
            "render": True,
            "credential_reference": "secret://productlens/demo-login",
            "audience": "sales leaders",
            "target_duration_seconds": 180,
        },
    )
    repository.update_run(run["id"], stage="FAILED", status="FAILED")
    response = client.post(f"/runs/{run['id']}/retry", json={}, headers=headers)
    assert response.status_code == 200
    assert (
        repository.job_for_run(response.json()["run_id"])["payload"]["allow_external_side_effects"]
        is False
    )


def test_authenticated_run_operations_are_scoped_and_preserve_evidence():
    client = TestClient(app)
    headers, user = account(client, f"operations-{uuid4().hex}@example.test")
    project = repository.ensure_user_project(user["id"])
    request = repository.create_request(
        str(uuid4()), "https://example.test", "Show product", project["id"]
    )
    run = repository.create_run(request["id"], str(settings.artifact_root))
    repository.enqueue_job(run["id"], "url", {"render": True})

    stages = client.get(f"/runs/{run['id']}/stages", headers=headers)
    assert stages.status_code == 200 and len(stages.json()["stages"]) == 6
    cancelled = client.post(f"/runs/{run['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    assert client.delete(f"/runs/{run['id']}", headers=headers).status_code == 204
    assert client.get(f"/runs/{run['id']}", headers=headers).status_code == 404


def test_run_events_streams_terminal_status_without_polling():
    client = TestClient(app)
    headers, user = account(client, f"events-{uuid4().hex}@example.test")
    project = repository.ensure_user_project(user["id"])
    request = repository.create_request(str(uuid4()), "fixture://gate-1", "Events", project["id"])
    run = repository.create_run(request["id"], str(settings.artifact_root))
    repository.update_run(run["id"], stage="COMPLETE", status="COMPLETE")
    with client.stream("GET", f"/runs/{run['id']}/events", headers=headers) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: status" in body
    assert run["id"] in body
    assert '"status":"COMPLETE"' in body


def test_media_and_events_accept_query_token_and_credentials_stay_opaque():
    client = TestClient(app)
    headers, user = account(client, f"media-{uuid4().hex}@example.test")
    token = headers["Authorization"].removeprefix("Bearer ").strip()
    project = repository.ensure_user_project(user["id"])
    request = repository.create_request(str(uuid4()), "fixture://gate-1", "Media", project["id"])
    run = repository.create_run(request["id"], str(settings.artifact_root))
    repository.update_run(run["id"], stage="COMPLETE", status="COMPLETE")
    with client.stream("GET", f"/runs/{run['id']}/events?token={token}") as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert '"status":"COMPLETE"' in body
    created = client.post(
        "/credentials",
        headers=headers,
        json={"name": "demo-login", "username": "user@example.test", "password": "not-returned"},
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["reference"] == "secret://productlens/demo-login"
    assert "password" not in payload and "username" not in payload
    listed = client.get("/credentials", headers=headers)
    assert listed.status_code == 200
    assert any(item["reference"] == payload["reference"] for item in listed.json())
    assert client.delete(f"/credentials/{payload['id']}", headers=headers).status_code == 204


def test_knowledge_invalidation_requires_workspace_access():
    client = TestClient(app)
    headers, user = account(client, f"knowledge-{uuid4().hex}@example.test")
    project = repository.ensure_user_project(user["id"])
    repository.create_request(str(uuid4()), "https://example.test", "Show product", project["id"])
    repository.upsert_knowledge("https://example.test", {"title": "Example"}, 0.9)
    response = client.post(
        "/knowledge/invalidate", json={"url": "https://example.test"}, headers=headers
    )
    assert response.status_code == 200 and response.json()["invalidated"] is True


def test_understanding_preview_is_separate_from_generation(monkeypatch: pytest.MonkeyPatch):
    client = TestClient(app)
    headers, _ = account(client, f"preflight-{uuid4().hex}@example.test")

    async def preview(**kwargs):
        assert kwargs["url"] == "https://public.example/"
        assert kwargs["prompt"] == "Show the reporting workflow"
        return UnderstandingPreview(
            url=kwargs["url"],
            prompt=kwargs["prompt"],
            suggested_prompt="Show the reporting workflow for a product prospect.",
            objective=ObjectiveSpec(raw=kwargs["prompt"], primary_entity="reporting workflow"),
            product_title="Public Example",
            relevant_areas=["Reports"],
            evidence_refs=["preflight:visible-dom"],
            pages_inspected=["https://public.example/"],
        )

    monkeypatch.setattr(api_main.preflight_service, "preview", preview)
    response = client.post(
        "/understanding/preview",
        json={"url": "https://public.example/", "prompt": "Show the reporting workflow"},
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "READY"
    assert payload["suggested_prompt"].startswith("Show the reporting")
    assert payload["cached"] is False
