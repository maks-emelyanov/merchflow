from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from merch import artwork_replacement as replacement
from merch.config import Settings
from merch.defaults import fixture_product_template
from merch.schemas import (
    Channel,
    MarketplaceListing,
    PriceQuote,
    PublishStatus,
    RunStatus,
)
from merch.services.mockup_verification import PreparedMockup


def test_replacement_print_areas_keeps_writable_placement_and_drops_read_only_fields() -> None:
    original = [{
        "variant_ids": [1001, 1002],
        "font_color": "#000",
        "placeholders": [{
            "position": "front",
            "decoration_method": "dtg",
            "images": [{
                "id": "old", "x": 0.47, "y": 0.51, "scale": 0.93, "angle": 2,
                "src": "https://provider.example/read-only", "width": 3692,
            }],
        }],
    }]

    updated = replacement.replacement_print_areas(original, "front", "new")

    assert original[0]["placeholders"][0]["images"][0]["id"] == "old"
    assert updated == [{
        "variant_ids": [1001, 1002],
        "placeholders": [{
            "position": "front",
            "images": [{
                "id": "new", "x": 0.47, "y": 0.51, "scale": 0.93, "angle": 2,
            }],
        }],
    }]


def test_replacement_print_areas_rejects_unmanaged_artwork_layers() -> None:
    areas = [{
        "variant_ids": [1001],
        "placeholders": [
            {"position": "front", "images": [{"id": "old"}]},
            {"position": "back", "images": [{"id": "unmanaged"}]},
        ],
    }]

    with pytest.raises(replacement.ArtworkReplacementError, match="additional artwork"):
        replacement.replacement_print_areas(areas, "front", "new")


def test_replacement_print_areas_rejects_fractional_angle() -> None:
    areas = [{
        "variant_ids": [1],
        "placeholders": [{
            "position": "front",
            "images": [
                {"id": "old", "x": 0.5, "y": 0.5, "scale": 1, "angle": 0.5}
            ],
        }],
    }]

    with pytest.raises(replacement.ArtworkReplacementError, match="invalid angle"):
        replacement.replacement_print_areas(areas, "front", "new")


