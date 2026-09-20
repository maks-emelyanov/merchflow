"""Select front mockups by their rendered Printify variant identity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from merch.schemas import ProductTemplate
from merch.services.storefront import StorefrontVerificationError


@dataclass(frozen=True)
class SelectedMockup:
    color: str
    source: str
    variant_id: int | None
    mockup_id: str
    variant_ids: tuple[int, ...]


def _mockup_variant_id(image: dict[str, Any], product_id: str) -> int | None:
    """Read the rendered variant, not potentially product-wide variant_ids."""
    match = re.match(r"^[^_]+_(\d+)_", str(image.get("mockup_id") or ""))
    encoded_id = int(match[1]) if match else None
    source = urlparse(str(image.get("src") or ""))
    hostname = (source.hostname or "").lower()
    url_match = re.match(r"^/mockup/([^/]+)/(\d+)(?:/|$)", source.path)
    url_id = None
    if (
        source.scheme == "https"
        and (hostname == "printify.com" or hostname.endswith(".printify.com"))
        and url_match
    ):
        if url_match[1] != product_id:
            raise StorefrontVerificationError("Printify mockup URL belongs to another product")
        url_id = int(url_match[2])
    if encoded_id is not None and url_id is not None and encoded_id != url_id:
        raise StorefrontVerificationError("Printify mockup has conflicting variant identities")
    return encoded_id if encoded_id is not None else url_id


def select_mockups(product: dict[str, Any], template: ProductTemplate) -> list[SelectedMockup]:
    featured = template.featured_variant()
    colors = sorted({item.color for item in template.variants if item.enabled})
    colors.remove(featured.color)
    colors.insert(0, featured.color)
    if len(colors) > 20:
        raise StorefrontVerificationError("Etsy supports at most 20 listing photos")
    front_images = [
        (image, _mockup_variant_id(image, str(product.get("id") or "")))
        for image in product.get("images", [])
        if image.get("position") == "front" and image.get("src")
    ]
    result: list[SelectedMockup] = []
    for color in colors:
        variants = [item for item in template.variants if item.enabled and item.color == color]
        variants.sort(key=lambda item: (item.size != featured.size, item.variant_id))
        selected: tuple[dict[str, Any], int | None] | None = next((
            (image, rendered_id)
            for variant in variants for image, rendered_id in front_images
            if rendered_id == variant.variant_id
        ), None)
        if selected is None:
            color_ids = {item.variant_id for item in template.variants if item.color == color}
            selected = next((
                (image, rendered_id)
                for variant in variants for image, rendered_id in front_images
                if rendered_id is None
                and variant.variant_id in (image.get("variant_ids") or [])
                and set(image["variant_ids"]) <= color_ids
            ), None)
        if selected is None:
            raise StorefrontVerificationError(f"Printify has no unambiguous front mockup for {color}")
        image, rendered_id = selected
        source = str(image["src"])
        if any(previous.source == source for previous in result):
            raise StorefrontVerificationError("Printify supplied the same mockup for different colors")
        result.append(SelectedMockup(
            color=color, source=source, variant_id=rendered_id,
            mockup_id=str(image.get("mockup_id") or ""),
            variant_ids=tuple(int(value) for value in image.get("variant_ids") or []),
        ))
    return result


def mockup_plan(product: dict[str, Any], template: ProductTemplate) -> list[tuple[str, str]]:
    """Compatibility view of the structured selection used by verification."""
    return [(item.color, item.source) for item in select_mockups(product, template)]
