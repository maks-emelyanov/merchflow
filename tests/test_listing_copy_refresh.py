from __future__ import annotations

import copy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from merch.config import get_settings
from merch.copy_refresh import (
    _batch_digest,
    _invariants,
    apply_copy_refresh_batch,
    approve_copy_refresh_batch,
    edit_copy_refresh_item,
    get_copy_refresh_batch,
    retry_copy_refresh_batch,
)
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.domain.listing_copy import ETSY_DISCLOSURE, normalize_listing_copy, validate_listing_copy
from merch.models import Base, CopyRefreshBatchRecord, CopyRefreshItemRecord, PublishRecord
from merch.repository import RunRepository
from merch.schemas import (
    Channel,
    CopyRefreshEdit,
    MarketplaceListing,
    MarketplaceListingSet,
    RunInput,
)


def _listing(channel: Channel, title: str = "Trail Graphic T-Shirt") -> MarketplaceListing:
    return MarketplaceListing(
        channel=channel, title=title, short_description="Take a little trail spirit along.",
        long_description="A sunrise trail shirt for hikers and weekend wanderers.",
        tags=["trail shirt", "hiking gift"], bullet_points=["Original trail illustration"],
        alt_text="Sunrise above a winding trail", target_customer="Hikers",
        gift_occasions=["birthday"], seo_meta_title="Trail graphic tee",
        seo_meta_description="An original trail shirt for outdoor days.",
    )


def test_listing_limits_and_disclosure() -> None:
    draft = MarketplaceListingSet(listings=[_listing(channel) for channel in Channel])
    normalized = normalize_listing_copy(draft)
    assert ETSY_DISCLOSURE in next(x for x in normalized.listings if x.channel == Channel.ETSY).long_description
    validate_listing_copy(normalized)
    etsy = next(x for x in normalized.listings if x.channel == Channel.ETSY)
    with pytest.raises(ValueError, match="140"):
        validate_listing_copy(MarketplaceListingSet(listings=[
            etsy.model_copy(update={"title": "x" * 141}) if x.channel == Channel.ETSY else x
            for x in normalized.listings
        ]))
    with pytest.raises(ValueError, match="1-20"):
        validate_listing_copy(MarketplaceListingSet(listings=[
            etsy.model_copy(update={"tags": ["very long unrelated keyword"]})
            if x.channel == Channel.ETSY else x for x in normalized.listings
        ]))


def test_copy_refresh_invariants_ignore_mockup_url_rotation_but_lock_artwork() -> None:
    product = {
        "variants": [{"id": 1001, "sku": "sku-1", "price": 2599,
                      "is_enabled": True, "is_default": True}],
        "images": [{"position": "front", "mockup_id": "mockup-1",
                    "src": "https://images.example/old.png", "variant_ids": [1001]}],
        "print_areas": [{
            "variant_ids": [1001],
            "placeholders": [{"position": "front", "images": [{
                "id": "artwork-1", "x": 0.5, "y": 0.5, "scale": 1, "angle": 0,
            }]}],
        }],
    }
    inventory = {"products": []}
    images = [{"listing_image_id": 9, "rank": 1}]
    baseline = _invariants(product, inventory, images, [])

    rotated = copy.deepcopy(product)
    rotated["images"][0]["src"] = "https://images.example/new.png"
    assert _invariants(rotated, inventory, images, []) == baseline

    changed_artwork = copy.deepcopy(product)
    changed_artwork["print_areas"][0]["placeholders"][0]["images"][0]["id"] = "artwork-2"
    assert _invariants(changed_artwork, inventory, images, []) != baseline