def test_print_area_variant_coverage_requires_an_exact_partition() -> None:
    template = fixture_product_template().model_copy(update={"featured_variant_id": 1001})
    expected = [item.variant_id for item in template.variants if item.enabled]
    midpoint = max(1, len(expected) // 2)

    replacement._verify_print_area_variant_coverage(
        [
            {"variant_ids": expected[:midpoint]},
            {"variant_ids": expected[midpoint:]},
        ],
        template,
    )

    invalid_coverage = [
        [{"variant_ids": expected[:-1]}],
        [{"variant_ids": expected}, {"variant_ids": [expected[0]]}],
        [{"variant_ids": [*expected, max(expected) + 1]}],
    ]
    for print_areas in invalid_coverage:
        with pytest.raises(
            replacement.ArtworkReplacementError,
            match="exact approved variants",
        ):
            replacement._verify_print_area_variant_coverage(print_areas, template)


def _context() -> replacement._ReplacementContext:
    template = fixture_product_template().model_copy(update={"featured_variant_id": 1001})
    listing = MarketplaceListing(
        channel=Channel.ETSY,
        title="Approved shirt",
        short_description="Approved",
        long_description="Approved existing listing",
        tags=["approved"],
        bullet_points=[],
        alt_text="Approved artwork",
        target_customer="Potters",
        gift_occasions=[],
        seo_meta_title="Approved shirt",
        seo_meta_description="Approved shirt",
    )
    quotes = [
        PriceQuote(
            channel=Channel.ETSY,
            variant_id=variant.variant_id,
            production_cost_cents=variant.production_cost_cents,
            retail_price_cents=2599,
            estimated_fee_cents=300,
            estimated_margin=0.4,
        )
        for variant in template.variants
    ]
    fingerprint = replacement.PrintifyClient.product_fingerprint(
        template, listing, quotes, "old-upload"
    )
    return replacement._ReplacementContext(
        run_id="run-1",
        run_version=1,
        template=template,
        listing=listing,
        quotes=quotes,
        shop_id="fixture-etsy",
        product_id="product-1",
        listing_id=99,
        artwork_upload_id="old-upload",
        product_fingerprint=fingerprint,
        publish_response={},
        previous_artifact_id="old-artifact",
    )


def _product(context: replacement._ReplacementContext) -> dict[str, Any]:
    return {
        "id": context.product_id,
        "title": context.listing.title,
        "description": context.listing.long_description,
        "tags": context.listing.tags,
        "blueprint_id": context.template.blueprint_id,
        "print_provider_id": context.template.print_provider_id,
        "is_locked": False,
        "external": {"id": str(context.listing_id)},
        "variants": [
            {
                "id": variant.variant_id,
                "sku": f"sku-{variant.variant_id}",
                "price": 2599,
                "is_enabled": True,
                "is_default": variant.variant_id == 1001,
            }
            for variant in context.template.variants
        ],
        "print_areas": [{
            "variant_ids": [item.variant_id for item in context.template.variants],
            "placeholders": [{
                "position": "front",
                "images": [{"id": "old-upload", "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}],
            }],
        }],
        "images": [],
    }


class _Printify:
    def __init__(self, context: replacement._ReplacementContext, events: list[str]) -> None:
        self.context = context
        self.events = events
        self.remote = _product(context)
        self.etsy: _Etsy | None = None
        self.upload_error: Exception | None = None
        self.orders_after_deactivation = False

    async def product(self, shop_id: str, product_id: str) -> dict[str, Any]:
        assert (shop_id, product_id) == (self.context.shop_id, self.context.product_id)
        return deepcopy(self.remote)

    async def orders(self, shop_id: str, page: int = 1) -> dict[str, Any]:
        assert shop_id == self.context.shop_id and page == 1
        if (
            self.orders_after_deactivation
            and self.etsy is not None
            and self.etsy.state == "inactive"
        ):
            return {
                "data": [{
                    "id": "late-order",
                    "line_items": [{"product_id": self.context.product_id}],
                }],
                "last_page": 1,
            }
        return {"data": [], "last_page": 1}

    async def upload_image(self, filename: str, data: bytes) -> dict[str, Any]:
        self.events.append("printify:upload")
        if self.upload_error:
            raise self.upload_error
        return {"id": "new-upload"}

    async def update_product_print_areas(
        self, shop_id: str, product_id: str, print_areas: list[dict[str, Any]]
    ) -> dict[str, Any]:
        assert self.etsy is not None and self.etsy.state == "inactive"
        self.events.append("printify:update-areas")
        self.remote["print_areas"] = deepcopy(print_areas)
        return deepcopy(self.remote)


class _Etsy:
    def __init__(self, context: replacement._ReplacementContext, events: list[str]) -> None:
        self.context = context
        self.events = events
        self.state = "active"
        self.gallery = [
            {"listing_image_id": 10, "rank": 1, "url_fullxfull": "https://i.etsystatic.com/old-a.png"},
            {"listing_image_id": 11, "rank": 2, "url_fullxfull": "https://i.etsystatic.com/old-b.png"},
        ]
        self.links = [
            {"property_id": 514, "value_id": 601, "image_id": 10},
            {"property_id": 514, "value_id": 602, "image_id": 11},
        ]
        self.next_image_id = 20
        self.transactions_after_deactivation = False

    async def listing(self, listing_id: int) -> dict[str, Any]:
        assert listing_id == self.context.listing_id
        return {
            "listing_id": listing_id,
            "shop_id": 42,
            "state": self.state,
            "title": self.context.listing.title,
            "description": self.context.listing.long_description,
            "tags": list(self.context.listing.tags),
        }

    async def listing_transactions(self, listing_id: int) -> list[dict[str, Any]]:
        assert listing_id == self.context.listing_id
        if self.transactions_after_deactivation and self.state == "inactive":
            return [{"transaction_id": 7001}]
        return []

    async def inventory(self, listing_id: int) -> dict[str, Any]:
        assert listing_id == self.context.listing_id
        products = []
        for index, variant in enumerate(self.context.template.variants):
            products.append({
                "sku": f"sku-{variant.variant_id}",
                "is_deleted": False,
                "offerings": [{
                    "is_enabled": True,
                    "is_deleted": False,
                    "quantity": 10,
                    "price": {"amount": 2599, "divisor": 100, "currency_code": "USD"},
                }],
                "property_values": [
                    {"property_id": 513, "property_name": "Size", "values": [variant.size], "value_ids": [501 + index]},
                    {"property_id": 514, "property_name": "Color", "values": [variant.color], "value_ids": [601 + index]},
                ],
            })
        return {"products": products}

    async def images(self, listing_id: int) -> list[dict[str, Any]]:
        assert listing_id == self.context.listing_id
        return deepcopy(self.gallery)

    async def variation_images(self, listing_id: int) -> list[dict[str, Any]]:
        assert listing_id == self.context.listing_id
        return deepcopy(self.links)

    async def update_listing(self, listing_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.state = str(payload["state"])
        self.events.append(f"etsy:{self.state}")
        return await self.listing(listing_id)

    async def update_variation_images(
        self, listing_id: int, images: list[dict[str, int]]
    ) -> None:
        assert listing_id == self.context.listing_id
        self.links = deepcopy(images)
        self.events.append(f"etsy:links:{len(images)}")

    async def delete_image(self, listing_id: int, image_id: int) -> None:
        assert not self.links
        if len(self.gallery) <= 1:
            raise RuntimeError("Listings must have at least 1 image")
        self.events.append(f"etsy:delete:{image_id}")
        self.gallery = [item for item in self.gallery if item["listing_image_id"] != image_id]

    async def upload_mockup(
        self,
        listing_id: int,
        image: bytes,
        content_type: str,
        alt_text: str,
        rank: int,
    ) -> int:
        assert self.state == "inactive"
        image_id = self.next_image_id
        self.next_image_id += 1
        self.gallery.append({
            "listing_image_id": image_id,
            "rank": rank,
            "url_fullxfull": f"https://i.etsystatic.com/new-{image_id}.png",
            "alt_text": alt_text,
        })
        self.events.append(f"etsy:upload:{rank}")
        return image_id


def _prepared(context: replacement._ReplacementContext, *, new: bool) -> list[PreparedMockup]:
    prefix = "new" if new else "old"
    return [
        PreparedMockup(
            color=variant.color,
            source=f"https://images.printify.com/{prefix}-{index}.png",
            variant_id=variant.variant_id,
            image=f"{prefix}-{index}".encode(),
            content_type="image/png",
            evidence={"source_pixel_sha256": f"{prefix}-pixels-{index}"},
        )
        for index, variant in enumerate(context.template.variants)
    ]


async def _replacement_checkpoint(
    context: replacement._ReplacementContext,
    etsy: _Etsy,
) -> dict[str, Any]:
    product = _product(context)
    inventory = await etsy.inventory(context.listing_id)
    baseline = _prepared(context, new=False)
    proposed = _prepared(context, new=True)
    return {
        "operation_id": "replacement-operation",
        "artwork_sha256": "a" * 64,
        "artifact_object_key": "artifacts/aa/replacement.png",
        "new_artwork_upload_id": "new-upload",
        "quality_attestation": {
            "passed": True,
            "artwork_sha256": "a" * 64,
            "reviewer": "visual-qa",
        },
        "replacement_mockups": [
            {"color": item.color, **deepcopy(item.evidence)} for item in proposed
        ],
        "snapshot": {
            "printify": {
                "print_areas": deepcopy(product["print_areas"]),
                "invariants_sha256": replacement._digest(
                    replacement._product_invariants(product)
                ),
            },
            "etsy": {
                "inventory_sha256": replacement._digest(inventory),
                "gallery": deepcopy(etsy.gallery),
                "variation_images": deepcopy(etsy.links),
                "baseline_verification": {
                    "checks": [
                        {"color": item.color, **deepcopy(item.evidence)}
                        for item in baseline
                    ],
                },
            },
        },
    }


def _patch_reconciliation_verifiers(
    monkeypatch: pytest.MonkeyPatch,
    context: replacement._ReplacementContext,
) -> None:
    monkeypatch.setattr(replacement, "verify_printify_product", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_inventory", lambda *args: None)
    monkeypatch.setattr(replacement, "selector_labels_are_exact", lambda inventory: True)

    async def prepare(product, template, **kwargs):  # type: ignore[no-untyped-def]
        ids = set(replacement._print_area_image_ids(product["print_areas"], "front"))
        return _prepared(context, new=ids == {"new-upload"})

    async def verify(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"status": "verified"}

    monkeypatch.setattr(replacement, "prepare_mockups", prepare)
    monkeypatch.setattr(replacement, "verify_etsy_mockups", verify)


def test_reconciliation_lease_distinguishes_fresh_and_stale_attempts() -> None:
    now = datetime(2026, 9, 21, 18, 0, tzinfo=UTC)
    fresh = {
        "status": "reconciling",
        "updated_at": (
            now
            - timedelta(
                seconds=replacement.RECONCILIATION_LEASE_TIMEOUT_SECONDS - 1
            )
        ).isoformat(),
    }
    stale = {
        "status": "reconciling",
        "updated_at": (
            now
            - timedelta(seconds=replacement.RECONCILIATION_LEASE_TIMEOUT_SECONDS)
        ).isoformat(),
    }

    assert not replacement._reconciliation_lease_is_stale(fresh, now=now)
    assert replacement._reconciliation_lease_is_stale(stale, now=now)
    assert not replacement._reconciliation_lease_is_stale(
        {**stale, "status": "failed"}, now=now
    )


def test_fresh_initial_replacement_checkpoint_is_not_reconcilable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    checkpoint = {
        "operation_id": "replacement-operation",
        "status": "in_progress",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    loaded = replace(
        context,
        publish_response={"artwork_replacement": checkpoint},
    )
    monkeypatch.setattr(
        replacement,
        "_load_context",
        lambda run_id, *, reconciliation=False: loaded,
    )

    assert not replacement._initial_replacement_is_stale(checkpoint)
    with pytest.raises(
        replacement.ArtworkReplacementError,
        match="still in progress; wait for its checkpoint lease",
    ):
        replacement._failed_replacement_context(context.run_id)


@pytest.mark.asyncio
async def test_stale_initial_replacement_requires_operation_bound_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    checkpoint = {
        "operation_id": "replacement-operation",
        "status": "in_progress",
        "stage": "preflight_complete",
        "updated_at": (
            datetime.now(UTC)
            - timedelta(seconds=replacement.RECONCILIATION_LEASE_TIMEOUT_SECONDS)
        ).isoformat(),
        "artwork_sha256": "a" * 64,
        "artifact_object_key": "artifacts/aa/replacement.png",
        "quality_attestation": {"passed": True},
        "snapshot": {},
    }
    loaded = replace(
        context,
        publish_response={"artwork_replacement": checkpoint},
    )
    monkeypatch.setattr(
        replacement,
        "_load_context",
        lambda run_id, *, reconciliation=False: loaded,
    )

    assert replacement._initial_replacement_is_stale(checkpoint)
    recovered_context, recovered_checkpoint = replacement._failed_replacement_context(
        context.run_id
    )
    assert recovered_context is loaded
    assert recovered_checkpoint == checkpoint

    with pytest.raises(
        replacement.ArtworkReplacementError,
        match=f"TAKEOVER {context.run_id} replacement-operation",
    ):
        await replacement.reconcile_published_artwork(
            context.run_id,
            apply=True,
            confirmation=f"RECONCILE {context.run_id}",
            settings=Settings(provider_mode="live", publish_mode="live"),
        )


@pytest.mark.parametrize("stored_status", ["failed", "completed"])
@pytest.mark.parametrize("incoming_attempt_id", ["stale-attempt", None])
def test_checkpoint_rejects_stale_or_missing_reconciliation_attempt_after_terminal_status(
    stored_status: str,
    incoming_attempt_id: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    original_response = {
        "artwork_replacement": {
            "operation_id": "replacement-operation",
            "status": stored_status,
            "stage": "terminal",
            "reconciliation_attempt_id": "current-attempt",
        }
    }
    publish = SimpleNamespace(response_data=deepcopy(original_response))

    class FakeSession:
        def __init__(self) -> None:
            self.scalar_calls = 0

        def scalar(self, statement: object) -> object:
            self.scalar_calls += 1
            return context.run_id if self.scalar_calls == 1 else publish

    class FakeSessionScope:
        def __init__(self) -> None:
            self.session = FakeSession()

        def __enter__(self) -> FakeSession:
            return self.session

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(replacement, "session_scope", FakeSessionScope)
    attempt_update = (
        {"reconciliation_attempt_id": incoming_attempt_id}
        if incoming_attempt_id is not None
        else {}
    )

    with pytest.raises(
        replacement.ArtworkReplacementError,
        match="reconciliation owns this checkpoint",
    ):
        replacement._checkpoint(
            context,
            "replacement-operation",
            stage="stale-writer",
            **attempt_update,
        )

    assert publish.response_data == original_response


def test_initial_and_aborted_checkpoints_transition_database_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    run = SimpleNamespace(status=RunStatus.PUBLISHED.value, error=None)
    publish = SimpleNamespace(
        response_data={},
        status=PublishStatus.SUCCEEDED.value,
        error=None,
        printify_product_id=context.product_id,
        external_product_id=str(context.listing_id),
        artwork_upload_id=context.artwork_upload_id,
    )
    audit_events: list[object] = []

    class FakeSession:
        def __init__(self) -> None:
            self.scalar_calls = 0

        def scalar(self, statement: object) -> object:
            self.scalar_calls += 1
            return context.run_id if self.scalar_calls == 1 else publish

        def get(self, model: object, record_id: str) -> object:
            assert record_id == context.run_id
            return run

        def add(self, record: object) -> None:
            audit_events.append(record)

    class FakeSessionScope:
        def __init__(self) -> None:
            self.session = FakeSession()

        def __enter__(self) -> FakeSession:
            return self.session

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(replacement, "session_scope", FakeSessionScope)

    replacement._initial_checkpoint(
        context,
        "replacement-operation",
        "a" * 64,
        {"passed": True},
        {"passed": True, "reviewer": "visual-qa"},
        {"printify": {}, "etsy": {}},
        "artifacts/aa/replacement.png",
    )

    assert publish.status == PublishStatus.RECONCILIATION_REQUIRED.value
    assert run.status == RunStatus.VERIFICATION_REQUIRED.value
    assert publish.error == run.error
    assert "interrupted" in publish.error
    assert publish.response_data["artwork_replacement"]["status"] == "in_progress"
    assert audit_events

    replacement._checkpoint(
        context,
        "replacement-operation",
        stage="aborted",
        status="aborted",
        error="replacement aborted safely",
    )

    assert publish.status == PublishStatus.SUCCEEDED.value
    assert publish.error is None
    assert run.status == RunStatus.PUBLISHED.value
    assert run.error is None
    assert publish.response_data["artwork_replacement"]["status"] == "aborted"


@pytest.mark.asyncio
async def test_stale_reconciliation_requires_exact_attempt_bound_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    etsy = _Etsy(context, [])
    checkpoint = await _replacement_checkpoint(context, etsy)
    checkpoint.update({
        "status": "reconciling",
        "stage": "reconciliation_preflight",
        "reconciliation_attempt_id": "old-attempt",
        "updated_at": (
            datetime.now(UTC)
            - timedelta(seconds=replacement.RECONCILIATION_LEASE_TIMEOUT_SECONDS)
        ).isoformat(),
    })
    monkeypatch.setattr(
        replacement,
        "_failed_replacement_context",
        lambda run_id: (context, deepcopy(checkpoint)),
    )

    with pytest.raises(
        replacement.ArtworkReplacementError,
        match=f"TAKEOVER {context.run_id} old-attempt",
    ):
        await replacement.reconcile_published_artwork(
            context.run_id,
            apply=True,
            confirmation=f"RECONCILE {context.run_id}",
            settings=Settings(provider_mode="live", publish_mode="live"),
        )


@pytest.mark.asyncio
async def test_reconciliation_classifies_exact_old_snapshot_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    events: list[str] = []
    printify = _Printify(context, events)
    etsy = _Etsy(context, events)
    printify.etsy = etsy
    etsy.state = "inactive"
    checkpoint = await _replacement_checkpoint(context, etsy)
    _patch_reconciliation_verifiers(monkeypatch, context)

    inspection = await replacement._inspect_failed_replacement(
        context,
        checkpoint,
        Settings(etsy_shop_id=42),
        printify,  # type: ignore[arg-type]
        etsy,  # type: ignore[arg-type]
    )

    assert inspection.action == "abort"
    assert inspection.summary["gallery_state"] == "unchanged_old_gallery"
    assert inspection.gallery == checkpoint["snapshot"]["etsy"]["gallery"]


@pytest.mark.asyncio
async def test_stale_initial_checkpoint_with_old_art_aborts_without_new_upload_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    events: list[str] = []
    printify = _Printify(context, events)
    etsy = _Etsy(context, events)
    printify.etsy = etsy
    checkpoint = await _replacement_checkpoint(context, etsy)
    checkpoint.update({
        "status": "in_progress",
        "stage": "preflight_complete",
        "updated_at": (
            datetime.now(UTC)
            - timedelta(seconds=replacement.RECONCILIATION_LEASE_TIMEOUT_SECONDS)
        ).isoformat(),
    })
    checkpoint.pop("new_artwork_upload_id")
    _patch_reconciliation_verifiers(monkeypatch, context)

    inspection = await replacement._inspect_failed_replacement(
        context,
        checkpoint,
        Settings(etsy_shop_id=42),
        printify,  # type: ignore[arg-type]
        etsy,  # type: ignore[arg-type]
    )

    assert replacement._initial_replacement_is_stale(checkpoint)
    assert inspection.action == "abort"
    assert inspection.summary["printify_artwork_upload_id"] == context.artwork_upload_id
    assert "new_artwork_upload_id" not in checkpoint


@pytest.mark.asyncio
async def test_reconciliation_classifies_new_upload_and_exact_old_sentinel_as_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    events: list[str] = []
    printify = _Printify(context, events)
    etsy = _Etsy(context, events)
    printify.etsy = etsy
    checkpoint = await _replacement_checkpoint(context, etsy)
    sentinel = deepcopy(etsy.gallery[-1])
    checkpoint["retained_old_etsy_image_id"] = sentinel["listing_image_id"]
    printify.remote["print_areas"] = replacement.replacement_print_areas(
        printify.remote["print_areas"], context.template.position, "new-upload"
    )
    etsy.state = "inactive"
    etsy.gallery = [sentinel]
    etsy.links = []
    _patch_reconciliation_verifiers(monkeypatch, context)

    inspection = await replacement._inspect_failed_replacement(
        context,
        checkpoint,
        Settings(etsy_shop_id=42),
        printify,  # type: ignore[arg-type]
        etsy,  # type: ignore[arg-type]
    )

    assert inspection.action == "resume"
    assert inspection.summary["gallery_state"] == "retained_old_sentinel"
    assert {item.color for item in inspection.replacement_mockups} == {
        item.color for item in context.template.variants
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_state", ["wrong_sentinel", "mixed_upload_ids"])
async def test_reconciliation_rejects_wrong_sentinel_or_mixed_upload_ids(
    invalid_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    events: list[str] = []
    printify = _Printify(context, events)
    etsy = _Etsy(context, events)
    printify.etsy = etsy
    checkpoint = await _replacement_checkpoint(context, etsy)
    etsy.state = "inactive"
    _patch_reconciliation_verifiers(monkeypatch, context)

    if invalid_state == "wrong_sentinel":
        sentinel = deepcopy(etsy.gallery[-1])
        checkpoint["retained_old_etsy_image_id"] = etsy.gallery[0]["listing_image_id"]
        printify.remote["print_areas"] = replacement.replacement_print_areas(
            printify.remote["print_areas"], context.template.position, "new-upload"
        )
        etsy.gallery = [sentinel]
        etsy.links = []
        message = "partially changed"
    else:
        variant_ids = [
            item.variant_id for item in context.template.variants if item.enabled
        ]
        assert len(variant_ids) >= 2
        printify.remote["print_areas"] = [
            {
                "variant_ids": [variant_ids[0]],
                "placeholders": [{
                    "position": context.template.position,
                    "images": [{"id": "old-upload"}],
                }],
            },
            {
                "variant_ids": variant_ids[1:],
                "placeholders": [{
                    "position": context.template.position,
                    "images": [{"id": "new-upload"}],
                }],
            },
        ]
        message = "mixed or unrecognized"

    with pytest.raises(replacement.ArtworkReplacementError, match=message):
        await replacement._inspect_failed_replacement(
            context,
            checkpoint,
            Settings(etsy_shop_id=42),
            printify,  # type: ignore[arg-type]
            etsy,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_reconcile_apply_acquires_lease_before_failed_first_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    events: list[str] = []
    printify = _Printify(context, events)
    etsy = _Etsy(context, events)
    printify.etsy = etsy
    checkpoint = {
        "operation_id": "replacement-operation",
        "status": "failed",
    }
    ordering: list[str] = []
    saved_checkpoints: list[dict[str, Any]] = []

    monkeypatch.setattr(
        replacement,
        "_failed_replacement_context",
        lambda run_id: (context, deepcopy(checkpoint)),
    )

    def acquire_lease(
        loaded: replacement._ReplacementContext,
        operation_id: str,
        *,
        stale_attempt_id: str | None = None,
    ) -> str:
        assert loaded is context
        assert operation_id == "replacement-operation"
        assert stale_attempt_id is None
        ordering.append("lease")
        return "attempt-1"

    async def inspect(*args, **kwargs):  # type: ignore[no-untyped-def]
        assert etsy.state == "active"
        ordering.append("inspect")
        raise replacement.ArtworkReplacementError(
            "Mixed Printify/Etsy state is active and requires reconciliation"
        )

    def save_checkpoint(*args, **kwargs):  # type: ignore[no-untyped-def]
        saved_checkpoints.append(dict(kwargs))

    monkeypatch.setattr(replacement, "_acquire_reconciliation_lease", acquire_lease)
    monkeypatch.setattr(replacement, "_inspect_failed_replacement", inspect)
    monkeypatch.setattr(replacement, "_checkpoint", save_checkpoint)

    with pytest.raises(
        replacement.ArtworkReplacementError,
        match="Mixed Printify/Etsy state is active",
    ):
        await replacement.reconcile_published_artwork(
            context.run_id,
            apply=True,
            confirmation=f"RECONCILE {context.run_id}",
            settings=Settings(provider_mode="live", publish_mode="live"),
            printify_client=printify,  # type: ignore[arg-type]
            etsy_client=etsy,  # type: ignore[arg-type]
        )

    assert ordering == ["lease", "inspect"]
    assert etsy.state == "inactive"
    assert events == ["etsy:inactive"]
    assert saved_checkpoints == [{
        "stage": "reconciliation_required",
        "status": "failed",
        "error": "Mixed Printify/Etsy state is active and requires reconciliation",
        "reconciliation_attempt_id": "attempt-1",
        "etsy_inactive_confirmed": True,
    }]


@pytest.mark.asyncio
async def test_replace_gallery_rejects_active_listing_before_link_writes() -> None:
    context = _context()
    events: list[str] = []
    etsy = _Etsy(context, events)
    inventory = await etsy.inventory(context.listing_id)
    expected_gallery = deepcopy(etsy.gallery)
    expected_links = deepcopy(etsy.links)
    checkpoints: list[dict[str, Any]] = []

    with pytest.raises(
        replacement.ArtworkReplacementError,
        match="remain inactive immediately before gallery replacement",
    ):
        await replacement._replace_etsy_gallery(
            etsy,  # type: ignore[arg-type]
            context,
            inventory,
            _prepared(context, new=True),
            lambda **updates: checkpoints.append(dict(updates)),
            settings=Settings(etsy_shop_id=42),
            expected_gallery=expected_gallery,
            expected_variation_images=expected_links,
        )

    assert "etsy:links:0" not in events
    assert etsy.gallery == expected_gallery
    assert etsy.links == expected_links
    assert checkpoints == []


@pytest.mark.asyncio
async def test_apply_deactivates_before_print_area_change_and_replaces_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context()
    events: list[str] = []
    printify = _Printify(context, events)
    etsy = _Etsy(context, events)
    printify.etsy = etsy
    checkpoints: list[dict[str, Any]] = []
    persisted: list[dict[str, Any]] = []
    artwork = b"replacement-artwork"
    digest = hashlib.sha256(artwork).hexdigest()

    monkeypatch.setattr(replacement, "_load_context", lambda run_id: context)
    monkeypatch.setattr(
        replacement,
        "_validate_artwork",
        lambda data, loaded, settings: (digest, {"passed": True, "has_alpha": True}),
    )
    monkeypatch.setattr(replacement, "verify_printify_product", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_listing", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_inventory", lambda *args: None)
    monkeypatch.setattr(replacement, "selector_labels_are_exact", lambda inventory: True)
    monkeypatch.setattr(replacement, "_initial_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        replacement,
        "_checkpoint",
        lambda *args, **kwargs: checkpoints.append(dict(kwargs)),
    )

    async def prepare(product, template, **kwargs):  # type: ignore[no-untyped-def]
        ids = replacement._print_area_image_ids(product["print_areas"], "front")
        return _prepared(context, new=ids == ["new-upload"])

    async def verify(
        client, listing_id, template, expected, inventory, *, image_ids=None, **kwargs
    ):  # type: ignore[no-untyped-def]
        resolved = image_ids or {
            item.color: image_id
            for item, image_id in zip(expected, [10, 11], strict=True)
        }
        return {
            "status": "verified",
            "image_ids": resolved,
            "featured_image_id": resolved[template.featured_variant().color],
            "final_gallery": await client.images(listing_id),
            "final_variation_images": await client.variation_images(listing_id),
            "final_inventory": deepcopy(inventory),
        }

    monkeypatch.setattr(replacement, "prepare_mockups", prepare)
    monkeypatch.setattr(replacement, "verify_etsy_mockups", verify)
    monkeypatch.setattr(
        replacement,
        "_persist_success",
        lambda *args, **kwargs: persisted.append({"args": args, "kwargs": kwargs}),
    )
    settings = Settings(
        provider_mode="live",
        publish_mode="live",
        etsy_shop_id=42,
        local_storage_path=tmp_path / "artifacts",
    )

    result = await replacement.replace_published_artwork(
        context.run_id,
        artwork,
        apply=True,
        confirmation=f"REPLACE {context.run_id}",
        quality_attestation={
            "passed": True, "artwork_sha256": digest, "reviewer": "visual-qa",
        },
        settings=settings,
        printify_client=printify,  # type: ignore[arg-type]
        etsy_client=etsy,  # type: ignore[arg-type]
        mockup_attempts=1,
        mockup_interval_seconds=0,
    )

    assert result["status"] == "completed"
    assert result["printify_product_id"] == "product-1"
    assert result["etsy_listing_id"] == 99
    assert events.index("etsy:inactive") < events.index("printify:update-areas")
    assert events.index("etsy:links:0") < events.index("etsy:delete:10")
    assert events.index("etsy:upload:1") < events.index("etsy:delete:11")
    assert events[-1] == "etsy:active"
    assert etsy.state == "active"
    assert {item["listing_image_id"] for item in etsy.gallery} == {20, 21}
    assert {item["image_id"] for item in etsy.links} == {20, 21}
    assert replacement._print_area_image_ids(printify.remote["print_areas"], "front") == [
        "new-upload"
    ]
    assert persisted
    assert any(item.get("stage") == "storefront_verified" for item in checkpoints)


@pytest.mark.asyncio
async def test_gallery_drift_during_mockup_wait_stops_before_gallery_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    events: list[str] = []
    printify = _Printify(context, events)
    etsy = _Etsy(context, events)
    printify.etsy = etsy
    checkpoints: list[dict[str, Any]] = []
    artwork = b"replacement-artwork"
    digest = hashlib.sha256(artwork).hexdigest()

    monkeypatch.setattr(replacement, "_load_context", lambda run_id: context)
    monkeypatch.setattr(
        replacement,
        "_validate_artwork",
        lambda data, loaded, settings: (digest, {"passed": True, "has_alpha": True}),
    )
    monkeypatch.setattr(replacement, "verify_printify_product", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_listing", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_inventory", lambda *args: None)
    monkeypatch.setattr(replacement, "selector_labels_are_exact", lambda inventory: True)
    monkeypatch.setattr(replacement, "_initial_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        replacement,
        "_checkpoint",
        lambda *args, **kwargs: checkpoints.append(dict(kwargs)),
    )

    async def prepare(product, template, **kwargs):  # type: ignore[no-untyped-def]
        ids = replacement._print_area_image_ids(product["print_areas"], "front")
        new = ids == ["new-upload"]
        if new:
            etsy.gallery.append({
                "listing_image_id": 91,
                "rank": 3,
                "url_fullxfull": "https://i.etsystatic.com/manual-edit.png",
            })
            etsy.links.append({
                "property_id": 514,
                "value_id": 699,
                "image_id": 91,
            })
        return _prepared(context, new=new)

    async def verify(
        client, listing_id, template, expected, inventory, *, image_ids=None, **kwargs
    ):  # type: ignore[no-untyped-def]
        resolved = image_ids or {
            item.color: image_id
            for item, image_id in zip(expected, [10, 11], strict=True)
        }
        return {
            "status": "verified",
            "image_ids": resolved,
            "featured_image_id": resolved[template.featured_variant().color],
            "final_gallery": await client.images(listing_id),
            "final_variation_images": await client.variation_images(listing_id),
            "final_inventory": deepcopy(inventory),
        }

    monkeypatch.setattr(replacement, "prepare_mockups", prepare)
    monkeypatch.setattr(replacement, "verify_etsy_mockups", verify)
    monkeypatch.setattr(replacement, "_persist_success", lambda *args, **kwargs: None)
    settings = Settings(
        provider_mode="live",
        publish_mode="live",
        etsy_shop_id=42,
        local_storage_path=tmp_path / "artifacts",
    )

    with pytest.raises(
        replacement.ArtworkReplacementError,
        match="changed while mockups were rendered",
    ):
        await replacement.replace_published_artwork(
            context.run_id,
            artwork,
            apply=True,
            confirmation=f"REPLACE {context.run_id}",
            quality_attestation={
                "passed": True,
                "artwork_sha256": digest,
                "reviewer": "visual-qa",
            },
            settings=settings,
            printify_client=printify,  # type: ignore[arg-type]
            etsy_client=etsy,  # type: ignore[arg-type]
            mockup_attempts=1,
            mockup_interval_seconds=0,
        )

    assert "printify:update-areas" in events
    assert "etsy:links:0" not in events
    assert etsy.state == "inactive"
    assert checkpoints[-1]["stage"] == "reconciliation_required"
    assert checkpoints[-1]["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("drift_kind", ["printify", "gallery"])
async def test_remote_drift_after_deactivation_fails_closed_before_printify_update(
    drift_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    events: list[str] = []
    printify = _Printify(context, events)
    etsy = _Etsy(context, events)
    printify.etsy = etsy
    checkpoints: list[dict[str, Any]] = []
    artwork = b"replacement-artwork"
    digest = hashlib.sha256(artwork).hexdigest()

    monkeypatch.setattr(replacement, "_load_context", lambda run_id: context)
    monkeypatch.setattr(
        replacement,
        "_validate_artwork",
        lambda data, loaded, settings: (digest, {"passed": True, "has_alpha": True}),
    )
    monkeypatch.setattr(replacement, "verify_printify_product", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_listing", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_inventory", lambda *args: None)
    monkeypatch.setattr(replacement, "selector_labels_are_exact", lambda inventory: True)
    monkeypatch.setattr(replacement, "_initial_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        replacement,
        "_checkpoint",
        lambda *args, **kwargs: checkpoints.append(dict(kwargs)),
    )

    async def prepare(product, template, **kwargs):  # type: ignore[no-untyped-def]
        return _prepared(context, new=False)

    async def verify(
        client, listing_id, template, expected, inventory, **kwargs
    ):  # type: ignore[no-untyped-def]
        resolved = {
            item.color: image_id
            for item, image_id in zip(expected, [10, 11], strict=True)
        }
        return {
            "status": "verified",
            "image_ids": resolved,
            "featured_image_id": resolved[template.featured_variant().color],
            "final_gallery": await client.images(listing_id),
            "final_variation_images": await client.variation_images(listing_id),
            "final_inventory": deepcopy(inventory),
        }

    monkeypatch.setattr(replacement, "prepare_mockups", prepare)
    monkeypatch.setattr(replacement, "verify_etsy_mockups", verify)
    original_update_listing = etsy.update_listing
    drifted = False

    async def update_listing_with_drift(
        listing_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal drifted
        result = await original_update_listing(listing_id, payload)
        if payload.get("state") == "inactive" and not drifted:
            drifted = True
            if drift_kind == "printify":
                printify.remote["title"] = "Externally edited title"
            else:
                etsy.gallery.append({
                    "listing_image_id": 91,
                    "rank": 3,
                    "url_fullxfull": "https://i.etsystatic.com/manual-edit.png",
                })
        return result

    monkeypatch.setattr(etsy, "update_listing", update_listing_with_drift)
    settings = Settings(
        provider_mode="live",
        publish_mode="live",
        etsy_shop_id=42,
        local_storage_path=tmp_path / "artifacts",
    )

    with pytest.raises(replacement.ArtworkReplacementError):
        await replacement.replace_published_artwork(
            context.run_id,
            artwork,
            apply=True,
            confirmation=f"REPLACE {context.run_id}",
            quality_attestation={
                "passed": True,
                "artwork_sha256": digest,
                "reviewer": "visual-qa",
            },
            settings=settings,
            printify_client=printify,  # type: ignore[arg-type]
            etsy_client=etsy,  # type: ignore[arg-type]
            mockup_attempts=1,
            mockup_interval_seconds=0,
        )

    assert drifted
    assert "etsy:inactive" in events
    assert "printify:update-areas" not in events
    assert "etsy:active" not in events
    assert etsy.state == "inactive"
    assert checkpoints[-1]["stage"] == "reconciliation_required"
    assert checkpoints[-1]["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_sale", ["printify_order", "etsy_transaction"])
async def test_late_sale_after_deactivation_aborts_before_printify_update(
    late_sale: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    events: list[str] = []
    printify = _Printify(context, events)
    etsy = _Etsy(context, events)
    printify.etsy = etsy
    printify.orders_after_deactivation = late_sale == "printify_order"
    etsy.transactions_after_deactivation = late_sale == "etsy_transaction"
    checkpoints: list[dict[str, Any]] = []
    artwork = b"replacement-artwork"
    digest = hashlib.sha256(artwork).hexdigest()

    monkeypatch.setattr(replacement, "_load_context", lambda run_id: context)
    monkeypatch.setattr(
        replacement,
        "_validate_artwork",
        lambda data, loaded, settings: (digest, {"passed": True, "has_alpha": True}),
    )
    monkeypatch.setattr(replacement, "verify_printify_product", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_listing", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_inventory", lambda *args: None)
    monkeypatch.setattr(replacement, "selector_labels_are_exact", lambda inventory: True)
    monkeypatch.setattr(replacement, "_initial_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        replacement,
        "_checkpoint",
        lambda *args, **kwargs: checkpoints.append(dict(kwargs)),
    )

    async def prepare(product, template, **kwargs):  # type: ignore[no-untyped-def]
        return _prepared(context, new=False)

    async def verify(
        client, listing_id, template, expected, inventory, **kwargs
    ):  # type: ignore[no-untyped-def]
        resolved = {
            item.color: image_id
            for item, image_id in zip(expected, [10, 11], strict=True)
        }
        return {
            "status": "verified",
            "image_ids": resolved,
            "featured_image_id": resolved[template.featured_variant().color],
            "final_gallery": await client.images(listing_id),
            "final_variation_images": await client.variation_images(listing_id),
            "final_inventory": deepcopy(inventory),
        }

    monkeypatch.setattr(replacement, "prepare_mockups", prepare)
    monkeypatch.setattr(replacement, "verify_etsy_mockups", verify)
    monkeypatch.setattr(replacement, "_persist_success", lambda *args, **kwargs: None)
    settings = Settings(
        provider_mode="live",
        publish_mode="live",
        etsy_shop_id=42,
        local_storage_path=tmp_path / "artifacts",
    )

    with pytest.raises(
        replacement.ArtworkReplacementError,
        match="after deactivation",
    ):
        await replacement.replace_published_artwork(
            context.run_id,
            artwork,
            apply=True,
            confirmation=f"REPLACE {context.run_id}",
            quality_attestation={
                "passed": True,
                "artwork_sha256": digest,
                "reviewer": "visual-qa",
            },
            settings=settings,
            printify_client=printify,  # type: ignore[arg-type]
            etsy_client=etsy,  # type: ignore[arg-type]
            mockup_attempts=1,
            mockup_interval_seconds=0,
        )

    assert "etsy:inactive" in events
    assert "printify:update-areas" not in events
    assert etsy.state == "active"
    assert events[-1] == "etsy:active"
    assert checkpoints[-1]["stage"] == "aborted"
    assert checkpoints[-1]["status"] == "aborted"


@pytest.mark.asyncio
async def test_upload_failure_closes_initial_checkpoint_without_touching_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context()
    events: list[str] = []
    printify = _Printify(context, events)
    printify.upload_error = RuntimeError("upload unavailable")
    etsy = _Etsy(context, events)
    printify.etsy = etsy
    artwork = b"replacement-artwork"
    digest = hashlib.sha256(artwork).hexdigest()
    checkpoints: list[dict[str, Any]] = []

    monkeypatch.setattr(replacement, "_load_context", lambda run_id: context)
    monkeypatch.setattr(
        replacement,
        "_validate_artwork",
        lambda data, loaded, settings: (digest, {"passed": True, "has_alpha": True}),
    )
    monkeypatch.setattr(replacement, "verify_printify_product", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_listing", lambda *args: None)
    monkeypatch.setattr(replacement, "verify_etsy_inventory", lambda *args: None)
    monkeypatch.setattr(replacement, "selector_labels_are_exact", lambda inventory: True)
    monkeypatch.setattr(replacement, "_initial_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        replacement,
        "_checkpoint",
        lambda *args, **kwargs: checkpoints.append(dict(kwargs)),
    )

    async def prepare(product, template, **kwargs):  # type: ignore[no-untyped-def]
        return _prepared(context, new=False)

    async def verify(client, listing_id, template, expected, inventory, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "status": "verified",
            "image_ids": {},
            "featured_image_id": 10,
            "final_gallery": await client.images(listing_id),
            "final_variation_images": await client.variation_images(listing_id),
            "final_inventory": deepcopy(inventory),
        }

    monkeypatch.setattr(replacement, "prepare_mockups", prepare)
    monkeypatch.setattr(replacement, "verify_etsy_mockups", verify)
    settings = Settings(
        provider_mode="live",
        publish_mode="live",
        etsy_shop_id=42,
        local_storage_path=tmp_path / "artifacts",
    )

    with pytest.raises(replacement.ArtworkReplacementError, match="upload unavailable"):
        await replacement.replace_published_artwork(
            context.run_id,
            artwork,
            apply=True,
            confirmation=f"REPLACE {context.run_id}",
            quality_attestation={
                "passed": True, "artwork_sha256": digest, "reviewer": "visual-qa",
            },
            settings=settings,
            printify_client=printify,  # type: ignore[arg-type]
            etsy_client=etsy,  # type: ignore[arg-type]
            mockup_attempts=1,
            mockup_interval_seconds=0,
        )

    assert etsy.state == "active"
    assert "etsy:inactive" not in events
    assert checkpoints[-1]["stage"] == "aborted"
    assert checkpoints[-1]["status"] == "aborted"
