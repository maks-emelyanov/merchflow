"""Normalize Printify's heterogeneous catalog into deterministic product contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from merch.schemas import CatalogProduct, CatalogVariant, PrintSurface


class UnsupportedDecorationMethod(ValueError):
    pass


PLACEMENT_BY_METHOD: dict[str, Literal["placed", "full_bleed", "repeat", "restricted_palette"]] = {
    "dtg": "placed",
    "direct_to_garment": "placed",
    "dtf": "placed",
    "direct_to_film": "placed",
    "digital": "placed",
    "digital_print": "placed",
    "screen_print": "placed",
    "uv": "placed",
    "uv_print": "placed",
    "sublimation": "full_bleed",
    "aop": "full_bleed",
    "all_over_print": "full_bleed",
    "cut_sew": "full_bleed",
    "embroidery": "restricted_palette",
    "laser_engraving": "restricted_palette",
    "engraving": "restricted_palette",
}


def placement_for(
    decoration_method: str, position: str
) -> Literal["placed", "full_bleed", "repeat", "restricted_palette"]:
    method = decoration_method.strip().casefold().replace("-", "_").replace(" ", "_")
    if method not in PLACEMENT_BY_METHOD:
        raise UnsupportedDecorationMethod(
            f"unsupported Printify decoration method: {decoration_method}"
        )
    placement = PLACEMENT_BY_METHOD[method]
    normalized_position = position.casefold().replace("-", "_").replace(" ", "_")
    if "pattern" in normalized_position:
        return "repeat"
    if any(token in normalized_position for token in ("all_over", "wrap", "full_bleed")):
        return "full_bleed"
    return placement


def _shipping_by_variant(payload: dict[str, Any]) -> dict[int, int]:
    result: dict[int, int] = {}
    for profile in payload.get("profiles", []):
        countries = {str(item).upper() for item in profile.get("countries", [])}
        if "US" not in countries and "USA" not in countries:
            continue
        first_item = profile.get("first_item") or {}
        if str(first_item.get("currency", "USD")).upper() != "USD":
            continue
        cost = first_item.get("cost")
        if type(cost) is not int or cost < 0:
            continue
        for variant_id in profile.get("variant_ids", []):
            if type(variant_id) is int:
                result[variant_id] = cost
    return result


def normalize_catalog_product(
    blueprint: dict[str, Any],
    provider: dict[str, Any],
    variants_payload: dict[str, Any],
    shipping_payload: dict[str, Any],
    *,
    synced_at: datetime | None = None,
) -> CatalogProduct:
    blueprint_id = int(blueprint["id"])
    provider_id = int(provider["id"])
    synced_at = synced_at or datetime.now(UTC)
    shipping = _shipping_by_variant(shipping_payload)
    variants: list[CatalogVariant] = []
    for raw in variants_payload.get("variants", []):
        variant_id = raw.get("id")
        if type(variant_id) is not int or variant_id <= 0:
            continue
        surfaces: list[PrintSurface] = []
        for placeholder in raw.get("placeholders", []):
            try:
                position = str(placeholder["position"])
                method = str(placeholder["decoration_method"])
                width = int(placeholder["width"])
                height = int(placeholder["height"])
            except KeyError, TypeError, ValueError:
                continue
            placement: Literal[
                "placed", "full_bleed", "repeat", "restricted_palette", "unsupported"
            ]
            try:
                placement = placement_for(method, position)
            except UnsupportedDecorationMethod:
                placement = "unsupported"
            if width <= 0 or height <= 0:
                continue
            surfaces.append(
                PrintSurface(
                    position=position,
                    decoration_method=method,
                    width=width,
                    height=height,
                    placement=placement,
                    rules={
                        str(key): value
                        for key, value in placeholder.items()
                        if key not in {"position", "decoration_method", "width", "height"}
                        and isinstance(value, (str, int, float, bool))
                    },
                )
            )
        if not surfaces:
            continue
        options = {
            str(key): str(value)
            for key, value in (raw.get("options") or {}).items()
            if str(key).strip() and str(value).strip()
        }
        variants.append(
            CatalogVariant(
                variant_id=variant_id,
                title=str(raw.get("title") or variant_id),
                options=options,
                surfaces=surfaces,
                available=bool(raw.get("is_available", True)),
                shipping_cost_cents=shipping.get(variant_id),
            )
        )
    if not variants:
        raise ValueError(f"Printify product {blueprint_id}/{provider_id} has no printable variants")
    canonical = {
        "blueprint": blueprint,
        "provider": provider,
        "variants": variants_payload,
        "shipping": shipping_payload,
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, default=str).encode()
    ).hexdigest()
    return CatalogProduct(
        blueprint_id=blueprint_id,
        print_provider_id=provider_id,
        title=str(blueprint.get("title") or f"Printify blueprint {blueprint_id}"),
        description=str(blueprint.get("description") or ""),
        brand=str(blueprint["brand"]) if blueprint.get("brand") else None,
        model=str(blueprint["model"]) if blueprint.get("model") else None,
        tags=[str(item) for item in blueprint.get("tags", [])],
        variants=variants,
        synced_at=synced_at,
        source_fingerprint=fingerprint,
    )


def fixture_catalog(synced_at: datetime | None = None) -> list[CatalogProduct]:
    """Representative catalog used by fake providers and contract tests."""
    synced_at = synced_at or datetime.now(UTC)
    specifications = [
        (6, 1, "Unisex Graphic T-Shirt", "dtg", "front", "color", ["Black", "Natural"]),
        (68, 9, "Ceramic Mug", "sublimation", "wrap", "size", ["11 oz", "15 oz"]),
        (282, 1, "Matte Art Poster", "digital", "front", "size", ["8x10", "12x16"]),
        (450, 29, "Protective Phone Case", "uv", "back", "device", ["Phone A", "Phone B"]),
        (7060, 99, "All-Over Tote Bag", "cut_sew", "all_over_front", "size", ["One size"]),
        (1090, 3, "Embroidered Cap", "embroidery", "front", "color", ["Black", "Navy"]),
    ]
    products: list[CatalogProduct] = []
    variant_id = 10000
    for blueprint_id, provider_id, title, method, position, option_name, values in specifications:
        raw_variants = []
        profiles = []
        ids = []
        for index, value in enumerate(values):
            variant_id += 1
            ids.append(variant_id)
            placeholders = [
                {
                    "position": position,
                    "decoration_method": method,
                    "width": 3000 + index * 100,
                    "height": 3000 + index * 100,
                }
            ]
            if title == "All-Over Tote Bag":
                placeholders.append(
                    {
                        "position": "all_over_back",
                        "decoration_method": method,
                        "width": 3000,
                        "height": 3000,
                    }
                )
            raw_variants.append(
                {
                    "id": variant_id,
                    "title": value,
                    "options": {option_name: value},
                    "is_available": True,
                    "placeholders": placeholders,
                }
            )
        profiles.append(
            {
                "variant_ids": ids,
                "first_item": {"currency": "USD", "cost": 499},
                "additional_items": {"currency": "USD", "cost": 250},
                "countries": ["US"],
            }
        )
        product = normalize_catalog_product(
            {"id": blueprint_id, "title": title, "description": f"Fixture {title}"},
            {"id": provider_id, "title": "Fixture provider"},
            {"variants": raw_variants},
            {"profiles": profiles},
            synced_at=synced_at,
        )
        products.append(
            product.model_copy(
                update={
                    "variants": [
                        item.model_copy(update={"production_cost_cents": 500 + index * 125})
                        for index, item in enumerate(product.variants)
                    ]
                }
            )
        )
    return products
