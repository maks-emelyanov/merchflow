"""Fail-closed image-content verification for every approved Etsy garment color.

Compare normalized RGB pixels rather than URLs, metadata, or byte hashes alone.
The absolute error bounds tolerate resizing and JPEG recompression; the unique
nearest-source check prevents those bounds from accepting a different near color.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import warnings
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageChops, ImageOps, ImageStat, UnidentifiedImageError

from merch.schemas import ProductTemplate
from merch.services.mockup_selection import select_mockups
from merch.services.storefront import (
    EtsyStorefrontClient,
    StorefrontVerificationError,
    download_mockup,
)

Downloader = Callable[[str], Awaitable[tuple[bytes, str]]]
EvidenceWriter = Callable[[bytes, str], str]
Checkpoint = Callable[..., None]
MAX_IMAGE_BYTES = 15_000_000
MAX_IMAGE_PIXELS = 25_000_000
NORMALIZED_SIZE = (256, 256)
MAX_CHANNEL_MEAN_ERROR = 3.0
MAX_CHANNEL_RMS_ERROR = 8.0
MIN_MATCH_MARGIN = 0.25


@dataclass
class PreparedMockup:
    color: str
    source: str
    variant_id: int | None
    image: bytes
    content_type: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Pixels:
    image: Image.Image
    width: int
    height: int
    digest: str


def _validate_url(url: str, *, etsy: bool) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    domains = ("etsystatic.com",) if etsy else ("printify.com", "printify.me")
    if (
        parsed.scheme != "https" or parsed.username or parsed.password
        or parsed.port not in (None, 443)
        or not any(host == domain or host.endswith(f".{domain}") for domain in domains)
    ):
        origin = "Etsy CDN" if etsy else "Printify"
        raise StorefrontVerificationError(f"Mockup URL is outside {origin}")


async def download_etsy_image(url: str) -> tuple[bytes, str]:
    """Read only bounded Etsy CDN images; redirects never bypass the host policy."""
    _validate_url(url, etsy=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as http:
        for attempt in range(3):
            try:
                async with http.stream("GET", url) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if content_type not in {"image/jpeg", "image/png"}:
                        raise StorefrontVerificationError("Etsy mockup is not a supported image")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > MAX_IMAGE_BYTES:
                            raise StorefrontVerificationError("Etsy mockup exceeds the image size limit")
                    if not payload:
                        raise StorefrontVerificationError("Etsy mockup is empty")
                    return bytes(payload), content_type
            except httpx.HTTPError as exc:
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code in {404, 408, 429} or exc.response.status_code >= 500
                )
                if not retryable or attempt == 2:
                    raise StorefrontVerificationError("Etsy mockup download could not be verified") from exc
                await asyncio.sleep(0.25 * (attempt + 1))
    raise StorefrontVerificationError("Etsy mockup download could not be verified")


def _pixels(payload: bytes, content_type: str) -> _Pixels:
    if content_type not in {"image/png", "image/jpeg"} or not 0 < len(payload) <= MAX_IMAGE_BYTES:
        raise StorefrontVerificationError("Mockup is not a supported bounded image")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as original:
                width, height = original.size
                if min(width, height) < 64 or width * height > MAX_IMAGE_PIXELS:
                    raise StorefrontVerificationError("Mockup dimensions are outside verification limits")
                expected_format = "PNG" if content_type == "image/png" else "JPEG"
                if original.format != expected_format or getattr(original, "n_frames", 1) != 1:
                    raise StorefrontVerificationError("Mockup encoding does not match its supported image type")
                original.load()
                oriented = ImageOps.exif_transpose(original)
                width, height = oriented.size
                rgba = oriented.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                rgb = Image.alpha_composite(background, rgba).convert("RGB")
                normalized = rgb.resize(NORMALIZED_SIZE, Image.Resampling.LANCZOS)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombWarning,
            Image.DecompressionBombError) as exc:
        raise StorefrontVerificationError("Mockup image pixels could not be decoded safely") from exc
    return _Pixels(normalized, width, height, hashlib.sha256(normalized.tobytes()).hexdigest())


def _comparison(left: _Pixels, right: _Pixels) -> dict[str, Any]:
    difference = ImageStat.Stat(ImageChops.difference(left.image, right.image))
    aspect_error = abs((left.width / left.height) / (right.width / right.height) - 1)
    return {
        "channel_mean_error": [round(value, 6) for value in difference.mean],
        "channel_rms_error": [round(value, 6) for value in difference.rms],
        "mean_error": sum(difference.mean) / 3,
        "aspect_error": aspect_error,
    }


def _indistinguishable(comparison: dict[str, Any]) -> bool:
    # Small compression/rounding differences must not turn one photo into two
    # supposedly distinct garment colors. Close sources require manual review.
    return bool(
        comparison["aspect_error"] <= 0.01
        and max(comparison["channel_mean_error"]) <= 1.5
        and max(comparison["channel_rms_error"]) <= 4.0
    )


def _record_bytes(
    evidence: dict[str, Any], payload: bytes, content_type: str,
    writer: EvidenceWriter | None, prefix: str,
) -> None:
    evidence[f"{prefix}_sha256"] = hashlib.sha256(payload).hexdigest()
    evidence[f"{prefix}_content_type"] = content_type
    evidence[f"{prefix}_bytes"] = len(payload)
    if writer:
        evidence[f"{prefix}_object_key"] = writer(payload, content_type)


def _record_pixels(evidence: dict[str, Any], pixels: _Pixels, prefix: str) -> None:
    evidence[f"{prefix}_pixel_sha256"] = pixels.digest
    evidence[f"{prefix}_dimensions"] = [pixels.width, pixels.height]


def _save(checkpoint: Checkpoint | None, **updates: Any) -> None:
    if checkpoint:
        checkpoint(**deepcopy(updates))


async def prepare_mockups(
    product: dict[str, Any], template: ProductTemplate, *,
    downloader: Downloader | None = None,
    evidence_writer: EvidenceWriter | None = None,
    checkpoint: Checkpoint | None = None,
) -> list[PreparedMockup]:
    """Select, retain and compare all source images before any gallery mutation."""
    manifest: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    prepared: list[PreparedMockup] = []
    decoded: list[_Pixels] = []
    try:
        selected = select_mockups(product, template)
        for item in selected:
            entry: dict[str, Any] = {
                "color": item.color, "source": item.source, "variant_id": item.variant_id,
                "mockup_id": item.mockup_id, "variant_ids": list(item.variant_ids),
                "identity_method": "rendered_variant" if item.variant_id is not None else "single_color_membership",
                "product_id": str(product.get("id") or ""),
            }
            manifest.append(entry)
            _validate_url(item.source, etsy=False)
            payload, content_type = await (downloader or download_mockup)(item.source)
            _record_bytes(entry, payload, content_type, evidence_writer, "source")
            pixels = _pixels(payload, content_type)
            _record_pixels(entry, pixels, "source")
            for prior, prior_pixels in zip(prepared, decoded, strict=True):
                comparison = _comparison(prior_pixels, pixels)
                if _indistinguishable(comparison):
                    checks.append({"color": item.color, "conflicting_color": prior.color, **comparison})
                    raise StorefrontVerificationError(
                        f"Printify mockup content for {item.color} duplicates or cannot be distinguished from {prior.color}"
                    )
            prepared.append(PreparedMockup(
                item.color, item.source, item.variant_id, payload, content_type, entry,
            ))
            decoded.append(pixels)
            _save(checkpoint, mockup_manifest=manifest, mockup_verification={
                "status": "preparing_sources", "checks": checks,
            })
    except Exception as exc:
        error = str(exc)
        _save(checkpoint, mockup_manifest=manifest, mockup_verification={
            "status": "failed", "phase": "source_content", "error": error, "checks": checks,
        })
        if isinstance(exc, StorefrontVerificationError):
            raise
        raise StorefrontVerificationError(f"Mockup source verification failed: {error}") from exc
    _save(checkpoint, mockup_manifest=manifest, mockup_verification={
        "status": "sources_verified", "source_count": len(prepared), "checks": checks,
    })
    return prepared


def _color_values(inventory: dict[str, Any], colors: set[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for product in inventory.get("products", []):
        if product.get("is_deleted"):
            continue
        properties = [prop for prop in product.get("property_values", [])
                      if int(prop.get("property_id") or 0) == 514]
        if len(properties) != 1:
            raise StorefrontVerificationError("Etsy inventory lacks a unique Color selector")
        prop = properties[0]
        labels, value_ids = prop.get("values") or [], prop.get("value_ids") or []
        if len(labels) != 1 or len(value_ids) != 1 or int(value_ids[0]) <= 0:
            raise StorefrontVerificationError("Etsy Color selector lacks a unique value ID")
        color, value_id = str(labels[0]), int(value_ids[0])
        if color in result and result[color] != value_id:
            raise StorefrontVerificationError(f"Etsy returned inconsistent value IDs for {color}")
        result[color] = value_id
    if set(result) != colors or len(set(result.values())) != len(colors):
        raise StorefrontVerificationError("Etsy color values do not match approved gallery colors")
    return result


def _gallery_state(images: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    return sorted(
        (int(item.get("listing_image_id") or 0), int(item.get("rank") or 0),
         str(item.get("url_fullxfull") or item.get("url_570xN") or ""))
        for item in images
    )


def _link_state(links: list[dict[str, Any]]) -> list[tuple[int, int, int]]:
    return sorted(
        (int(item.get("property_id") or 0), int(item.get("value_id") or 0),
         int(item.get("image_id") or 0))
        for item in links
    )


async def verify_etsy_mockups(
    etsy: EtsyStorefrontClient, listing_id: int, template: ProductTemplate,
    expected: list[PreparedMockup], inventory: dict[str, Any], *,
    image_ids: dict[str, int] | None = None,
    downloader: Downloader | None = None,
    evidence_writer: EvidenceWriter | None = None,
    checkpoint: Checkpoint | None = None,
) -> dict[str, Any]:
    """Read actual Etsy content and links, even for saved/adopted image IDs."""
    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {"status": "verifying", "listing_id": listing_id, "checks": checks}
    try:
        colors = {variant.color for variant in template.variants if variant.enabled}
        if len(expected) != len(colors) or {item.color for item in expected} != colors:
            raise StorefrontVerificationError("Source mockups do not match approved colors")
        source_pixels = [_pixels(item.image, item.content_type) for item in expected]
        values = _color_values(inventory, colors)
        result["inventory_color_values"] = values
        images = await etsy.images(listing_id)
        links = await etsy.variation_images(listing_id)
        result["gallery"] = images
        result["variation_images"] = links
        link_map: dict[int, int] = {}
        for link in links:
            if int(link.get("property_id") or 0) != 514:
                raise StorefrontVerificationError("Etsy variation photos contain an unexpected selector")
            value_id, image_id = int(link.get("value_id") or 0), int(link.get("image_id") or 0)
            if value_id in link_map or value_id <= 0 or image_id <= 0:
                raise StorefrontVerificationError("Etsy variation photos contain duplicate or invalid color links")
            link_map[value_id] = image_id
        if set(link_map) != set(values.values()):
            raise StorefrontVerificationError("Etsy variation photos are missing or contain extra color links")
        linked_ids = {color: link_map[value] for color, value in values.items()}
        if image_ids is not None and image_ids != linked_ids:
            raise StorefrontVerificationError("Etsy color-photo links differ from the approved upload mapping")
        if len(set(linked_ids.values())) != len(colors):
            raise StorefrontVerificationError("Etsy reused the same photo for different colors")
        by_id = {int(item.get("listing_image_id") or 0): item for item in images}
        if (
            len(by_id) != len(images) or len(images) != len(colors)
            or set(by_id) != set(linked_ids.values())
        ):
            raise StorefrontVerificationError("Etsy gallery photos do not exactly match approved color photos")
        featured_id = linked_ids[template.featured_variant().color]
        first_ids = [int(item["listing_image_id"]) for item in images if int(item.get("rank") or 0) == 1]
        if first_ids != [featured_id]:
            raise StorefrontVerificationError("Etsy has not placed the approved featured color first")
        actual_pixels: list[_Pixels] = []
        for index, item in enumerate(expected):
            image_id = linked_ids[item.color]
            remote = by_id[image_id]
            url = str(remote.get("url_fullxfull") or remote.get("url_570xN") or "")
            check: dict[str, Any] = {
                "color": item.color, "image_id": image_id, "source": item.source,
                "etsy_url": url, "variant_id": item.variant_id, **item.evidence,
            }
            checks.append(check)
            _validate_url(url, etsy=True)
            payload, content_type = await (downloader or download_etsy_image)(url)
            _record_bytes(check, payload, content_type, evidence_writer, "actual")
            pixels = _pixels(payload, content_type)
            _record_pixels(check, pixels, "actual")
            comparisons = [_comparison(source, pixels) for source in source_pixels]
            own = comparisons[index]
            check["comparison"] = own
            alternatives = sorted(
                ((comparison["mean_error"], expected[other].color)
                 for other, comparison in enumerate(comparisons) if other != index),
            )
            if alternatives:
                nearest_error, nearest_color = alternatives[0]
                check["nearest_other_color"] = nearest_color
                check["nearest_other_mean_error"] = nearest_error
                if nearest_error - own["mean_error"] <= max(MIN_MATCH_MARGIN, own["mean_error"] * 0.5):
                    raise StorefrontVerificationError(
                        f"Etsy photo for {item.color} matches another color or is ambiguous with {nearest_color}"
                    )
            if (
                own["aspect_error"] > 0.01
                or max(own["channel_mean_error"]) > MAX_CHANNEL_MEAN_ERROR
                or max(own["channel_rms_error"]) > MAX_CHANNEL_RMS_ERROR
            ):
                raise StorefrontVerificationError(f"Etsy photo content for {item.color} differs from its approved mockup")
            for prior_index, prior in enumerate(actual_pixels):
                if _indistinguishable(_comparison(prior, pixels)):
                    raise StorefrontVerificationError(
                        f"Etsy photo content for {item.color} duplicates {expected[prior_index].color}"
                    )
            actual_pixels.append(pixels)
            check["status"] = "verified"
            _save(checkpoint, mockup_verification=result)
        # A full gallery download can take time. Do not certify a snapshot if
        # photo membership, ordering, content URLs, color links or their
        # inventory value IDs changed while those pixels were being compared.
        final_images = await etsy.images(listing_id)
        final_links = await etsy.variation_images(listing_id)
        final_inventory = await etsy.inventory(listing_id)
        result["final_gallery"] = final_images
        result["final_variation_images"] = final_links
        result["final_inventory"] = final_inventory
        if _gallery_state(final_images) != _gallery_state(images):
            raise StorefrontVerificationError("Etsy gallery changed during mockup content verification")
        if _link_state(final_links) != _link_state(links):
            raise StorefrontVerificationError("Etsy color-photo links changed during mockup content verification")
        final_values = _color_values(final_inventory, colors)
        result["final_inventory_color_values"] = final_values
        if final_values != values:
            raise StorefrontVerificationError("Etsy color value IDs changed during mockup content verification")
        result.update(
            status="verified", image_ids=linked_ids, featured_image_id=featured_id,
            photo_count=len(images), color_photo_links=len(links),
        )
    except Exception as exc:
        error = str(exc)
        result.update(status="failed", phase="etsy_readback", error=error)
        _save(checkpoint, mockup_verification=result)
        if isinstance(exc, StorefrontVerificationError):
            raise
        raise StorefrontVerificationError(f"Etsy mockup verification failed: {error}") from exc
    _save(checkpoint, mockup_verification=result)
    return result
