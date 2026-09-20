from __future__ import annotations

import io
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw

from merch.config import Settings, get_settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.models import Base
from merch.pipeline import _verify_etsy_publish, finish_publishing, publish_channel_run
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import (
    Channel,
    MarketplaceListing,
    PriceQuote,
    ProductTemplate,
    PublishStatus,
    RunInput,
    RunStatus,
)
from merch.services.printify import PrintifyClient
from merch.services.storage import ArtifactStorage
from merch.services.storefront import StorefrontVerificationError
from merch.setup_etsy_tee import COLORS


def shirt_photo(color: str, *, jpeg: bool = False) -> bytes:
    image = Image.new("RGB", (320, 400), "#ededed")
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(100, 32), (131, 24), (189, 24), (220, 32), (283, 100), (241, 139),
         (222, 116), (222, 360), (98, 360), (98, 116), (79, 139), (37, 100)],
        fill=color,
    )
    draw.ellipse((136, 110, 184, 158), fill="#ee951b")
    draw.rectangle((126, 178, 194, 194), fill="#90bcea")
    stream = io.BytesIO()
    if jpeg:
        image = image.resize((256, 320), Image.Resampling.LANCZOS)
        image.save(stream, format="JPEG", quality=87)
    else:
        image.save(stream, format="PNG")
    return stream.getvalue()


class GalleryPrintify:
    product_payload = PrintifyClient.product_payload
    product_fingerprint = staticmethod(PrintifyClient.product_fingerprint)

    def __init__(self, product: dict[str, Any]) -> None:
        self.remote = product
        self.publishes = 0

    async def validate_template(self, template: ProductTemplate) -> ProductTemplate:
        return template

    async def upload_image(self, filename: str, data: bytes) -> dict[str, str]:
        return {"id": "upload-1"}

    async def create_product(self, shop_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return deepcopy(self.remote)

    async def product(self, shop_id: str, product_id: str) -> dict[str, Any]:
        return deepcopy(self.remote)

    async def publish(self, shop_id: str, product_id: str) -> dict[str, str]:
        self.publishes += 1
        return {"status": "accepted"}

    async def close(self) -> None:
        pass


class GalleryEtsy:
    configured = True

    def __init__(
        self, listing: MarketplaceListing, inventory: dict[str, Any],
        photos: list[dict[str, Any]], links: list[dict[str, int]],
    ) -> None:
        self.listing_data = {
            "listing_id": 99, "shop_id": 42, "state": "active", "title": listing.title,
        }
        self.inventory_data = inventory
        self.photos = photos
        self.links = links

    async def listing(self, listing_id: int) -> dict[str, Any]:
        assert listing_id == 99
        return deepcopy(self.listing_data)

    async def inventory(self, listing_id: int) -> dict[str, Any]:
        assert listing_id == 99
        return deepcopy(self.inventory_data)

    async def images(self, listing_id: int) -> list[dict[str, Any]]:
        assert listing_id == 99
        return deepcopy(self.photos)

    async def variation_images(self, listing_id: int) -> list[dict[str, int]]:
        assert listing_id == 99
        return deepcopy(self.links)

    async def close(self) -> None:
        pass


@dataclass
class GalleryCase:
    run_id: str
    settings: Settings
    template: ProductTemplate
    listing: MarketplaceListing
    quotes: list[PriceQuote]
    printify: GalleryPrintify
    etsy: GalleryEtsy
    downloads: dict[str, tuple[bytes, str]]
    downloaded: list[str]


@pytest.fixture
def gallery_case(isolated_app, monkeypatch: pytest.MonkeyPatch, request) -> GalleryCase:  # type: ignore[no-untyped-def]
    from merch.services import mockup_verification

    colors = COLORS if getattr(request, "param", 2) == 14 else {
        "Black": "#111827", "Forest": "#1f6e32",
    }
    template = fixture_product_template()
    template = template.model_copy(update={
        "featured_variant_id": 1001,
        "variants": [
            template.variants[0].model_copy(update={
                "variant_id": 1001 + index, "title": f"{color} / M", "size": "M",
                "color": color, "color_hex": color_hex,
            })
            for index, (color, color_hex) in enumerate(colors.items())
        ],
    })
    settings = get_settings().model_copy(update={
        "publish_mode": "live", "storage_backend": "local", "etsy_shop_id": 42,
        "etsy_native_publish_grace_seconds": 0,
    })
    listing = MarketplaceListing(
        channel=Channel.ETSY, title="Approved original shirt", short_description="Original shirt",
        long_description="An original illustration on a shirt.", tags=["original shirt"],
        bullet_points=[], alt_text="Original illustration", target_customer="Runners",
        gift_occasions=[], seo_meta_title="Original shirt", seo_meta_description="Original shirt",
    )
    quotes = [PriceQuote(
        channel=Channel.ETSY, variant_id=variant.variant_id,
        production_cost_cents=variant.production_cost_cents, retail_price_cents=2599,
        estimated_fee_cents=300, estimated_margin=0.4,
    ) for variant in template.variants]
    product: dict[str, Any] = {
        "id": "product-1", "is_locked": False, "external": {"id": "99"},
        "variants": [], "images": [],
    }
    inventory: dict[str, Any] = {"products": []}
    photos = []
    links = []
    downloads: dict[str, tuple[bytes, str]] = {}
    for index, variant in enumerate(template.variants):
        image_id, value_id = 701 + index, 801 + index
        source = f"https://images.printify.com/mockup/product-1/{variant.variant_id}/front.png"
        uploaded = f"https://i.etsystatic.com/99/{image_id}.jpg"
        product["variants"].append({
            "id": variant.variant_id, "sku": f"sku-{variant.variant_id}",
            "is_enabled": True, "is_default": index == 0, "price": 2599,
        })
        product["images"].append({
            "mockup_id": f"product-1_{variant.variant_id}_front", "position": "front",
            "variant_ids": [item.variant_id for item in template.variants], "src": source,
        })
        inventory["products"].append({
            "sku": f"sku-{variant.variant_id}",
            "property_values": [
                {"property_id": 513, "property_name": "Size", "values": ["M"], "value_ids": [1]},
                {"property_id": 514, "property_name": "Color", "values": [variant.color],
                 "value_ids": [value_id]},
            ],
            "offerings": [{
                "is_enabled": True, "quantity": 999,
                "price": {"amount": 2599, "divisor": 100, "currency_code": "USD"},
            }],
        })
        photos.append({"listing_image_id": image_id, "rank": index + 1, "url_fullxfull": uploaded})
        links.append({"property_id": 514, "value_id": value_id, "image_id": image_id})
        downloads[source] = (shirt_photo(str(variant.color_hex)), "image/png")
        downloads[uploaded] = (shirt_photo(str(variant.color_hex), jpeg=True), "image/jpeg")
    downloaded: list[str] = []

    async def download(url: str) -> tuple[bytes, str]:
        downloaded.append(url)
        return downloads[url]

    async def token(settings: Settings) -> str:
        return "test-token"

    async def no_wait(seconds: float) -> None:
        pass

    printify = GalleryPrintify(product)
    etsy = GalleryEtsy(listing, inventory, photos, links)
    monkeypatch.setattr(mockup_verification, "download_mockup", download)
    monkeypatch.setattr(mockup_verification, "download_etsy_image", download)
    monkeypatch.setattr("merch.pipeline.PrintifyClient", lambda settings: printify)
    monkeypatch.setattr("merch.pipeline.EtsyStorefrontClient", lambda *args, **kwargs: etsy)
    monkeypatch.setattr("merch.pipeline.etsy_access_token", token)
    monkeypatch.setattr("merch.pipeline.asyncio.sleep", no_wait)

    Base.metadata.create_all(get_engine())
    storage = ArtifactStorage(settings)
    object_key, digest = storage.put(b"saved-approved-artwork")
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=False)
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
        repository = RunRepository(session)
        run = repository.create(value, f"test-{value.run_id}")
        run.status = RunStatus.PUBLISHING.value
        run.template_snapshot = template.model_dump(mode="json")
        run.listings = {"listings": [listing.model_dump(mode="json")]}
        run.price_quotes = [item.model_dump(mode="json") for item in quotes]
        repository.add_artifact(
            str(value.run_id), kind="production-v1", revision=1,
            object_key=object_key, sha256=digest, width=400, height=500, metadata={},
        )
    return GalleryCase(
        str(value.run_id), settings, template, listing, quotes, printify, etsy, downloads, downloaded,
    )