@pytest.mark.parametrize("interrupt", ["printify", "etsy"])
@pytest.mark.asyncio
async def test_copy_refresh_resumes_after_remote_write(
    isolated_app, monkeypatch: pytest.MonkeyPatch, interrupt: str
) -> None:
    Base.metadata.create_all(get_engine())
    run_id = str(uuid4())
    product = {
        "id": "prod-1", "title": "Old title", "description": "Old description",
        "tags": ["old tag"], "external": {"id": "123"},
        "variants": [], "images": [],
    }
    listing = {
        "listing_id": 123, "shop_id": 42, "state": "active", "title": "Old title",
        "description": "Old description", "tags": ["old tag"],
    }
    inventory = {"products": []}
    images = [{"listing_image_id": 9, "rank": 1}]
    state = {"product": product, "listing": listing, "printify_writes": 0, "etsy_writes": 0,
             "interrupt": interrupt}
    revised = normalize_listing_copy(MarketplaceListingSet(
        listings=[_listing(channel) for channel in Channel]
    ))
    desired = next(x for x in revised.listings if x.channel == Channel.ETSY)
    with session_scope() as session:
        run = RunRepository(session).create(
            RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True),
            f"copy-test-{run_id}",
        )
        run_id = run.id
        run.status = "published"
        run.template_snapshot = fixture_product_template().model_dump(mode="json")
        run.listings = revised.model_dump(mode="json")
        session.add(PublishRecord(
            run_id=run_id, channel="etsy", status="succeeded",
            printify_product_id="prod-1", external_product_id="123", product_fingerprint="old",
        ))
        batch = CopyRefreshBatchRecord(status="pending_review", version=1)
        session.add(batch)
        session.flush()
        item = CopyRefreshItemRecord(
            batch_id=batch.id, run_id=run_id, channel="etsy", printify_product_id="prod-1",
            marketplace_listing_id="123", printify_shop_id="shop-1",
            before_json={"printify": {"title": "Old title", "description": "Old description", "tags": ["old tag"]},
                         "etsy": {"title": "Old title", "description": "Old description", "tags": ["old tag"]}},
            after_json=desired.model_dump(mode="json"),
            baseline_json=_invariants(product, inventory, images, []),
            status="pending_review", stage="prepared",
        )
        session.add(item)
        session.flush()
        batch.digest = _batch_digest([item])
        batch_id = batch.id

    class Printify:
        def __init__(self, settings):  # type: ignore[no-untyped-def]
            pass

        async def product(self, shop_id, product_id):  # type: ignore[no-untyped-def]
            assert (shop_id, product_id) == ("shop-1", "prod-1")
            return copy.deepcopy(state["product"])

        async def update_product_copy(self, shop_id, product_id, title, description, tags):  # type: ignore[no-untyped-def]
            state["printify_writes"] += 1
            state["product"].update(title=title, description=description, tags=tags)
            if state["interrupt"] == "printify":
                state["interrupt"] = None
                raise RuntimeError("interrupted after Printify write")

        async def close(self):  # type: ignore[no-untyped-def]
            pass

    class Etsy:
        def __init__(self, settings, token):  # type: ignore[no-untyped-def]
            pass

        async def listing(self, listing_id):  # type: ignore[no-untyped-def]
            assert listing_id == 123
            return copy.deepcopy(state["listing"])

        async def inventory(self, listing_id):  # type: ignore[no-untyped-def]
            return inventory

        async def images(self, listing_id):  # type: ignore[no-untyped-def]
            return images

        async def variation_images(self, listing_id):  # type: ignore[no-untyped-def]
            return []

        async def update_listing(self, listing_id, payload):  # type: ignore[no-untyped-def]
            state["etsy_writes"] += 1
            state["listing"].update(
                title=payload["title"], description=payload["description"],
                tags=payload["tags"].split(","),
            )
            if state["interrupt"] == "etsy":
                state["interrupt"] = None
                raise RuntimeError("interrupted after Etsy write")

        async def close(self):  # type: ignore[no-untyped-def]
            pass

    async def token(settings):  # type: ignore[no-untyped-def]
        return "fixture"

    monkeypatch.setattr("merch.copy_refresh.PrintifyClient", Printify)
    monkeypatch.setattr("merch.copy_refresh.EtsyStorefrontClient", Etsy)
    monkeypatch.setattr("merch.copy_refresh.etsy_access_token", token)
    monkeypatch.setattr("merch.copy_refresh._verify_approved_state", lambda *args: None)
    settings = get_settings().model_copy(update={"etsy_shop_id": 42})
    approved = get_copy_refresh_batch(batch_id)
    approve_copy_refresh_batch(batch_id, approved["version"], approved["digest"])
    assert await apply_copy_refresh_batch(batch_id, settings) == "verification_required"
    assert get_copy_refresh_batch(batch_id)["items"][0]["status"] == "verification_required"
    retry_copy_refresh_batch(batch_id)
    assert await apply_copy_refresh_batch(batch_id, settings) == "applied"
    assert state["printify_writes"] == 1
    assert state["etsy_writes"] == 1
    assert state["listing"]["state"] == "active"
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert run.status == "published"
        assert run.listings == revised.model_dump(mode="json")


def test_copy_refresh_edit_invalidates_previous_approval(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    listings = normalize_listing_copy(MarketplaceListingSet(
        listings=[_listing(channel) for channel in Channel]
    ))
    with session_scope() as session:
        run = RunRepository(session).create(
            RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True), "copy-edit"
        )
        batch = CopyRefreshBatchRecord(status="pending_review", version=1)
        session.add(batch)
        session.flush()
        item = CopyRefreshItemRecord(
            batch_id=batch.id, run_id=run.id, channel="etsy", printify_product_id="p",
            marketplace_listing_id="1", printify_shop_id="s", status="pending_review",
            after_json=next(x for x in listings.listings if x.channel == Channel.ETSY).model_dump(mode="json"),
            generation_json={"draft": listings.model_dump(mode="json")},
        )
        session.add(item)
        session.flush()
        batch.digest = _batch_digest([item])
        batch_id, item_id, old_digest = batch.id, item.id, batch.digest
    result = edit_copy_refresh_item(batch_id, item_id, CopyRefreshEdit(
        expected_version=1, title="New Trail Graphic T-Shirt",
        long_description="A sunrise trail shirt for hikers. " + ETSY_DISCLOSURE,
        tags=["trail shirt", "hiking gift"],
        alt_text="Text-free sunrise above a winding mountain trail",
    ))
    assert result["version"] == 2 and result["digest"] != old_digest
    assert result["items"][0]["after"]["alt_text"] == (
        "Text-free sunrise above a winding mountain trail"
    )
    with pytest.raises(ValueError, match="changed"):
        approve_copy_refresh_batch(batch_id, 1, old_digest)
