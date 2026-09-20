from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from merch.config import Settings, get_settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.models import Base
from merch.pipeline import publish_channel_run, record_publish_failure
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import Channel, MarketplaceListing, PriceQuote, PublishStatus, RunInput
from merch.services.printify import PrintifyClient, ProviderConfigurationError
from merch.services.storage import ArtifactStorage


class FakePrintify:
    product_payload = PrintifyClient.product_payload
    product_fingerprint = staticmethod(PrintifyClient.product_fingerprint)

    def __init__(self) -> None:
        self.creates = 0
        self.publishes = 0
        self.reconciles = 0
        self.matches: list[dict[str, Any]] = []
        self.create_error: Exception | None = None
        self.remote: dict[str, Any] = {
            "id": "product-1", "is_locked": False, "external": {"id": "7"},
        }

    async def validate_template(self, template):  # type: ignore[no-untyped-def]
        return template

    async def upload_image(self, filename, data):  # type: ignore[no-untyped-def]
        return {"id": "upload-1"}

    async def create_product(self, shop_id, payload):  # type: ignore[no-untyped-def]
        self.creates += 1
        if self.create_error is not None:
            error, self.create_error = self.create_error, None
            raise error
        self.remote.update(payload)
        return deepcopy(self.remote)

    async def reconcile_product(self, shop_id, upload_id, title):  # type: ignore[no-untyped-def]
        self.reconciles += 1
        return deepcopy(self.matches)

    async def publish(self, shop_id, product_id):  # type: ignore[no-untyped-def]
        self.publishes += 1
        return {"status": "accepted"}

    async def product(self, shop_id, product_id):  # type: ignore[no-untyped-def]
        return deepcopy(self.remote)

    async def close(self) -> None:
        pass


@dataclass
class PublishCase:
    run_id: str
    settings: Settings
    provider: FakePrintify


def verification(listing_id: int = 7) -> dict[str, Any]:
    return {
        "listing_id": listing_id, "featured_variant_id": 1001,
        "featured_image_id": 9, "variant_count": 2,
    }


@pytest.fixture
def publish_case(isolated_app, monkeypatch: pytest.MonkeyPatch) -> PublishCase:  # type: ignore[no-untyped-def]
    settings = get_settings().model_copy(update={
        "publish_mode": "live", "storage_backend": "local",
        "etsy_native_publish_grace_seconds": 0,
    })
    Base.metadata.create_all(get_engine())
    template = fixture_product_template().model_copy(update={"featured_variant_id": 1001})
    storage = ArtifactStorage(settings)
    object_key, digest = storage.put(b"saved-approved-artwork")
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
        repository = RunRepository(session)
        run = repository.create(value, f"test-{value.run_id}")
        run.status = "publishing"
        run.template_snapshot = template.model_dump(mode="json")
        run.listings = {"listings": [
            MarketplaceListing(
                channel=channel, title="Approved original shirt", short_description="Original shirt",
                long_description="An original illustration on a shirt.", tags=["original shirt"],
                bullet_points=[], alt_text="Original illustration", target_customer="Runners",
                gift_occasions=[], seo_meta_title="Original shirt", seo_meta_description="Original shirt",
            ).model_dump(mode="json")
            for channel in (Channel.ETSY, Channel.SHOPIFY)
        ]}
        run.price_quotes = [
            PriceQuote(
                channel=channel, variant_id=variant.variant_id,
                production_cost_cents=variant.production_cost_cents,
                retail_price_cents=2599, estimated_fee_cents=300, estimated_margin=0.4,
            ).model_dump(mode="json")
            for channel in (Channel.ETSY, Channel.SHOPIFY) for variant in template.variants
        ]
        repository.add_artifact(
            str(value.run_id), kind="production-v1", revision=1,
            object_key=object_key, sha256=digest, width=400, height=500, metadata={},
        )
    provider = FakePrintify()
    provider.remote.update({
        "variants": [
            {"id": v.variant_id, "price": 2599, "is_enabled": True,
             "is_default": v.variant_id == 1001, "sku": str(v.variant_id)}
            for v in template.variants
        ],
        "images": [
            {"mockup_id": f"product-1_{v.variant_id}_front", "position": "front",
             "src": f"https://images.printify.com/{v.variant_id}.jpg"}
            for v in template.variants
        ],
    })
    monkeypatch.setattr("merch.pipeline.PrintifyClient", lambda settings: provider)

    async def prepared(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Image validation has separate real-pixel integration coverage; isolate reservations here.
        return []

    monkeypatch.setattr("merch.pipeline.prepare_mockups", prepared)

    async def waiting(*args, **kwargs):  # type: ignore[no-untyped-def]
        return deepcopy(provider.remote)

    async def verified(*args, **kwargs):  # type: ignore[no-untyped-def]
        return deepcopy(provider.remote), verification()

    monkeypatch.setattr("merch.pipeline._wait_for_native_etsy_link", waiting)
    monkeypatch.setattr("merch.pipeline._verify_etsy_publish", verified)
    return PublishCase(str(value.run_id), settings, provider)


def seed_publish(
    case: PublishCase, status: str, progress: dict[str, Any],
    *, channel: Channel = Channel.ETSY, product_id: str | None = "product-1",
) -> None:
    with session_scope() as session:
        publish = RunRepository(session).publish_record(case.run_id, channel.value, "fixture")
        publish.status = status
        publish.artwork_upload_id = "upload-1"
        publish.printify_product_id = product_id
        publish.response_data = deepcopy(progress)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["creating", "publishing", "failed"])
