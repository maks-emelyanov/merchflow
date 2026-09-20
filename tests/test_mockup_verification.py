from __future__ import annotations

import hashlib
import io
from copy import deepcopy
from typing import Any, cast

import httpx
import pytest
import respx
from PIL import Image, ImageDraw, PngImagePlugin

from merch.defaults import fixture_product_template
from merch.schemas import ProductTemplate
from merch.services.mockup_selection import select_mockups
from merch.services.mockup_verification import (
    PreparedMockup,
    download_etsy_image,
    prepare_mockups,
    verify_etsy_mockups,
)
from merch.services.storefront import EtsyStorefrontClient, StorefrontVerificationError


def image_bytes(
    color: tuple[int, int, int], *, jpeg: bool = False,
    size: tuple[int, int] = (400, 500), note: str = "",
) -> bytes:
    canvas = Image.new("RGB", (400, 500), "white")
    draw = ImageDraw.Draw(canvas)
    draw.polygon([(140, 70), (260, 70), (325, 145), (280, 190), (265, 150),
                  (265, 440), (135, 440), (135, 150), (120, 190), (75, 145)], fill=color)
    draw.rectangle((165, 160, 235, 210), fill=(220, 160, 50))
    draw.ellipse((177, 171, 193, 187), fill=(20, 50, 90))
    canvas = canvas.resize(size, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", note)
    canvas.save(output, format="JPEG" if jpeg else "PNG", quality=88, pnginfo=metadata)
    return output.getvalue()


def fixtures(
    colors: tuple[tuple[int, int, int], ...] = ((20, 30, 40), (35, 70, 45)),
) -> tuple[ProductTemplate, dict[str, Any], dict[str, Any], dict[str, tuple[bytes, str]]]:
    template = fixture_product_template().model_copy(update={"featured_variant_id": 1001})
    product = {
        "id": "approved-product",
        "images": [
            {"mockup_id": f"product_{variant.variant_id}_front", "position": "front",
             "src": f"https://images.printify.com/{index}.png", "variant_ids": [1001, 1002]}
            for index, variant in enumerate(template.variants)
        ],
    }
    inventory = {"products": [
        {"property_values": [{"property_id": 514, "property_name": "Color",
                              "values": [variant.color], "value_ids": [index + 50]}]}
        for index, variant in enumerate(template.variants)
    ]}
    downloads = {
        f"https://images.printify.com/{index}.png": (image_bytes(color), "image/png")
        for index, color in enumerate(colors)
    }
    downloads.update({
        f"https://i.etsystatic.com/{index}.jpg": (
            image_bytes(color, jpeg=True, size=(320, 400)), "image/jpeg",
        )
        for index, color in enumerate(colors)
    })
    return template, product, inventory, downloads


class Gallery:
    def __init__(self, inventory: dict[str, Any]) -> None:
        self.inventory_data = deepcopy(inventory)
        self.photos: list[dict[str, Any]] = [
            {"listing_image_id": index + 100, "rank": index + 1,
             "url_fullxfull": f"https://i.etsystatic.com/{index}.jpg"}
            for index, _ in enumerate(inventory["products"])
        ]
        self.links: list[dict[str, Any]] = [
            {"property_id": 514, "value_id": index + 50, "image_id": index + 100}
            for index, _ in enumerate(inventory["products"])
        ]

    async def images(self, listing_id: int) -> list[dict[str, Any]]:
        assert listing_id == 7
        return deepcopy(self.photos)

    async def variation_images(self, listing_id: int) -> list[dict[str, Any]]:
        assert listing_id == 7
        return deepcopy(self.links)

    async def inventory(self, listing_id: int) -> dict[str, Any]:
        assert listing_id == 7
        return deepcopy(self.inventory_data)

    @property
    def client(self) -> EtsyStorefrontClient:
        return cast(EtsyStorefrontClient, self)


class Evidence:
    def __init__(self, downloads: dict[str, tuple[bytes, str]]) -> None:
        self.downloads = downloads
        self.objects: dict[str, bytes] = {}
        self.progress: dict[str, Any] = {}
        self.requested: list[str] = []

    async def download(self, url: str) -> tuple[bytes, str]:
        self.requested.append(url)
        return self.downloads[url]

    def write(self, payload: bytes, mime: str) -> str:
        assert mime in {"image/png", "image/jpeg"}
        key = f"evidence/{hashlib.sha256(payload).hexdigest()}"
        self.objects[key] = payload
        return key

    def checkpoint(self, **updates: Any) -> None:
        self.progress.update(updates)

    async def prepare(self, product: dict[str, Any], template: ProductTemplate) -> list[PreparedMockup]:
        return await prepare_mockups(
            product, template, downloader=self.download,
            evidence_writer=self.write, checkpoint=self.checkpoint,
        )

    async def verify(
        self, gallery: Gallery, template: ProductTemplate, expected: list[PreparedMockup],
        inventory: dict[str, Any], *, image_ids: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return await verify_etsy_mockups(
            gallery.client, 7, template, expected, inventory, image_ids=image_ids,
            downloader=self.download, evidence_writer=self.write, checkpoint=self.checkpoint,
        )


async def test_source_manifest_records_actual_rendered_variant_and_pixels() -> None:
    template, product, _, downloads = fixtures()
    evidence = Evidence(downloads)
    prepared = await evidence.prepare(product, template)
    assert [item.variant_id for item in prepared] == [1001, 1002]
    assert evidence.progress["mockup_verification"]["status"] == "sources_verified"
    for item in prepared:
        assert item.evidence["variant_id"] == item.variant_id
        assert item.evidence["identity_method"] == "rendered_variant"
        assert item.evidence["source_dimensions"] == [400, 500]
        assert len(item.evidence["source_pixel_sha256"]) == 64
        assert evidence.objects[item.evidence["source_object_key"]] == item.image


def test_safe_same_color_fallback_does_not_invent_rendered_variant() -> None:
    template, product, _, _ = fixtures()
    product["images"][0].pop("mockup_id")
    product["images"][0]["variant_ids"] = [1001]
    selected = select_mockups(product, template)
    assert selected[0].variant_id is None
    assert selected[0].variant_ids == (1001,)


@pytest.mark.parametrize("encoding", ["identical", "metadata", "jpeg", "jpeg_low_quality"])
async def test_distinct_source_urls_cannot_hide_duplicate_decoded_content(encoding: str) -> None:
    template, product, _, downloads = fixtures()
    original = downloads["https://images.printify.com/0.png"]
    duplicate = original
    if encoding == "metadata":
        duplicate = image_bytes((20, 30, 40), note="different metadata"), "image/png"
        assert duplicate[0] != original[0]
    elif encoding == "jpeg":
        duplicate = image_bytes((20, 30, 40), jpeg=True), "image/jpeg"
    elif encoding == "jpeg_low_quality":
        output = io.BytesIO()
        with Image.open(io.BytesIO(original[0])) as decoded:
            decoded.save(output, format="JPEG", quality=25)
        duplicate = output.getvalue(), "image/jpeg"
    downloads["https://images.printify.com/1.png"] = duplicate
    evidence = Evidence(downloads)
    with pytest.raises(StorefrontVerificationError, match="duplicates or cannot be distinguished"):
        await evidence.prepare(product, template)
    assert evidence.progress["mockup_verification"]["status"] == "failed"
    assert len(evidence.progress["mockup_manifest"]) == 2
    assert evidence.progress["mockup_verification"]["checks"][0]["conflicting_color"] == template.variants[0].color
    assert evidence.objects[evidence.progress["mockup_manifest"][1]["source_object_key"]] == duplicate[0]


async def test_recompressed_resized_etsy_content_and_exact_links_pass() -> None:
    template, product, inventory, downloads = fixtures()
    evidence = Evidence(downloads)
    expected = await evidence.prepare(product, template)
    result = await evidence.verify(Gallery(inventory), template, expected, inventory)
    assert result["status"] == "verified"
    assert result["image_ids"] == {template.variants[0].color: 100, template.variants[1].color: 101}
    assert result["featured_image_id"] == 100
    assert result["photo_count"] == result["color_photo_links"] == 2
    assert len(evidence.objects) == 4
    assert all(check["status"] == "verified" for check in result["checks"])
    assert all(check["actual_sha256"] != check["source_sha256"] for check in result["checks"])


async def test_close_colors_cannot_be_swapped_within_absolute_jpeg_tolerance() -> None:
    template, product, inventory, downloads = fixtures(((130, 130, 130), (138, 138, 138)))
    evidence = Evidence(downloads)
    expected = await evidence.prepare(product, template)
    downloads["https://i.etsystatic.com/0.jpg"], downloads["https://i.etsystatic.com/1.jpg"] = (
        downloads["https://i.etsystatic.com/1.jpg"], downloads["https://i.etsystatic.com/0.jpg"],
    )
    with pytest.raises(StorefrontVerificationError, match="matches another color or is ambiguous"):
        await evidence.verify(Gallery(inventory), template, expected, inventory)
    failure = evidence.progress["mockup_verification"]
    assert failure["status"] == "failed"
    check = failure["checks"][0]
    assert max(check["comparison"]["channel_mean_error"]) < 3
    assert evidence.objects[check["actual_object_key"]] == downloads["https://i.etsystatic.com/0.jpg"][0]


async def test_retry_saved_photo_ids_still_downloads_and_rejects_wrong_content() -> None:
    template, product, inventory, downloads = fixtures()
    evidence = Evidence(downloads)
    expected = await evidence.prepare(product, template)
    downloads["https://i.etsystatic.com/1.jpg"] = downloads["https://i.etsystatic.com/0.jpg"]
    saved_ids = {template.variants[0].color: 100, template.variants[1].color: 101}
    with pytest.raises(StorefrontVerificationError, match="matches another color"):
        await evidence.verify(Gallery(inventory), template, expected, inventory, image_ids=saved_ids)
    assert evidence.requested[-2:] == ["https://i.etsystatic.com/0.jpg", "https://i.etsystatic.com/1.jpg"]
    assert len(evidence.progress["mockup_verification"]["checks"]) == 2


@pytest.mark.parametrize("changed", ["rank", "url", "membership", "links", "inventory_value_ids"])
async def test_gallery_changes_during_download_cannot_be_certified(changed: str) -> None:
    template, product, inventory, downloads = fixtures()
    evidence = Evidence(downloads)
    expected = await evidence.prepare(product, template)
    gallery = Gallery(inventory)

    async def changing_download(url: str) -> tuple[bytes, str]:
        if url.endswith("/1.jpg"):
            if changed == "rank":
                gallery.photos[0]["rank"], gallery.photos[1]["rank"] = 2, 1
            elif changed == "url":
                gallery.photos[0]["url_fullxfull"] = "https://i.etsystatic.com/replaced.jpg"
            elif changed == "membership":
                gallery.photos[0]["listing_image_id"] = 102
            elif changed == "links":
                gallery.links[0]["image_id"], gallery.links[1]["image_id"] = 101, 100
            else:
                first, second = [item["property_values"][0] for item in gallery.inventory_data["products"]]
                first["value_ids"], second["value_ids"] = second["value_ids"], first["value_ids"]
        return await evidence.download(url)

    with pytest.raises(StorefrontVerificationError, match="changed during mockup content verification"):
        await verify_etsy_mockups(
            gallery.client, 7, template, expected, inventory,
            downloader=changing_download, evidence_writer=evidence.write,
            checkpoint=evidence.checkpoint,
        )
    result = evidence.progress["mockup_verification"]
    assert result["status"] == "failed"
    assert len(result["checks"]) == 2
    assert result["final_gallery"] == gallery.photos
    assert result["final_variation_images"] == gallery.links
    assert result["final_inventory"] == gallery.inventory_data
    if changed == "inventory_value_ids":
        assert result["final_inventory_color_values"] != result["inventory_color_values"]


async def test_gallery_readback_ignores_unrelated_metadata_changes() -> None:
    template, product, inventory, downloads = fixtures()
    evidence = Evidence(downloads)
    expected = await evidence.prepare(product, template)
    gallery = Gallery(inventory)

    async def changing_download(url: str) -> tuple[bytes, str]:
        gallery.photos[0]["alt_text"] = "Another spelling of the same garment"
        gallery.links[0]["unused_metadata"] = "Etsy response metadata"
        gallery.inventory_data["products"][0]["offerings"] = [{"quantity": 99}]
        return await evidence.download(url)

    result = await verify_etsy_mockups(
        gallery.client, 7, template, expected, inventory,
        downloader=changing_download, checkpoint=evidence.checkpoint,
    )
    assert result["status"] == "verified"


@pytest.mark.parametrize("problem", ["missing_link", "extra_link", "duplicate_link", "wrong_link",
                                    "reused_id", "featured", "extra_photo", "missing_photo"])
async def test_gallery_structure_must_match_every_color(problem: str) -> None:
    template, product, inventory, downloads = fixtures()
    evidence = Evidence(downloads)
    expected = await evidence.prepare(product, template)
    gallery = Gallery(inventory)
    if problem == "missing_link":
        gallery.links.pop()
    elif problem == "extra_link":
        gallery.links.append({"property_id": 514, "value_id": 52, "image_id": 102})
    elif problem == "duplicate_link":
        gallery.links.append(gallery.links[0].copy())
    elif problem == "wrong_link":
        gallery.links[0]["image_id"], gallery.links[1]["image_id"] = 101, 100
    elif problem == "reused_id":
        gallery.links[1]["image_id"] = 100
    elif problem == "featured":
        gallery.photos[0]["rank"], gallery.photos[1]["rank"] = 2, 1
    elif problem == "extra_photo":
        gallery.photos.append({"listing_image_id": 102, "rank": 3})
    elif problem == "missing_photo":
        gallery.photos.pop()
    with pytest.raises(StorefrontVerificationError):
        await evidence.verify(gallery, template, expected, inventory)
    assert evidence.progress["mockup_verification"]["status"] == "failed"


async def test_inventory_value_ids_cannot_conflict_across_sizes() -> None:
    template, product, inventory, downloads = fixtures()
    evidence = Evidence(downloads)
    expected = await evidence.prepare(product, template)
    extra = deepcopy(inventory["products"][0])
    extra["property_values"][0]["value_ids"] = [999]
    inventory["products"].append(extra)
    with pytest.raises(StorefrontVerificationError, match="inconsistent value IDs"):
        await evidence.verify(Gallery(inventory), template, expected, inventory)


@pytest.mark.parametrize("bad_url", ["http://i.etsystatic.com/0.jpg", "https://etsystatic.com.evil/0.jpg",
                                    "https://127.0.0.1/0.jpg", "https://i.etsystatic.com:8443/0.jpg",
                                    "https://secret@i.etsystatic.com/0.jpg"])
async def test_untrusted_etsy_image_url_is_rejected_before_downloading(bad_url: str) -> None:
    template, product, inventory, downloads = fixtures()
    evidence = Evidence(downloads)
    expected = await evidence.prepare(product, template)
    gallery = Gallery(inventory)
    gallery.photos[0]["url_fullxfull"] = bad_url
    with pytest.raises(StorefrontVerificationError, match="outside Etsy CDN"):
        await evidence.verify(gallery, template, expected, inventory)
    assert bad_url not in evidence.requested


async def test_untrusted_source_url_is_rejected_before_downloading() -> None:
    template, product, _, downloads = fixtures()
    product["images"][0]["src"] = "https://printify.com.evil/0.png"
    evidence = Evidence(downloads)
    with pytest.raises(StorefrontVerificationError, match="outside Printify"):
        await evidence.prepare(product, template)
    assert evidence.requested == []


@pytest.mark.parametrize("invalid", ["corrupt", "format_mismatch", "too_small", "too_large"])
async def test_invalid_source_images_fail_with_retained_evidence(invalid: str) -> None:
    template, product, _, downloads = fixtures()
    payload, mime = downloads["https://images.printify.com/0.png"]
    if invalid == "corrupt":
        payload = b"this is not a PNG image"
    elif invalid == "format_mismatch":
        mime = "image/jpeg"
    elif invalid == "too_small":
        payload = image_bytes((20, 30, 40), size=(32, 40))
    else:
        payload = b"x" * 15_000_001
    downloads["https://images.printify.com/0.png"] = payload, mime
    evidence = Evidence(downloads)
    with pytest.raises(StorefrontVerificationError):
        await evidence.prepare(product, template)
    assert evidence.progress["mockup_verification"]["status"] == "failed"
    assert evidence.objects[evidence.progress["mockup_manifest"][0]["source_object_key"]] == payload


async def test_changed_aspect_ratio_is_rejected() -> None:
    template, product, inventory, downloads = fixtures()
    evidence = Evidence(downloads)
    expected = await evidence.prepare(product, template)
    downloads["https://i.etsystatic.com/0.jpg"] = image_bytes((20, 30, 40), jpeg=True, size=(320, 320)), "image/jpeg"
    with pytest.raises(StorefrontVerificationError, match="differs from its approved mockup"):
        await evidence.verify(Gallery(inventory), template, expected, inventory)


@respx.mock
async def test_etsy_cdn_download_retries_brief_unavailability() -> None:
    payload = image_bytes((20, 30, 40), jpeg=True)
    route = respx.get("https://i.etsystatic.com/image.jpg").mock(side_effect=[
        httpx.Response(404), httpx.Response(200, content=payload, headers={"content-type": "image/jpeg"}),
    ])
    assert await download_etsy_image("https://i.etsystatic.com/image.jpg") == (payload, "image/jpeg")
    assert route.call_count == 2


@respx.mock
async def test_etsy_cdn_redirect_is_not_followed() -> None:
    respx.get("https://i.etsystatic.com/image.jpg").respond(302, headers={"location": "http://127.0.0.1/secret"})
    with pytest.raises(StorefrontVerificationError, match="download could not be verified"):
        await download_etsy_image("https://i.etsystatic.com/image.jpg")
    assert len(respx.calls) == 1
