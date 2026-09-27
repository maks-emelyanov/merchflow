from __future__ import annotations

import pytest

from merch.catalog_publisher import _catalog_plan_drift
from merch.config import Settings
from merch.database import get_engine, session_scope
from merch.domain.catalog import fixture_catalog, normalize_catalog_product
from merch.models import Base, CatalogProductRecord
from merch.repository import CatalogRepository
from merch.schemas import ProductPlanV2, SurfaceArtwork
from merch.services.catalog import (
    CATALOG_PROVIDER_DISCOVERY_ATTEMPTS,
    claim_catalog_refresh,
    discover_catalog_refresh,
    fail_catalog_refresh,
    finalize_catalog_refresh,
    get_catalog_refresh,
    recover_catalog_providers,
    refresh_catalog_batch,
    refresh_catalog_candidates,
    unresolved_catalog_provider_ids,
)
from merch.services.printify import PrintifyClient, PrintifyHTTPError


def _live_settings() -> Settings:
    return Settings(
        _env_file=None,
        provider_mode="live",
        printify_api_token="token",
        printify_catalog_request_interval_seconds=0,
    )


@pytest.fixture(autouse=True)
def _rank_fixture_blueprints(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ranked_blueprints(self):  # type: ignore[no-untyped-def]
        values = [
            {"blueprintId": item.blueprint_id, "name": item.title}
            for item in fixture_catalog()
        ]
        return [*values, {"blueprintId": 1, "name": "Blueprint"}]

    monkeypatch.setattr(PrintifyClient, "ranked_blueprints", ranked_blueprints)


def _remote_product(product):  # type: ignore[no-untyped-def]
    return {
        "variants": [
            {
                "id": variant.variant_id,
                "title": variant.title,
                "options": variant.options,
                "is_available": variant.available,
                "placeholders": [
                    {
                        "position": surface.position,
                        "decoration_method": surface.decoration_method,
                        "width": surface.width,
                        "height": surface.height,
                    }
                    for surface in variant.surfaces
                ],
            }
            for variant in product.variants
        ]
    }


def _remote_shipping(product):  # type: ignore[no-untyped-def]
    profiles = []
    for variant in product.variants:
        if variant.shipping_cost_cents is not None:
            profiles.append(
                {
                    "variant_ids": [variant.variant_id],
                    "first_item": {
                        "currency": "USD",
                        "cost": variant.shipping_cost_cents,
                    },
                    "countries": ["US"],
                }
            )
    return {"profiles": profiles}


def test_catalog_fingerprint_uses_only_ordered_normalized_fields() -> None:
    source = fixture_catalog()[0]
    variants = _remote_product(source)
    shipping = _remote_shipping(source)
    first = normalize_catalog_product(
        {"id": source.blueprint_id, "title": source.title, "ignored": "summary"},
        {"id": source.print_provider_id, "title": "Provider A"},
        variants,
        shipping,
        synced_at=source.synced_at,
    )
    second = normalize_catalog_product(
        {
            "id": source.blueprint_id,
            "title": source.title,
            "images": ["detail-only-field"],
        },
        {"id": source.print_provider_id, "location": "detail-only-field"},
        {"variants": list(reversed(variants["variants"]))},
        {"profiles": list(reversed(shipping["profiles"]))},
        synced_at=source.synced_at,
    )
    assert first.source_fingerprint == second.source_fingerprint


@pytest.mark.asyncio
async def test_provider_oriented_refresh_checkpoints_and_retires_only_on_completion(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    fixtures = fixture_catalog()[:3]
    with session_scope() as session:
        repository = CatalogRepository(session)
        for product in fixtures:
            repository.upsert_product(product)

    calls: list[str] = []

    async def blueprints(self):  # type: ignore[no-untyped-def]
        calls.append("blueprints")
        return [
            {
                "id": item.blueprint_id,
                "title": item.title,
                "description": item.description,
            }
            for item in fixtures[:2]
        ]

    async def providers(self):  # type: ignore[no-untyped-def]
        calls.append("providers")
        return [{"id": 900, "title": "Provider"}]

    async def provider(self, provider_id):  # type: ignore[no-untyped-def]
        calls.append(f"provider:{provider_id}")
        return {
            "id": provider_id,
            "title": "Provider",
            "blueprints": [
                {"id": item.blueprint_id, "title": item.title} for item in fixtures[:2]
            ],
        }

    async def forbidden_blueprint(self, blueprint_id):  # type: ignore[no-untyped-def]
        raise AssertionError("full refresh must not fetch individual blueprint details")

    async def forbidden_blueprint_providers(self, blueprint_id):  # type: ignore[no-untyped-def]
        raise AssertionError("full refresh must not enumerate providers by blueprint")

    async def variants(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        calls.append(f"variants:{blueprint_id}:{provider_id}")
        source = next(item for item in fixtures if item.blueprint_id == blueprint_id)
        return _remote_product(source)

    async def shipping(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        calls.append(f"shipping:{blueprint_id}:{provider_id}")
        source = next(item for item in fixtures if item.blueprint_id == blueprint_id)
        return _remote_shipping(source)

    monkeypatch.setattr(PrintifyClient, "blueprints", blueprints)
    monkeypatch.setattr(PrintifyClient, "catalog_print_providers", providers)
    monkeypatch.setattr(PrintifyClient, "catalog_print_provider", provider)
    monkeypatch.setattr(PrintifyClient, "blueprint", forbidden_blueprint)
    monkeypatch.setattr(PrintifyClient, "print_providers", forbidden_blueprint_providers)
    monkeypatch.setattr(PrintifyClient, "variants", variants)
    monkeypatch.setattr(PrintifyClient, "shipping", shipping)

    claim = claim_catalog_refresh("refresh-attempt-1")
    sync_id = str(claim["sync_id"])
    joined = claim_catalog_refresh("concurrent-refresh-attempt")
    assert joined["sync_id"] == sync_id
    assert joined["resumed"] and not joined["owner"]
    await discover_catalog_refresh(sync_id, _live_settings())
    first = await refresh_catalog_batch(sync_id, _live_settings(), batch_size=1)
    assert first["cursor"] == 1
    fail_catalog_refresh(sync_id, "worker restarted")

    with session_scope() as session:
        old_records = [
            session.get(CatalogProductRecord, f"{item.blueprint_id}:{item.print_provider_id}")
            for item in fixtures
        ]
        assert all(item is not None and item.active for item in old_records)

    resumed = claim_catalog_refresh("refresh-attempt-2")
    assert resumed["sync_id"] == sync_id
    assert resumed["resumed"] and resumed["owner"]
    await discover_catalog_refresh(sync_id, _live_settings())
    second = await refresh_catalog_batch(sync_id, _live_settings(), batch_size=1)
    assert second["cursor"] == 2
    completed = finalize_catalog_refresh(sync_id)
    assert completed["refreshed_products"] == 2
    assert completed["retired_products"] == 3
    replayed = finalize_catalog_refresh(sync_id)
    assert replayed["status"] == completed["status"]
    assert replayed["retired_products"] == completed["retired_products"]
    assert calls.count("blueprints") == 1
    assert calls.count("providers") == 1
    assert calls.count("provider:900") == 1
    assert sum(call.startswith("variants:") for call in calls) == 2
    assert sum(call.startswith("shipping:") for call in calls) == 2
    assert not any(call.startswith("blueprint:") for call in calls)

    with session_scope() as session:
        records = list(session.query(CatalogProductRecord))
        active = {(item.blueprint_id, item.print_provider_id) for item in records if item.active}
        assert active == {(fixtures[0].blueprint_id, 900), (fixtures[1].blueprint_id, 900)}

    reactivation_id = str(claim_catalog_refresh("reactivate-fixtures")["sync_id"])
    fake_settings = Settings(_env_file=None, provider_mode="fake")
    await discover_catalog_refresh(reactivation_id, fake_settings)
    await refresh_catalog_batch(reactivation_id, fake_settings)
    finalize_catalog_refresh(reactivation_id)
    with session_scope() as session:
        assert all(
            session.get(
                CatalogProductRecord,
                f"{item.blueprint_id}:{item.print_provider_id}",
            ).active
            for item in fixtures
        )


@pytest.mark.asyncio
async def test_full_refresh_excludes_unranked_provider_offerings(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    ranked, unranked = fixture_catalog()[:2]

    async def ranked_blueprints(self):  # type: ignore[no-untyped-def]
        return [{"blueprintId": ranked.blueprint_id, "name": ranked.title}]

    async def blueprints(self):  # type: ignore[no-untyped-def]
        return [
            {"id": ranked.blueprint_id, "title": ranked.title},
            {"id": unranked.blueprint_id, "title": unranked.title},
        ]

    async def providers(self):  # type: ignore[no-untyped-def]
        return [{"id": ranked.print_provider_id, "title": "Provider"}]

    async def provider(self, provider_id):  # type: ignore[no-untyped-def]
        return {
            "id": provider_id,
            "blueprints": [
                {"id": ranked.blueprint_id, "title": ranked.title},
                {"id": unranked.blueprint_id, "title": unranked.title},
            ],
        }

    monkeypatch.setattr(PrintifyClient, "ranked_blueprints", ranked_blueprints)
    monkeypatch.setattr(PrintifyClient, "blueprints", blueprints)
    monkeypatch.setattr(PrintifyClient, "catalog_print_providers", providers)
    monkeypatch.setattr(PrintifyClient, "catalog_print_provider", provider)

    sync_id = str(claim_catalog_refresh("ranked-only-discovery")["sync_id"])
    state = await discover_catalog_refresh(sync_id, _live_settings())

    assert state["blueprint_count"] == 1
    assert state["total_pairs"] == 1


@pytest.mark.asyncio
async def test_full_refresh_skips_missing_offering_and_completes_with_warning(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    source = fixture_catalog()[0]

    async def blueprints(self):  # type: ignore[no-untyped-def]
        return [{"id": source.blueprint_id, "title": source.title}]

    async def providers(self):  # type: ignore[no-untyped-def]
        return [{"id": source.print_provider_id, "title": "Provider"}]

    async def provider(self, provider_id):  # type: ignore[no-untyped-def]
        return {
            "id": provider_id,
            "blueprints": [{"id": source.blueprint_id, "title": source.title}],
        }

    async def missing(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        raise PrintifyHTTPError("GET", f"/catalog/{blueprint_id}/{provider_id}", 404)

    monkeypatch.setattr(PrintifyClient, "blueprints", blueprints)
    monkeypatch.setattr(PrintifyClient, "catalog_print_providers", providers)
    monkeypatch.setattr(PrintifyClient, "catalog_print_provider", provider)
    monkeypatch.setattr(PrintifyClient, "variants", missing)
    monkeypatch.setattr(PrintifyClient, "shipping", missing)

    sync_id = str(claim_catalog_refresh("missing-offering")["sync_id"])
    await discover_catalog_refresh(sync_id, _live_settings())
    state = await refresh_catalog_batch(sync_id, _live_settings())
    assert state["skipped_products"] == 1
    result = finalize_catalog_refresh(sync_id)
    assert result["status"] == "completed_with_warnings"
    assert result["warning_count"] == 1


@pytest.mark.asyncio
async def test_full_refresh_skips_forbidden_provider_when_others_are_accessible(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    source = fixture_catalog()[0]
    accessible_provider_id = source.print_provider_id
    forbidden_provider_id = accessible_provider_id + 1
    cached_forbidden = source.model_copy(
        update={"print_provider_id": forbidden_provider_id}
    )
    with session_scope() as session:
        CatalogRepository(session).upsert_product(cached_forbidden)
    provider_calls: dict[int, int] = {}

    async def blueprints(self):  # type: ignore[no-untyped-def]
        return [{"id": source.blueprint_id, "title": source.title}]

    async def providers(self):  # type: ignore[no-untyped-def]
        return [
            {"id": accessible_provider_id, "title": "Accessible"},
            {"id": forbidden_provider_id, "title": "Forbidden"},
        ]

    async def provider(self, provider_id):  # type: ignore[no-untyped-def]
        provider_calls[provider_id] = provider_calls.get(provider_id, 0) + 1
        if provider_id == forbidden_provider_id:
            raise PrintifyHTTPError(
                "GET",
                f"/catalog/print_providers/{provider_id}.json",
                403,
                '{"error":"Invalid scope(s) provided."}',
            )
        return {
            "id": provider_id,
            "blueprints": [{"id": source.blueprint_id, "title": source.title}],
        }

    async def unavailable_blueprint_providers(
        self, blueprint_id
    ):  # type: ignore[no-untyped-def]
        raise PrintifyHTTPError(
            "GET",
            f"/catalog/blueprints/{blueprint_id}/print_providers.json",
            403,
            '{"error":"Invalid scope(s) provided."}',
        )

    async def variants(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        assert (blueprint_id, provider_id) == (
            source.blueprint_id,
            accessible_provider_id,
        )
        return _remote_product(source)

    async def shipping(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        return _remote_shipping(source)

    monkeypatch.setattr(PrintifyClient, "blueprints", blueprints)
    monkeypatch.setattr(PrintifyClient, "catalog_print_providers", providers)
    monkeypatch.setattr(PrintifyClient, "catalog_print_provider", provider)
    monkeypatch.setattr(
        PrintifyClient, "print_providers", unavailable_blueprint_providers
    )
    monkeypatch.setattr(PrintifyClient, "variants", variants)
    monkeypatch.setattr(PrintifyClient, "shipping", shipping)

    sync_id = str(claim_catalog_refresh("mixed-provider-access")["sync_id"])
    state = await discover_catalog_refresh(sync_id, _live_settings())

    assert state["total_pairs"] == 1
    assert state["warning_count"] == 2
    assert {item["stage"] for item in state["warning_samples"]} == {
        "blueprint_provider_discovery",
        "provider_discovery",
    }
    provider_warning = next(
        item
        for item in state["warning_samples"]
        if item["stage"] == "provider_discovery"
    )
    assert provider_warning["print_provider_id"] == forbidden_provider_id
    assert provider_calls == {
        accessible_provider_id: 1,
        forbidden_provider_id: CATALOG_PROVIDER_DISCOVERY_ATTEMPTS,
    }

    await refresh_catalog_batch(sync_id, _live_settings())
    result = finalize_catalog_refresh(sync_id)
    assert result["retired_products"] == 0
    assert unresolved_catalog_provider_ids(sync_id) == {forbidden_provider_id}
    with session_scope() as session:
        preserved = session.get(
            CatalogProductRecord,
            f"{source.blueprint_id}:{forbidden_provider_id}",
        )
        assert preserved is not None and preserved.active


@pytest.mark.asyncio
async def test_full_refresh_recovers_provider_after_transient_forbidden_responses(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    source = fixture_catalog()[0]
    attempts = 0

    async def blueprints(self):  # type: ignore[no-untyped-def]
        return [{"id": source.blueprint_id, "title": source.title}]

    async def providers(self):  # type: ignore[no-untyped-def]
        return [{"id": source.print_provider_id, "title": "Provider"}]

    async def provider(self, provider_id):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts < CATALOG_PROVIDER_DISCOVERY_ATTEMPTS:
            raise PrintifyHTTPError(
                "GET",
                f"/catalog/print_providers/{provider_id}.json",
                403,
                '{"error":"Invalid scope(s) provided."}',
            )
        return {
            "id": provider_id,
            "blueprints": [{"id": source.blueprint_id, "title": source.title}],
        }

    monkeypatch.setattr(PrintifyClient, "blueprints", blueprints)
    monkeypatch.setattr(PrintifyClient, "catalog_print_providers", providers)
    monkeypatch.setattr(PrintifyClient, "catalog_print_provider", provider)

    sync_id = str(claim_catalog_refresh("transient-provider-access")["sync_id"])
    state = await discover_catalog_refresh(sync_id, _live_settings())

    assert attempts == CATALOG_PROVIDER_DISCOVERY_ATTEMPTS
    assert state["total_pairs"] == 1
    assert state["warning_count"] == 0


@pytest.mark.asyncio
async def test_full_refresh_recovers_forbidden_provider_from_blueprint_lists(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    source = fixture_catalog()[0]
    attempts = 0

    async def blueprints(self):  # type: ignore[no-untyped-def]
        return [{"id": source.blueprint_id, "title": source.title}]

    async def providers(self):  # type: ignore[no-untyped-def]
        return [{"id": source.print_provider_id, "title": "Provider"}]

    async def forbidden_provider(self, provider_id):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        raise PrintifyHTTPError(
            "GET",
            f"/catalog/print_providers/{provider_id}.json",
            403,
            '{"error":"Invalid scope(s) provided."}',
        )

    async def providers_for_blueprint(
        self, blueprint_id
    ):  # type: ignore[no-untyped-def]
        assert blueprint_id == source.blueprint_id
        return [{"id": source.print_provider_id, "title": "Provider"}]

    monkeypatch.setattr(PrintifyClient, "blueprints", blueprints)
    monkeypatch.setattr(PrintifyClient, "catalog_print_providers", providers)
    monkeypatch.setattr(PrintifyClient, "catalog_print_provider", forbidden_provider)
    monkeypatch.setattr(PrintifyClient, "print_providers", providers_for_blueprint)

    sync_id = str(claim_catalog_refresh("blueprint-provider-recovery")["sync_id"])
    state = await discover_catalog_refresh(sync_id, _live_settings())

    assert attempts == CATALOG_PROVIDER_DISCOVERY_ATTEMPTS
    assert state["total_pairs"] == 1
    assert state["warning_count"] == 0


@pytest.mark.asyncio
async def test_targeted_provider_recovery_fetches_only_missing_pairs(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    active_source, other_source = fixture_catalog()[:2]
    provider_id = active_source.print_provider_id
    missing_source = other_source.model_copy(
        update={"print_provider_id": provider_id}
    )
    with session_scope() as session:
        repository = CatalogRepository(session)
        repository.upsert_product(active_source)
        active_record = session.get(
            CatalogProductRecord,
            f"{active_source.blueprint_id}:{provider_id}",
        )
        assert active_record is not None
        active_data = dict(active_record.data)
        active_synced_at = active_record.synced_at

    def blueprint_value(product):  # type: ignore[no-untyped-def]
        return {
            "id": product.blueprint_id,
            "title": product.title,
            "description": product.description,
        }

    async def provider(self, requested_provider_id):  # type: ignore[no-untyped-def]
        assert requested_provider_id == provider_id
        return {
            "id": provider_id,
            "title": "Recovered provider",
            "blueprints": [
                blueprint_value(active_source),
                blueprint_value(missing_source),
            ],
        }

    async def blueprints(self):  # type: ignore[no-untyped-def]
        return [blueprint_value(active_source), blueprint_value(missing_source)]

    fetched_pairs: list[tuple[str, int, int]] = []

    async def variants(self, blueprint_id, requested_provider_id):  # type: ignore[no-untyped-def]
        fetched_pairs.append(("variants", blueprint_id, requested_provider_id))
        assert (blueprint_id, requested_provider_id) == (
            missing_source.blueprint_id,
            provider_id,
        )
        return _remote_product(missing_source)

    async def shipping(self, blueprint_id, requested_provider_id):  # type: ignore[no-untyped-def]
        fetched_pairs.append(("shipping", blueprint_id, requested_provider_id))
        assert (blueprint_id, requested_provider_id) == (
            missing_source.blueprint_id,
            provider_id,
        )
        return _remote_shipping(missing_source)

    monkeypatch.setattr(PrintifyClient, "catalog_print_provider", provider)
    monkeypatch.setattr(PrintifyClient, "blueprints", blueprints)
    monkeypatch.setattr(PrintifyClient, "variants", variants)
    monkeypatch.setattr(PrintifyClient, "shipping", shipping)

    result = await recover_catalog_providers({provider_id}, _live_settings())

    assert result == {
        "requested_provider_ids": [provider_id],
        "resolved_provider_ids": [provider_id],
        "unresolved_provider_ids": [],
        "discovered_pairs": 2,
        "already_active_pairs": 1,
        "missing_pairs": 1,
        "recovered_products": 1,
        "skipped_products": 0,
        "warnings": [],
    }
    assert fetched_pairs == [
        ("variants", missing_source.blueprint_id, provider_id),
        ("shipping", missing_source.blueprint_id, provider_id),
    ]
    with session_scope() as session:
        active_record = session.get(
            CatalogProductRecord,
            f"{active_source.blueprint_id}:{provider_id}",
        )
        recovered_record = session.get(
            CatalogProductRecord,
            f"{missing_source.blueprint_id}:{provider_id}",
        )
        assert active_record is not None
        assert active_record.data == active_data
        assert active_record.synced_at == active_synced_at
        assert recovered_record is not None and recovered_record.active


@pytest.mark.asyncio
async def test_full_refresh_falls_back_to_blueprint_provider_lists_and_preserves_failures(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    accessible, forbidden = fixture_catalog()[:2]
    with session_scope() as session:
        CatalogRepository(session).upsert_product(forbidden)

    async def blueprints(self):  # type: ignore[no-untyped-def]
        return [
            {"id": accessible.blueprint_id, "title": accessible.title},
            {"id": forbidden.blueprint_id, "title": forbidden.title},
        ]

    async def unavailable_provider_index(self):  # type: ignore[no-untyped-def]
        raise PrintifyHTTPError(
            "GET",
            "/catalog/print_providers.json",
            403,
            '{"error":"Invalid scope(s) provided."}',
        )

    async def providers_for_blueprint(self, blueprint_id):  # type: ignore[no-untyped-def]
        if blueprint_id == forbidden.blueprint_id:
            raise PrintifyHTTPError(
                "GET",
                f"/catalog/blueprints/{blueprint_id}/print_providers.json",
                403,
                '{"error":"Invalid scope(s) provided."}',
            )
        return [{"id": accessible.print_provider_id, "title": "Accessible"}]

    async def forbidden_provider_detail(self, provider_id):  # type: ignore[no-untyped-def]
        raise AssertionError("fallback must not call provider-oriented discovery")

    async def variants(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        assert (blueprint_id, provider_id) == (
            accessible.blueprint_id,
            accessible.print_provider_id,
        )
        return _remote_product(accessible)

    async def shipping(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        return _remote_shipping(accessible)

    monkeypatch.setattr(PrintifyClient, "blueprints", blueprints)
    monkeypatch.setattr(
        PrintifyClient, "catalog_print_providers", unavailable_provider_index
    )
    monkeypatch.setattr(PrintifyClient, "print_providers", providers_for_blueprint)
    monkeypatch.setattr(
        PrintifyClient, "catalog_print_provider", forbidden_provider_detail
    )
    monkeypatch.setattr(PrintifyClient, "variants", variants)
    monkeypatch.setattr(PrintifyClient, "shipping", shipping)

    sync_id = str(claim_catalog_refresh("blueprint-provider-fallback")["sync_id"])
    state = await discover_catalog_refresh(sync_id, _live_settings())

    assert state["total_pairs"] == 1
    assert state["warning_count"] == 2
    assert {item["stage"] for item in state["warning_samples"]} == {
        "provider_index_fallback",
        "blueprint_provider_discovery",
    }

    await refresh_catalog_batch(sync_id, _live_settings())
    result = finalize_catalog_refresh(sync_id)
    assert result["retired_products"] == 0
    with session_scope() as session:
        preserved = session.get(
            CatalogProductRecord,
            f"{forbidden.blueprint_id}:{forbidden.print_provider_id}",
        )
        assert preserved is not None and preserved.active


@pytest.mark.asyncio
async def test_full_refresh_rejects_token_when_no_provider_details_are_accessible(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())

    async def blueprints(self):  # type: ignore[no-untyped-def]
        return [{"id": 1, "title": "Blueprint"}]

    async def providers(self):  # type: ignore[no-untyped-def]
        return [{"id": 10, "title": "Forbidden"}]

    attempts = 0

    async def provider(self, provider_id):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        raise PrintifyHTTPError(
            "GET",
            f"/catalog/print_providers/{provider_id}.json",
            403,
            '{"error":"Invalid scope(s) provided."}',
        )

    async def unavailable_blueprint_providers(
        self, blueprint_id
    ):  # type: ignore[no-untyped-def]
        raise PrintifyHTTPError(
            "GET",
            f"/catalog/blueprints/{blueprint_id}/print_providers.json",
            403,
            '{"error":"Invalid scope(s) provided."}',
        )

    monkeypatch.setattr(PrintifyClient, "blueprints", blueprints)
    monkeypatch.setattr(PrintifyClient, "catalog_print_providers", providers)
    monkeypatch.setattr(PrintifyClient, "catalog_print_provider", provider)
    monkeypatch.setattr(
        PrintifyClient, "print_providers", unavailable_blueprint_providers
    )

    sync_id = str(claim_catalog_refresh("no-provider-access")["sync_id"])
    with pytest.raises(PrintifyHTTPError, match="status 403"):
        await discover_catalog_refresh(sync_id, _live_settings())
    assert attempts == CATALOG_PROVIDER_DISCOVERY_ATTEMPTS


@pytest.mark.asyncio
async def test_targeted_refresh_replaces_a_missing_cached_candidate(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    first, second = fixture_catalog()[:2]
    with session_scope() as session:
        repository = CatalogRepository(session)
        repository.upsert_product(first, printify_rank=1)
        repository.upsert_product(second, printify_rank=2)

    async def blueprints(self):  # type: ignore[no-untyped-def]
        raise AssertionError("targeted refresh must not fetch the full blueprint list")

    async def providers(self):  # type: ignore[no-untyped-def]
        raise AssertionError("targeted refresh must not fetch the global provider list")

    async def blueprint(self, blueprint_id):  # type: ignore[no-untyped-def]
        if blueprint_id == first.blueprint_id:
            raise PrintifyHTTPError("GET", f"/catalog/blueprints/{blueprint_id}.json", 404)
        return {
            "id": second.blueprint_id,
            "title": second.title,
            "description": second.description,
            "tags": ["fresh-tag"],
        }

    async def variants(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        assert blueprint_id == second.blueprint_id
        return _remote_product(second)

    async def shipping(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        return _remote_shipping(second)

    monkeypatch.setattr(PrintifyClient, "blueprints", blueprints)
    monkeypatch.setattr(PrintifyClient, "catalog_print_providers", providers)
    monkeypatch.setattr(PrintifyClient, "blueprint", blueprint)
    monkeypatch.setattr(PrintifyClient, "variants", variants)
    monkeypatch.setattr(PrintifyClient, "shipping", shipping)

    products, stats = await refresh_catalog_candidates(1, _live_settings())
    assert [(item.blueprint_id, item.tags) for item in products] == [
        (second.blueprint_id, ["fresh-tag"])
    ]
    assert stats["skipped_products"] == 1
    with session_scope() as session:
        records = {
            item.key: item.active for item in session.query(CatalogProductRecord)
        }
    assert not records[f"{first.blueprint_id}:{first.print_provider_id}"]
    assert records[f"{second.blueprint_id}:{second.print_provider_id}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_mode", ["fake", "live"])
async def test_targeted_refresh_requires_manual_bootstrap(
    isolated_app, provider_mode: str
) -> None:
    Base.metadata.create_all(get_engine())
    with pytest.raises(RuntimeError, match="merch catalog-sync"):
        await refresh_catalog_candidates(
            25,
            Settings(
                _env_file=None,
                provider_mode=provider_mode,
                printify_api_token="token",
                printify_catalog_request_interval_seconds=0,
            ),
        )


@pytest.mark.asyncio
async def test_targeted_refresh_replaces_unsupported_product(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    first, second = fixture_catalog()[:2]
    with session_scope() as session:
        repository = CatalogRepository(session)
        repository.upsert_product(first, printify_rank=1)
        repository.upsert_product(second, printify_rank=2)

    async def blueprint(self, blueprint_id):  # type: ignore[no-untyped-def]
        source = first if blueprint_id == first.blueprint_id else second
        return {
            "id": source.blueprint_id,
            "title": source.title,
            "description": source.description,
        }

    async def variants(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        source = first if blueprint_id == first.blueprint_id else second
        payload = _remote_product(source)
        if source is first:
            for variant in payload["variants"]:
                for placeholder in variant["placeholders"]:
                    placeholder["decoration_method"] = "unsupported-future-method"
        return payload

    async def shipping(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        source = first if blueprint_id == first.blueprint_id else second
        return _remote_shipping(source)

    monkeypatch.setattr(PrintifyClient, "blueprint", blueprint)
    monkeypatch.setattr(PrintifyClient, "variants", variants)
    monkeypatch.setattr(PrintifyClient, "shipping", shipping)

    products, stats = await refresh_catalog_candidates(1, _live_settings())
    assert [item.blueprint_id for item in products] == [second.blueprint_id]
    assert stats["skipped_products"] == 1
    with session_scope() as session:
        assert not session.get(
            CatalogProductRecord,
            f"{first.blueprint_id}:{first.print_provider_id}",
        ).active


@pytest.mark.parametrize("change", ["missing", "options", "surface", "shipping"])
def test_publish_catalog_plan_drift_detects_every_safety_boundary(change: str) -> None:
    product = fixture_catalog()[0]
    planned = product.variants[0]
    plan = ProductPlanV2(
        blueprint_id=product.blueprint_id,
        print_provider_id=product.print_provider_id,
        product_title=product.title,
        variants=[planned],
        surface_artworks=[
            SurfaceArtwork(
                surface_signature=planned.surfaces[0].signature,
                artifact_id="artifact-1",
                placement=planned.surfaces[0].placement,
            )
        ],
        featured_variant_id=planned.variant_id,
        gallery_variant_ids=[planned.variant_id],
        etsy_profile={
            "taxonomy_id": 1,
            "shipping_profile_id": 1,
            "return_policy_id": 2,
            "readiness_state_id": 3,
            "production_partner_ids": [4],
            "variation_property_ids": {
                axis: index
                for index, axis in enumerate(planned.options, start=1)
            },
            "customer_shipping_cents": 0,
        },
        generated_at=product.synced_at,
    )
    current = product
    if change == "missing":
        current = product.model_copy(update={"variants": product.variants[1:]})
    else:
        variant = planned
        if change == "options":
            variant = planned.model_copy(update={"options": {"color": "Changed"}})
        elif change == "surface":
            surface = planned.surfaces[0].model_copy(update={"width": 1})
            variant = planned.model_copy(update={"surfaces": [surface]})
        elif change == "shipping":
            variant = planned.model_copy(
                update={"shipping_cost_cents": (planned.shipping_cost_cents or 0) + 1}
            )
        current = product.model_copy(update={"variants": [variant, *product.variants[1:]]})
    assert _catalog_plan_drift(current, plan) is not None


def test_catalog_sync_cli_waits_by_default_and_supports_no_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from merch.cli import app

    waits: list[bool] = []

    async def start(settings=None, *, wait=False):  # type: ignore[no-untyped-def]
        waits.append(wait)
        return {
            "sync_id": "sync-1",
            "workflow_id": "workflow-1",
            "status": "completed" if wait else "pending",
            "resumed": False,
            "owner": True,
        }

    monkeypatch.setattr("merch.cli.start_catalog_refresh", start)
    runner = CliRunner()
    waited = runner.invoke(app, ["catalog-sync"])
    background = runner.invoke(app, ["catalog-sync", "--no-wait"])

    assert waited.exit_code == 0
    assert background.exit_code == 0
    assert waits == [True, False]
    assert '"status": "completed"' in waited.output
    assert '"status": "pending"' in background.output


def test_completed_refresh_status_never_exposes_manifest(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    sync_id = str(claim_catalog_refresh("status-shape")["sync_id"])
    payload = get_catalog_refresh(sync_id)
    assert "manifest" not in payload