async def test_saved_response_and_etsy_checkpoints_survive_interrupted_resume(
    publish_case: PublishCase, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    progress = {
        "publish_response": {"status": "accepted"}, "publish_started": True,
        "etsy_listing_id": 7, "draft_create_started": True, "etsy_listing_owned": True,
        "etsy_color_image_ids": {"Black": 91}, "image_upload_started_color": "Forest",
        "stage": "uploading_images", "native_poll_started": "2026-09-19T13:30:00+00:00",
    }
    seed_publish(publish_case, status, progress)

    async def interrupted(*args, **kwargs):  # type: ignore[no-untyped-def]
        with session_scope() as session:
            publish = RunRepository(session).get(publish_case.run_id, full=True).publishes[0]
            assert all(publish.response_data[key] == value for key, value in progress.items())
        raise RuntimeError("Worker interrupted during native readback")

    monkeypatch.setattr("merch.pipeline._wait_for_native_etsy_link", interrupted)
    with pytest.raises(RuntimeError, match="Worker interrupted"):
        await publish_channel_run(publish_case.run_id, Channel.ETSY, publish_case.settings)
    with session_scope() as session:
        publish = RunRepository(session).get(publish_case.run_id, full=True).publishes[0]
        assert all(publish.response_data[key] == value for key, value in progress.items())

    async def waiting(*args, **kwargs):  # type: ignore[no-untyped-def]
        return deepcopy(publish_case.provider.remote)

    monkeypatch.setattr("merch.pipeline._wait_for_native_etsy_link", waiting)
    assert await publish_channel_run(
        publish_case.run_id, Channel.ETSY, publish_case.settings,
    ) == PublishStatus.SUCCEEDED
    assert publish_case.provider.creates == publish_case.provider.publishes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [True, False])
async def test_product_create_intent_reconciles_without_duplicate_create(
    publish_case: PublishCase, found: bool,
) -> None:
    seed_publish(publish_case, "failed", {"product_create_started": True}, product_id=None)
    if found:
        publish_case.provider.matches = [deepcopy(publish_case.provider.remote)]
    status = await publish_channel_run(publish_case.run_id, Channel.ETSY, publish_case.settings)
    assert status == (PublishStatus.SUCCEEDED if found else PublishStatus.RECONCILIATION_REQUIRED)
    assert publish_case.provider.creates == 0
    assert publish_case.provider.reconciles == 1
    assert publish_case.provider.publishes == int(found)


