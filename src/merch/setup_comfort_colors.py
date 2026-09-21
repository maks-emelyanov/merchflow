"""Preflight and explicitly activate a verified Comfort Colors 1717 template.

Catalog reads discover current variant IDs and dimensions. Production prices
must come from the operator's reviewed Printify costs, never a catalog headline.
The default command only prints a preview and a costs-file skeleton.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from merch.config import Settings, get_settings
from merch.database import session_scope
from merch.domain.pricing import quote_price
from merch.domain.print_areas import largest_compatible_print_area, variant_print_dimensions
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import Channel, GarmentFacts, ProductTemplate, StrictModel, VariantConfig
from merch.services.printify import PrintifyClient

BLUEPRINT_ID = 706
PROVIDER_ID = 99
SIZES = ("S", "M", "L", "XL", "2XL", "3XL", "4XL")
# Printify catalog swatches are used for contrast previews, not color-match guarantees.
COLORS = {
    "Pepper": "#5F605B",
    "Ivory": "#FFF7E7",
    "Black": "#000000",
    "Blue Jean": "#788CA1",
    "Moss": "#747F66",
    "Espresso": "#846B5B",
    "True Navy": "#183047",
    "Crimson": "#B66A74",
    "White": "#FFFFFF",
    "Bay": "#C3CFC1",
    "Butter": "#F5E1A4",
    "Blossom": "#F8D1E2",
    "Sage": "#6D775C",
    "Yam": "#C9814F",
}
SOURCE_URL = "https://printify.com/app/products/706/comfort-colors/unisex-garmentdyed-tshirt"


class ReviewedCosts(StrictModel):
    currency: Literal["USD"]
    reviewed_at: date
    costs_by_variant: dict[str, Annotated[int, Field(strict=True, gt=0)]]

    @field_validator("reviewed_at")
    @classmethod
    def review_is_not_future(cls, value: date) -> date:
        if value > datetime.now(UTC).date():
            raise ValueError("cost review date cannot be in the future")
        return value

    @field_validator("costs_by_variant")
    @classmethod
    def positive_variant_ids(cls, value: dict[str, int]) -> dict[str, int]:
        if not value or any(not key.isdecimal() or str(int(key)) != key or int(key) < 1 for key in value):
            raise ValueError("costs must use canonical positive variant IDs as keys")
        return value


def read_reviewed_costs(path: Path) -> ReviewedCosts:
    return ReviewedCosts.model_validate_json(path.read_text())


def catalog_selection(catalog: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Require every color/size with proportionally compatible front-DTG areas."""
    by_options: dict[tuple[str, str], dict[str, Any]] = {}
    for row in catalog.get("variants", []):
        options = row.get("options", {})
        color, size = options.get("color"), options.get("size")
        if color not in COLORS or size not in SIZES:
            continue
        key = (color, size)
        if key in by_options:
            raise ValueError(f"Ambiguous catalog variants for {color} / {size}")
        by_options[key] = row
    selected = []
    dimensions: set[tuple[int, int]] = set()
    missing = []
    for color in COLORS:
        for size in SIZES:
            row = by_options.get((color, size))
            if row is None or row.get("is_available") is False:
                missing.append(f"{color} / {size}")
                continue
            if type(row.get("id")) is not int or row["id"] <= 0:
                raise ValueError(f"Invalid catalog variant ID for {color} / {size}")
            try:
                dimensions.add(
                    variant_print_dimensions(row, position="front", decoration_method="dtg")
                )
            except ValueError as exc:
                raise ValueError(f"{exc} for {color} / {size}") from exc
            selected.append(row)
    if missing:
        raise ValueError(
            "Printify Choice is missing requested in-stock variants: " + ", ".join(missing)
        )
    if len({row["id"] for row in selected}) != len(selected):
        raise ValueError("Selected catalog variant IDs are not unique")
    # Printify scales the shared artwork to each garment's placeholder. Render
    # at the largest size, accepting only a pixel of catalog rounding per side.
    try:
        width, height = largest_compatible_print_area(dimensions)
    except ValueError as exc:
        raise ValueError("Selected variants have incompatible front-DTG print dimensions") from exc
    return selected, width, height


