from __future__ import annotations

import re
from datetime import UTC, datetime
from io import BytesIO
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.models import Base
from merch.repository import RunRepository
from merch.schemas import RunInput, RunStatus
from merch.services.storage import ArtifactStorage
from merch.web import create_app

COLOR = "#1F3A32 & Green"


def _login(client: TestClient) -> None:
    login = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text)
    assert csrf
    response = client.post(
        "/login", data={"password": "test-password", "csrf_token": csrf.group(1)},
    )
    assert response.status_code == 200


def _create_run(response: dict | None = None, channel: str = "etsy") -> str:  # type: ignore[type-arg]
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.create(value, f"mockup-evidence-{value.run_id}")
        run.status = RunStatus.VERIFICATION_REQUIRED.value
        publish = repo.publish_record(run.id, channel, "test-fingerprint")
        publish.status = "reconciliation_required"
        publish.response_data = response
    return str(value.run_id)


@pytest.fixture
def evidence(isolated_app):  # type: ignore[no-untyped-def]
    Base.metadata.create_all(get_engine())
    storage = ArtifactStorage(get_settings())
    encoded = BytesIO()
    Image.new("RGB", (64, 80), "#1F3A32").save(encoded, format="PNG")
    expected = encoded.getvalue()
    source_key, source_sha = storage.put(expected)
    encoded = BytesIO()
    Image.new("RGB", (64, 80), "#1F3A32").save(encoded, format="JPEG")
    actual = encoded.getvalue()
    actual_key, actual_sha = storage.put(actual, "jpg", "image/jpeg")
    response = {
        "mockup_manifest": [{
            "color": COLOR, "source_object_key": source_key,
            "source_sha256": source_sha, "source_content_type": "image/png",
        }],
        "mockup_verification": {
            "status": "failed", "phase": "etsy_content",
            "error": "Etsy photo differs from the approved mockup",
            "checks": [{
                "color": COLOR, "actual_object_key": actual_key,
                "actual_sha256": actual_sha, "actual_content_type": "image/jpeg",
            }],
        },
    }
    return {"run_id": _create_run(response), "response": response,
            "source": expected, "actual": actual}


def test_mockup_evidence_requires_admin_before_reading_run(evidence) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_app(get_settings())) as client:
        assert client.get(
            f"/api/runs/{evidence['run_id']}/mockup-evidence/source", params={"color": COLOR},
        ).status_code == 401
        assert client.get(
            "/api/runs/unknown/mockup-evidence/actual", params={"color": COLOR},
        ).status_code == 401


@pytest.mark.parametrize("side,mime", [("source", "image/png"), ("actual", "image/jpeg")])
def test_mockup_evidence_serves_only_saved_image_with_integrity(side, mime, evidence) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_app(get_settings())) as client:
        _login(client)
        response = client.get(
            f"/api/runs/{evidence['run_id']}/mockup-evidence/{side}", params={"color": COLOR},
        )
        assert response.status_code == 200
        assert response.content == evidence[side]
        assert response.headers["content-type"] == mime
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_evidence_lookup_does_not_accept_arbitrary_object_keys_or_other_run_evidence(evidence) -> None:  # type: ignore[no-untyped-def]
    storage = ArtifactStorage(get_settings())
    unrelated_key, _ = storage.put(b"unrelated private artifact")
    other_run = _create_run()
    shopify_run = _create_run(evidence["response"], channel="shopify")
    with TestClient(create_app(get_settings())) as client:
        _login(client)
        response = client.get(
            f"/api/runs/{evidence['run_id']}/mockup-evidence/source",
            params={"color": COLOR, "object_key": unrelated_key},
        )
        assert response.status_code == 200 and response.content == evidence["source"]
        for run_id in (other_run, shopify_run, "missing-run"):
            assert client.get(
                f"/api/runs/{run_id}/mockup-evidence/source",
                params={"color": COLOR, "object_key": unrelated_key},
            ).status_code == 404
        assert client.get(
            f"/api/runs/{evidence['run_id']}/mockup-evidence/source",
            params={"color": unrelated_key},
        ).status_code == 404


@pytest.mark.parametrize("failure", ["side", "color", "missing", "encoding", "hash"])
def test_evidence_returns_not_found_for_unavailable_or_invalid_image(failure, evidence) -> None:  # type: ignore[no-untyped-def]
    side, color = "source", COLOR
    if failure == "side":
        side = "arbitrary"
    elif failure == "color":
        color = ""
    else:
        source = evidence["response"]["mockup_manifest"][0]
        if failure == "missing":
            source["source_object_key"] = "artifacts/missing.png"
        elif failure == "encoding":
            source["source_content_type"] = "text/html"
        else:
            source["source_sha256"] = "0" * 64
        with session_scope() as session:
            publish = RunRepository(session).publish_record(evidence["run_id"], "etsy", "pending")
            publish.response_data = evidence["response"]
    with TestClient(create_app(get_settings())) as client:
        _login(client)
        assert client.get(
            f"/api/runs/{evidence['run_id']}/mockup-evidence/{side}", params={"color": color},
        ).status_code == 404


@pytest.mark.parametrize("source_failure", [True, False])
def test_run_page_shows_failed_verification_and_saved_comparison_links(source_failure, evidence) -> None:  # type: ignore[no-untyped-def]
    if source_failure:
        report = evidence["response"]["mockup_verification"]
        report.update(phase="source_content", checks=[], error="Printify supplied duplicate photos")
        with session_scope() as session:
            publish = RunRepository(session).publish_record(evidence["run_id"], "etsy", "pending")
            publish.response_data = evidence["response"]
    with TestClient(create_app(get_settings())) as client:
        _login(client)
        page = client.get(f"/runs/{evidence['run_id']}")
        assert page.status_code == 200
        assert "Mockup verification" in page.text
        assert "Failed" in page.text
        assert evidence["response"]["mockup_verification"]["error"] in page.text
        expected_link = f"/api/runs/{evidence['run_id']}/mockup-evidence/source?color={quote(COLOR)}"
        actual_link = f"/api/runs/{evidence['run_id']}/mockup-evidence/actual?color={quote(COLOR)}"
        assert expected_link in page.text
        assert (actual_link in page.text) is not source_failure
        assert client.get(expected_link).status_code == 200