@pytest.mark.asyncio
async def test_non_etsy_unconfirmed_publish_intent_never_claims_success(
    publish_case: PublishCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_publish(publish_case, "publishing", {"publish_started": True}, channel=Channel.SHOPIFY)
    publish_case.provider.remote["external"] = None
    assert await publish_channel_run(
        publish_case.run_id, Channel.SHOPIFY, publish_case.settings,
    ) == PublishStatus.RECONCILIATION_REQUIRED
    assert publish_case.provider.publishes == publish_case.provider.creates == 0
    with session_scope() as session:
        publish = RunRepository(session).get(publish_case.run_id, full=True).publishes[0]
        assert "outcome is unknown" in publish.error
        assert publish.status != PublishStatus.SUCCEEDED.value

    publish_case.provider.remote["external"] = {"id": "shopify-7"}
    verified = 0

    def verify_product(product, template, quotes):  # type: ignore[no-untyped-def]
        nonlocal verified
        verified += 1
        assert product["external"]["id"] == "shopify-7"
        return "https://images.printify.com/featured.jpg"

    monkeypatch.setattr("merch.pipeline.verify_printify_product", verify_product)
    assert await publish_channel_run(
        publish_case.run_id, Channel.SHOPIFY, publish_case.settings,
    ) == PublishStatus.SUCCEEDED
    assert verified == 1
    assert publish_case.provider.publishes == publish_case.provider.creates == 0


@pytest.mark.asyncio
async def test_etsy_unconfirmed_publish_intent_resumes_saved_draft(
    publish_case: PublishCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_publish(publish_case, "failed", {
        "publish_started": True, "native_poll_started": "2026-09-19T13:30:00+00:00",
        "etsy_listing_id": 99, "etsy_listing_owned": True, "stage": "writing_inventory",
    })
    publish_case.provider.remote["external"] = None
    fallback_calls = 0

    async def resumed(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal fallback_calls
        fallback_calls += 1
        with session_scope() as session:
            publish = RunRepository(session).get(publish_case.run_id, full=True).publishes[0]
            assert publish.response_data["etsy_listing_id"] == 99
            assert publish.response_data["stage"] == "writing_inventory"
        return {"id": "product-1", "external": {"id": "99"}}, verification(99)

    monkeypatch.setattr("merch.pipeline._direct_etsy_fallback", resumed)
    assert await publish_channel_run(
        publish_case.run_id, Channel.ETSY, publish_case.settings,
    ) == PublishStatus.SUCCEEDED
    assert fallback_calls == 1
    assert publish_case.provider.publishes == publish_case.provider.creates == 0
    with session_scope() as session:
        publish = RunRepository(session).get(publish_case.run_id, full=True).publishes[0]
        assert publish.external_product_id == "99"


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [True, False])
async def test_definitive_create_rejection_does_not_block_corrected_retry(
    publish_case: PublishCase, wrapped: bool,
) -> None:
    request = httpx.Request("POST", "https://api.printify.com/v1/shops/fixture/products.json")
    response = httpx.Response(400, request=request)
    http_error = httpx.HTTPStatusError("Invalid product", request=request, response=response)
    if wrapped:
        error: Exception = ProviderConfigurationError("Printify rejected POST products with status 400")
        error.__cause__ = http_error
    else:
        error = http_error
    publish_case.provider.create_error = error
    with pytest.raises(type(error)):
        await publish_channel_run(publish_case.run_id, Channel.ETSY, publish_case.settings)
    record_publish_failure(publish_case.run_id, Channel.ETSY, str(error))
    with session_scope() as session:
        publish = RunRepository(session).get(publish_case.run_id, full=True).publishes[0]
        assert not publish.response_data.get("product_create_started")

    assert await publish_channel_run(
        publish_case.run_id, Channel.ETSY, publish_case.settings,
    ) == PublishStatus.SUCCEEDED
    assert publish_case.provider.creates == 2
    assert publish_case.provider.reconciles == 0
    assert publish_case.provider.publishes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["create", "publish"])
async def test_concurrent_activity_snapshots_reserve_each_remote_mutation_once(
    publish_case: PublishCase, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    seed_publish(
        publish_case, "pending", {},
        product_id=None if stage == "create" else "product-1",
    )
    snapshots_loaded = asyncio.Event()
    validations = 0

    async def validate(template):  # type: ignore[no-untyped-def]
        nonlocal validations
        validations += 1
        if validations == 2:
            snapshots_loaded.set()
        await snapshots_loaded.wait()
        return template

    monkeypatch.setattr(publish_case.provider, "validate_template", validate)
    results = await asyncio.wait_for(asyncio.gather(
        publish_channel_run(publish_case.run_id, Channel.ETSY, publish_case.settings),
        publish_channel_run(publish_case.run_id, Channel.ETSY, publish_case.settings),
    ), timeout=5)

    assert results == [PublishStatus.SUCCEEDED, PublishStatus.SUCCEEDED]
    assert publish_case.provider.creates == int(stage == "create")
    assert publish_case.provider.publishes == 1


@pytest.mark.asyncio
async def test_delayed_empty_reconciliation_does_not_overwrite_completed_publication(
    publish_case: PublishCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_started = asyncio.Event()
    reconcile_started = asyncio.Event()
    allow_create = asyncio.Event()
    allow_reconcile = asyncio.Event()
    original_create = publish_case.provider.create_product

    async def delayed_create(*args):  # type: ignore[no-untyped-def]
        create_started.set()
        await allow_create.wait()
        return await original_create(*args)

    async def stale_reconcile(*args):  # type: ignore[no-untyped-def]
        publish_case.provider.reconciles += 1
        reconcile_started.set()
        await allow_reconcile.wait()
        return []

    monkeypatch.setattr(publish_case.provider, "create_product", delayed_create)
    monkeypatch.setattr(publish_case.provider, "reconcile_product", stale_reconcile)
    tasks = []
    try:
        async with asyncio.timeout(5):
            owner = asyncio.create_task(publish_channel_run(
                publish_case.run_id, Channel.ETSY, publish_case.settings,
            ))
            tasks.append(owner)
            await create_started.wait()
            retry = asyncio.create_task(publish_channel_run(
                publish_case.run_id, Channel.ETSY, publish_case.settings,
            ))
            tasks.append(retry)
            await reconcile_started.wait()
            allow_create.set()
            assert await owner == PublishStatus.SUCCEEDED
            allow_reconcile.set()
            assert await retry == PublishStatus.SUCCEEDED
    finally:
        allow_create.set()
        allow_reconcile.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert publish_case.provider.creates == publish_case.provider.publishes == 1
    assert publish_case.provider.reconciles == 1
    with session_scope() as session:
        publish = RunRepository(session).get(publish_case.run_id, full=True).publishes[0]
        assert publish.status == PublishStatus.SUCCEEDED.value
        assert publish.error is None
        assert publish.external_product_id == "7"
        assert publish.response_data["verification"]["listing_id"] == 7


@pytest.mark.asyncio
async def test_late_publish_failure_preserves_completed_result(publish_case: PublishCase) -> None:
    assert await publish_channel_run(
        publish_case.run_id, Channel.ETSY, publish_case.settings,
    ) == PublishStatus.SUCCEEDED
    with session_scope() as session:
        publish = RunRepository(session).get(publish_case.run_id, full=True).publishes[0]
        completed = deepcopy(publish.response_data)

    record_publish_failure(publish_case.run_id, Channel.ETSY, "Failure from an expired activity")

    with session_scope() as session:
        publish = RunRepository(session).get(publish_case.run_id, full=True).publishes[0]
        assert publish.status == PublishStatus.SUCCEEDED.value
        assert publish.error is None
        assert publish.response_data == completed
        assert publish.external_product_id == "7"