def build_template(
    catalog: dict[str, Any], current: ProductTemplate, costs: ReviewedCosts,
    *, verified_at: date,
) -> ProductTemplate:
    selected, width, height = catalog_selection(catalog)
    expected = {str(row["id"]) for row in selected}
    supplied = set(costs.costs_by_variant)
    if supplied != expected:
        missing = ", ".join(sorted(expected - supplied)) or "none"
        extra = ", ".join(sorted(supplied - expected)) or "none"
        raise ValueError(f"Costs must match selected variants exactly; missing: {missing}; extra: {extra}")
    costs_by_size = {
        size: {
            costs.costs_by_variant[str(row["id"])]
            for row in selected
            if row["options"]["size"] == size
        }
        for size in SIZES
    }
    if any(len(values) != 1 for values in costs_by_size.values()):
        raise ValueError(
            "Printify Choice production costs must be size-tiered across the selected colors "
            "so reserve colors can be priced safely"
        )
    variants = [
        VariantConfig(
            variant_id=row["id"], title=row["title"],
            color=row["options"]["color"], color_hex=COLORS[row["options"]["color"]],
            size=row["options"]["size"], production_cost_cents=costs.costs_by_variant[str(row["id"])],
        )
        for row in selected
    ]
    return ProductTemplate(
        name="Comfort Colors 1717 / Printify Choice / 14 colors / S-4XL",
        blueprint_id=BLUEPRINT_ID, print_provider_id=PROVIDER_ID,
        print_width=width, print_height=height, variants=variants,
        featured_variant_id=next(
            item.variant_id for item in variants if item.color == "Pepper" and item.size == "L"
        ),
        channels=current.channels,
        etsy_production_partner_confirmed=current.etsy_production_partner_confirmed,
        garment_facts=GarmentFacts(
            brand="Comfort Colors", model="1717", finish="garment-dyed", fit="relaxed",
            source_url=SOURCE_URL, verified_at=verified_at,
        ),
        production_costs_reviewed_at=costs.reviewed_at,
    )


def verify_connections(
    current: ProductTemplate, settings: Settings, shops: list[dict[str, Any]],
    blueprint: dict[str, Any], providers: list[dict[str, Any]],
) -> None:
    etsy = [item for item in current.channels if item.enabled and item.channel == Channel.ETSY]
    if len(etsy) != 1:
        raise ValueError("One enabled Etsy shop and its fee settings are required")
    if settings.etsy_production_partner_check_enabled and not current.etsy_production_partner_confirmed:
        raise ValueError("Confirm the Etsy production partner before activating a garment")
    known_shops = {str(shop["id"]): shop for shop in shops}
    for channel in current.channels:
        if channel.enabled and channel.printify_shop_id not in known_shops:
            raise ValueError(f"Configured {channel.channel.value} Printify shop is unavailable")
    remote_etsy = known_shops[etsy[0].printify_shop_id]
    if str(remote_etsy.get("sales_channel", "")).casefold() != "etsy":
        raise ValueError("Configured Etsy Printify shop is not connected to Etsy")
    brand = str(blueprint.get("brand", "")).replace("®", "").strip().casefold()
    if blueprint.get("id") != BLUEPRINT_ID or brand != "comfort colors" or str(blueprint.get("model")) != "1717":
        raise ValueError("Printify blueprint is not the expected Comfort Colors 1717")
    provider = next((item for item in providers if item.get("id") == PROVIDER_ID), None)
    if provider is None or str(provider.get("title", "")).strip().casefold() != "printify choice":
        raise ValueError("Printify Choice is not an available provider for Comfort Colors 1717")
    if "decoration_methods" in provider and "dtg" not in provider["decoration_methods"]:
        raise ValueError("Printify Choice does not offer DTG for this blueprint")