@pytest.mark.asyncio
async def test_automatic_publish_blocks_duplicate_source_pixels_before_publish(
    gallery_case: GalleryCase,
) -> None:
    case = gallery_case
    sources = [item["src"] for item in case.printify.remote["images"]]
    assert sources[0] != sources[1]
    case.downloads[sources[1]] = case.downloads[sources[0]]

    status = await publish_channel_run(case.run_id, Channel.ETSY, case.settings)
    finish_publishing(case.run_id, [status])

    assert status == PublishStatus.RECONCILIATION_REQUIRED
    assert case.printify.publishes == 0
    with session_scope() as session:
        run = RunRepository(session).get(case.run_id, full=True)
        assert run.status == RunStatus.VERIFICATION_REQUIRED.value
        publish = run.publishes[0]
        assert publish.error
        assert not publish.response_data.get("publish_started")
        assert publish.response_data["mockup_verification"]["status"] == "failed"
        assert not publish.response_data.get("verification")
        manifest = publish.response_data["mockup_manifest"]
        assert len(manifest) == 2
        storage = ArtifactStorage(case.settings)
        assert all(
            storage.get(item["source_object_key"]) == case.downloads[item["source"]][0]
            for item in manifest
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_image_id", [None, 701])
async def test_native_verification_checks_nonfeatured_color_even_on_retry(
    gallery_case: GalleryCase, prior_image_id: int | None,
) -> None:
    case = gallery_case
    # Rank one and the saved image ID are correct; only Forest's pixels are wrong.
    case.downloads[case.etsy.photos[1]["url_fullxfull"]] = case.downloads[
        case.etsy.photos[0]["url_fullxfull"]
    ]

    with pytest.raises(StorefrontVerificationError):
        await _verify_etsy_publish(
            case.run_id, case.printify, case.settings, "fixture-etsy", "product-1",
            case.template, case.listing, case.quotes, prior_image_id,
        )

    assert case.etsy.photos[1]["url_fullxfull"] in case.downloaded
    with session_scope() as session:
        publish = RunRepository(session).get(case.run_id, full=True).publishes[0]
        evidence = publish.response_data["mockup_verification"]
        assert evidence["status"] == "failed"
        assert evidence["checks"][-1]["color"] == "Forest"
        assert "comparison" in evidence["checks"][-1]
        storage = ArtifactStorage(case.settings)
        assert storage.get(evidence["checks"][-1]["actual_object_key"]) == case.downloads[
            case.etsy.photos[1]["url_fullxfull"]
        ][0]


@pytest.mark.asyncio
async def test_saved_featured_id_does_not_bypass_featured_content_check(
    gallery_case: GalleryCase,
) -> None:
    case = gallery_case
    case.downloads[case.etsy.photos[0]["url_fullxfull"]] = case.downloads[
        case.etsy.photos[1]["url_fullxfull"]
    ]
    with pytest.raises(StorefrontVerificationError):
        await _verify_etsy_publish(
            case.run_id, case.printify, case.settings, "fixture-etsy", "product-1",
            case.template, case.listing, case.quotes, 701,
        )
    assert case.etsy.photos[0]["url_fullxfull"] in case.downloaded


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_automatic_publish_retains_readback_failure_and_requires_verification(
    gallery_case: GalleryCase, retry: bool,
) -> None:
    case = gallery_case
    if retry:
        with session_scope() as session:
            publish = RunRepository(session).publish_record(case.run_id, Channel.ETSY.value, "fixture")
            publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
            publish.printify_product_id = "product-1"
            publish.artwork_upload_id = "upload-1"
            publish.response_data = {
                "publish_started": True, "publish_response": {"status": "accepted"},
                "featured_image_id": 701,
            }
    # IDs and photos exist, but the color selector points at the other shirt.
    case.etsy.links[1]["image_id"] = case.etsy.links[0]["image_id"]

    status = await publish_channel_run(case.run_id, Channel.ETSY, case.settings)
    finish_publishing(case.run_id, [status])

    assert status == PublishStatus.RECONCILIATION_REQUIRED
    assert case.printify.publishes == int(not retry)
    with session_scope() as session:
        run = RunRepository(session).get(case.run_id, full=True)
        assert run.status == RunStatus.VERIFICATION_REQUIRED.value
        publish = run.publishes[0]
        assert publish.status == PublishStatus.RECONCILIATION_REQUIRED.value
        assert publish.error
        assert publish.response_data["mockup_manifest"]
        assert publish.response_data["mockup_verification"]["status"] == "failed"
        assert not publish.response_data.get("verification")

    # After the gallery is repaired, retry reads it again without another publish.
    case.etsy.links[1]["image_id"] = 702
    status = await publish_channel_run(case.run_id, Channel.ETSY, case.settings)
    finish_publishing(case.run_id, [status])
    assert status == PublishStatus.SUCCEEDED
    assert case.printify.publishes == int(not retry)
    with session_scope() as session:
        run = RunRepository(session).get(case.run_id, full=True)
        assert run.status == RunStatus.PUBLISHED.value
        assert run.publishes[0].response_data["mockup_verification"]["status"] == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("gallery_case", [14], indirect=True)
async def test_automatic_publish_accepts_resized_jpeg_photos_for_every_color(
    gallery_case: GalleryCase,
) -> None:
    case = gallery_case
    status = await publish_channel_run(case.run_id, Channel.ETSY, case.settings)
    finish_publishing(case.run_id, [status])

    assert status == PublishStatus.SUCCEEDED
    assert case.printify.publishes == 1
    assert len({url for url in case.downloaded if "etsystatic.com" in url}) == 14
    with session_scope() as session:
        run = RunRepository(session).get(case.run_id, full=True)
        assert run.status == RunStatus.PUBLISHED.value
        publish = run.publishes[0]
        evidence = publish.response_data["mockup_verification"]
        assert evidence["status"] == "verified"
        assert evidence["color_photo_links"] == evidence["photo_count"] == 14
        assert set(evidence["image_ids"]) == set(COLORS)
        assert evidence["featured_image_id"] == 701
        assert len(evidence["checks"]) == 14
        assert all(check["source_object_key"] and check["actual_object_key"] for check in evidence["checks"])
        assert publish.response_data["verification"]["featured_image_id"] == 701


def test_another_channels_success_does_not_hide_gallery_verification_failure(
    gallery_case: GalleryCase,
) -> None:
    finish_publishing(
        gallery_case.run_id, [PublishStatus.SUCCEEDED, PublishStatus.RECONCILIATION_REQUIRED],
    )
    with session_scope() as session:
        run = RunRepository(session).get(gallery_case.run_id)
        assert run.status == RunStatus.VERIFICATION_REQUIRED.value
