from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.models import Base, CopyRefreshBatchRecord, CopyRefreshItemRecord
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import ApprovalRequest, PublishStatus, RunInput, RunStatus
from merch.web import create_app


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_dashboard_authentication_and_csrf(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    app = create_app(get_settings())
    with TestClient(app) as client:
        assert client.get("/api/runs").status_code == 401
        login = client.get("/login")
        response = client.post(
            "/login",
            data={"password": "test-password", "csrf_token": _csrf(login.text)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Production history" in dashboard.text
        assert client.post("/api/runs").status_code == 403


def test_health_and_metrics_are_available(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    with TestClient(create_app(get_settings())) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/health/ready").status_code == 200
        assert "merch_http_requests_total" in client.get("/metrics").text


def test_copy_refresh_review_is_admin_only_and_shows_exact_draft(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        run = RunRepository(session).create(
            RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True),
            "copy-review-ui",
        )
        batch = CopyRefreshBatchRecord(status="pending_review", version=1, digest="a" * 64)
        session.add(batch)
        session.flush()
        session.add(CopyRefreshItemRecord(
            batch_id=batch.id, run_id=run.id, channel="etsy",
            printify_product_id="product-1", marketplace_listing_id="123",
            printify_shop_id="shop-1", status="pending_review", stage="prepared",
            before_json={"printify": {"title": "Old trail shirt", "description": "Old copy", "tags": []},
                         "etsy": {"title": "Old trail shirt", "description": "Old copy", "tags": []}},
            after_json={"title": "New trail shirt", "long_description": "New shopper copy",
                        "tags": ["trail shirt"]},
        ))
        batch_id = batch.id
    with TestClient(create_app(get_settings())) as client:
        assert client.get("/copy-refresh", follow_redirects=False).status_code == 303
        assert client.get(f"/api/copy-refresh/{batch_id}").status_code == 401
        login = client.get("/login")
        assert client.post(
            "/login", data={"password": "test-password", "csrf_token": _csrf(login.text)}
        ).status_code == 200
        page = client.get("/copy-refresh")
        assert page.status_code == 200
        assert "Old trail shirt" in page.text and "New trail shirt" in page.text
        assert "Approve and apply reviewed revisions" in page.text
        assert client.post(f"/api/copy-refresh/{batch_id}/approve", json={
            "expected_version": 1, "digest": "a" * 64
        }).status_code == 403


def test_featured_listing_photo_is_explicit_in_catalog_ui(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(update={"featured_variant_id": 1001})
        )
    with TestClient(create_app(get_settings())) as client:
        login = client.get("/login")
        assert client.post(
            "/login", data={"password": "test-password", "csrf_token": _csrf(login.text)}
        ).status_code == 200
        page = client.get("/connectors")
        assert page.status_code == 200
        assert "Featured listing photo" in page.text
        assert '<option value="1001" selected>' in page.text


def test_run_page_and_api_show_saved_featured_color_choice(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    template = fixture_product_template().model_copy(update={"featured_variant_id": 1002})
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
        repo = RunRepository(session)
        run = repo.create(value, f"manual-{value.run_id}")
        run.status = RunStatus.AWAITING_APPROVAL.value
        run.template_snapshot = template.model_dump(mode="json")
        run.publication_template_snapshot = template.model_dump(mode="json")
        preview = repo.add_artifact(
            str(value.run_id), kind="color-preview-v1", revision=1,
            object_key="artifacts/aa/preview.png", sha256="a" * 64,
            width=660, height=390, metadata={},
        )
        repo.add_artifact(
            str(value.run_id), kind="production-v1", revision=1,
            object_key="artifacts/bb/production.png", sha256="b" * 64,
            width=400, height=500,
            metadata={"featured_color_selection": {
                "selected_color": "#1F3A32", "selected_variant_id": 1002,
                "method": "vision", "reason": "Best color harmony",
                "preview_artifact_id": preview.id,
            }},
        )
    with TestClient(create_app(get_settings())) as client:
        login = client.get("/login")
        assert client.post(
            "/login", data={"password": "test-password", "csrf_token": _csrf(login.text)}
        ).status_code == 200
        payload = client.get(f"/api/runs/{value.run_id}").json()
        assert payload["featured_color_selection"]["selected_variant_id"] == 1002
        page = client.get(f"/runs/{value.run_id}")
        assert page.status_code == 200
        assert "Best color harmony" in page.text
        assert f"/artifacts/{preview.id}" in page.text


def test_unresolved_artwork_replacement_hides_and_blocks_generic_etsy_retry(
    isolated_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(get_engine())
    template = fixture_product_template().model_copy(update={"featured_variant_id": 1001})
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.create(value, f"manual-{value.run_id}")
        run.status = RunStatus.VERIFICATION_REQUIRED.value
        run.template_snapshot = template.model_dump(mode="json")
        publish = repo.publish_record(str(value.run_id), "etsy", "old-fingerprint")
        publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
        publish.printify_product_id = "product-1"
        publish.response_data = {
            "artwork_replacement": {
                "operation_id": "replacement-operation",
                "status": "failed",
                "stage": "reconciliation_required",
            }
        }
        publish.error = "Dedicated artwork replacement reconciliation is required"

    temporal_calls: list[str] = []

    async def unexpected_temporal_client(settings):  # type: ignore[no-untyped-def]
        temporal_calls.append("connect")
        raise AssertionError("Temporal must not start for artwork reconciliation")

    monkeypatch.setattr("merch.web.temporal_client", unexpected_temporal_client)
    with TestClient(create_app(get_settings())) as client:
        login = client.get("/login")
        assert client.post(
            "/login",
            data={"password": "test-password", "csrf_token": _csrf(login.text)},
        ).status_code == 200
        page = client.get(f"/runs/{value.run_id}")
        assert page.status_code == 200
        assert "Dedicated artwork replacement reconciliation is required" in page.text
        assert "Retry verification" not in page.text

        response = client.post(
            f"/api/runs/{value.run_id}/retry/etsy",
            headers={"X-CSRF-Token": _csrf(page.text)},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Published artwork replacement requires the dedicated "
        "reconcile-published-artwork command"
    )
    assert temporal_calls == []


def test_run_effects_use_latest_saved_artifact_not_requested_settings(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    saved_typography = {"exact_text": "Trail Days", "text_arc_or_shape": "up"}
    effects = {
        "renderer_version": "effects-v1",
        "typography": {"requested_arc": "up", "applied_arc": "up"},
        "distress": {
            "scope": "design", "requested_level": 4, "applied_level": 2,
            "target_fraction": 0.08, "removed_fraction": 0.039, "seed": "saved-seed",
        },
    }
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.create(value, f"manual-{value.run_id}")
        run.typography_spec = {"text_arc_or_shape": "down", "distress_level": 5}
        for revision in (2, 1):
            repo.add_artifact(
                str(value.run_id), kind="production-v1", revision=revision,
                object_key=f"artifacts/effects-{revision}.png", sha256=str(revision) * 64,
                width=400, height=500,
                metadata={"typography_spec": saved_typography, "artwork_effects": effects}
                if revision == 2 else {},
            )
        repo.add_artifact(
            str(value.run_id), kind="production-v2", revision=3,
            object_key="artifacts/future.png", sha256="f" * 64,
            width=400, height=500, metadata={"artwork_effects": {"invalid": True}},
        )
    with TestClient(create_app(get_settings())) as client:
        login = client.get("/login")
        assert client.post(
            "/login", data={"password": "test-password", "csrf_token": _csrf(login.text)}
        ).status_code == 200
        payload = client.get(f"/api/runs/{value.run_id}").json()
        assert payload["typography_spec"] == saved_typography
        assert payload["artwork_effects"] == effects
        page = client.get(f"/runs/{value.run_id}")
        assert page.status_code == 200
        assert "Upward arch" in page.text
        assert "Whole-design distress, level 2/5" in page.text
        assert "3.9% ink removed" in page.text
        assert "Distress was reduced from level 4 to 2" in page.text


def test_legacy_artwork_does_not_claim_requested_effects_were_rendered(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.create(value, f"manual-{value.run_id}")
        run.typography_spec = {"text_arc_or_shape": "up", "distress_level": 5}
        repo.add_artifact(
            str(value.run_id), kind="production-v1", revision=1,
            object_key="artifacts/legacy.png", sha256="a" * 64,
            width=400, height=500, metadata={},
        )
    with TestClient(create_app(get_settings())) as client:
        login = client.get("/login")
        assert client.post(
            "/login", data={"password": "test-password", "csrf_token": _csrf(login.text)}
        ).status_code == 200
        payload = client.get(f"/api/runs/{value.run_id}").json()
        assert payload["typography_spec"] is None
        assert payload["artwork_effects"] is None
        page = client.get(f"/runs/{value.run_id}")
        assert page.status_code == 200
        assert "Latest artwork effects:" not in page.text


@pytest.mark.parametrize("distress,warnings,summary,warning", [
    (
        {"requested_level": 1, "applied_level": 0, "removed_fraction": 0.008,
         "reduced_for_detail": True},
        [], "Whole-design distress · 0.8% ink removed", "Distress was reduced below level 1",
    ),
    (
        {"requested_level": 5, "applied_level": 5, "removed_fraction": 0.085,
         "reduced_for_detail": True},
        [], "Whole-design distress, level 5/5 · 8.5% ink removed",
        "Distress was reduced to protect lettering and small details.",
    ),
    (
        {"requested_level": 5, "applied_level": 5, "removed_fraction": 0.085},
        ["Fine print details limited the amount of distress."],
        "Whole-design distress, level 5/5 · 8.5% ink removed",
        "Fine print details limited the amount of distress.",
    ),
])
def test_run_page_shows_fractional_distress_and_renderer_warnings(
    isolated_app, distress, warnings, summary, warning,
) -> None:
    Base.metadata.create_all(get_engine())
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        repo = RunRepository(session)
        repo.create(value, f"manual-{value.run_id}")
        repo.add_artifact(
            str(value.run_id), kind="production-v1", revision=1,
            object_key="artifacts/fractional.png", sha256="c" * 64,
            width=400, height=500,
            metadata={"artwork_effects": {
                "renderer_version": "effects-v1", "typography": {},
                "distress": {"scope": "design", **distress}, "warnings": warnings,
            }},
        )
    with TestClient(create_app(get_settings())) as client:
        login = client.get("/login")
        assert client.post(
            "/login", data={"password": "test-password", "csrf_token": _csrf(login.text)}
        ).status_code == 200
        page = client.get(f"/runs/{value.run_id}")
        assert page.status_code == 200
        assert summary in page.text
        assert "No distress" not in page.text
        assert f'<p class="warning">{warning}' in page.text


def test_ip_ui_is_hidden_until_enabled_even_for_a_saved_report(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        run = RunRepository(session).create(value, f"manual-{value.run_id}")
        run.status = RunStatus.AWAITING_APPROVAL.value
        run.ip_report = {
            "status": "pass", "risk_score": 0, "matches": [],
            "uspto_search_url": "https://tmsearch.uspto.gov/",
        }

    def run_html(ip_check_enabled: bool, manual_approval_enabled: bool = False) -> str:
        settings = get_settings().model_copy(
            update={
                "ip_check_enabled": ip_check_enabled,
                "manual_approval_enabled": manual_approval_enabled,
            }
        )
        with TestClient(create_app(settings)) as client:
            login = client.get("/login")
            assert client.post(
                "/login",
                data={"password": "test-password", "csrf_token": _csrf(login.text)},
            ).status_code == 200
            response = client.get(f"/runs/{value.run_id}")
            assert response.status_code == 200
            return response.text

    hidden = run_html(False)
    assert "QA and IP evidence" not in hidden
    assert "IP screen:" not in hidden
    assert 'id="ip-attested"' not in hidden
    assert 'id="approval-form"' not in hidden
    assert "Automatic release" in hidden
    assert '"ip_report"' not in hidden
    with session_scope() as session:
        RunRepository(session).audit(
            str(value.run_id),
            "worker",
            "artwork.typography_fallback",
            {"reason": "focused design-review fixture"},
        )
    fallback_review = run_html(False)
    assert 'id="approval-form"' in fallback_review
    assert "Design review required:" in fallback_review
    assert "Automatic release" not in fallback_review
    shown = run_html(True)
    assert "QA and IP evidence" in shown
    assert "IP screen:" in shown
    assert 'id="ip-attested"' in shown
    assert 'id="approval-form"' in shown
    manual = run_html(False, manual_approval_enabled=True)
    assert 'id="approval-form"' in manual
    assert 'id="ip-attested"' not in manual
    assert ApprovalRequest.model_validate(
        {"channels": ["shopify"], "expected_version": 1, "confirmation": "PUBLISH"}
    ).ip_attested is False