async def setup_comfort_colors(
    costs: ReviewedCosts | None = None, *, activate: bool = False, settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if activate and costs is None:
        raise ValueError("Activation requires --costs-file with reviewed production costs")
    if not settings.printify_api_token.get_secret_value():
        raise ValueError("Printify API token is required for catalog preflight")
    with session_scope() as session:
        repository = ConfigurationRepository(session)
        record = repository.get_template_record()
        version = record.version
        current = ProductTemplate.model_validate(record.data)
    printify = PrintifyClient(settings)
    try:
        shops, blueprint, providers, catalog = await asyncio.gather(
            printify.shops(), printify.blueprint(BLUEPRINT_ID),
            printify.print_providers(BLUEPRINT_ID), printify.variants(BLUEPRINT_ID, PROVIDER_ID),
        )
        verify_connections(current, settings, shops, blueprint, providers)
        selected, width, height = catalog_selection(catalog)
        today = datetime.now(UTC).date()
        preview: dict[str, Any] = {
            "status": "preview", "current_template_version": version,
            "blueprint_id": BLUEPRINT_ID, "print_provider_id": PROVIDER_ID,
            "colors": list(COLORS), "sizes": list(SIZES),
            "print_width": width, "print_height": height,
            "variant_count": len(selected),
            "variants": [
                {"variant_id": row["id"], "color": row["options"]["color"], "size": row["options"]["size"]}
                for row in selected
            ],
            "costs_source": "Operator-reviewed Printify production costs in USD cents",
            "swatch_note": "Garment swatches approximate colors for artwork contrast previews.",
        }
        if costs is None:
            preview["activation_ready"] = False
            preview["costs_file_template"] = {
                "currency": "USD", "reviewed_at": today.isoformat(),
                "costs_by_variant": {str(row["id"]): None for row in selected},
            }
            return preview
        template = build_template(catalog, current, costs, verified_at=today)
        # Repeating an already-active setup must not create a version only because
        # the catalog identity was verified again on a later date.
        if current.garment_facts and template.garment_facts:
            previous = current.garment_facts.model_dump(exclude={"verified_at"})
            if previous == template.garment_facts.model_dump(exclude={"verified_at"}):
                template.garment_facts = template.garment_facts.model_copy(
                    update={"verified_at": current.garment_facts.verified_at}
                )
        preview["activation_ready"] = True
        preview["template"] = template.model_dump(mode="json")
        preview["retail_prices"] = [
            quote_price(
                channel=channel.channel, variant_id=variant.variant_id,
                production_cost_cents=variant.production_cost_cents,
                percent_fee=channel.percent_fee, fixed_fee_cents=channel.fixed_fee_cents,
                target_margin=settings.target_margin,
            ).model_dump(mode="json")
            for channel in template.channels if channel.enabled for variant in template.variants
        ]
        if not activate:
            return preview
        latest_catalog = await printify.variants(BLUEPRINT_ID, PROVIDER_ID)
        rechecked = build_template(latest_catalog, current, costs, verified_at=today)
        rechecked.garment_facts = template.garment_facts
        if rechecked != template:
            raise ValueError("Printify catalog changed during preflight; review a fresh preview")
        with session_scope() as session:
            saved = ConfigurationRepository(session).activate_template(template, expected_version=version)
            changed = saved.version != version
            if changed:
                RunRepository(session).audit(None, "operator", "template.comfort_colors.activated", {
                    "version": saved.version, "blueprint_id": BLUEPRINT_ID,
                    "print_provider_id": PROVIDER_ID, "costs_reviewed_at": costs.reviewed_at.isoformat(),
                })
            preview["status"] = "activated" if changed else "already_active"
            preview["active_template_version"] = saved.version
        return preview
    finally:
        await printify.close()


def main() -> None:
    """Module entry point provides the same options as the operator command."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--costs-file", type=Path)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    try:
        costs = read_reviewed_costs(args.costs_file) if args.costs_file else None
        result = asyncio.run(setup_comfort_colors(costs, activate=args.activate))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
