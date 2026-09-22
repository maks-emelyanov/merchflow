"""Generic Etsy inventory payloads and deterministic option-axis limits."""

from __future__ import annotations

from merch.schemas import CatalogVariant, PriceDecision


def inventory_product_limit(axis_count: int) -> int:
    if axis_count < 1:
        return 1
    if axis_count == 1:
        return 70
    if axis_count == 2:
        return 4900
    if axis_count == 3:
        # Etsy's stricter limit applies when price, quantity, SKU, or readiness
        # differ across all three variation properties, as they do here.
        return 400
    raise ValueError("Etsy supports at most three variation axes")


def build_generic_etsy_inventory(
    variants: list[CatalogVariant],
    prices: list[PriceDecision],
    property_ids: dict[str, int],
    *,
    quantity: int,
    readiness_state_id: int,
    sku_by_variant: dict[int, str] | None = None,
) -> dict[str, object]:
    axes = sorted({axis for variant in variants for axis in variant.options})
    missing = [axis for axis in axes if axis not in property_ids]
    if missing:
        raise ValueError(f"Etsy taxonomy lacks variation properties for: {', '.join(missing)}")
    if len(variants) > inventory_product_limit(len(axes)):
        raise ValueError("selected variants exceed Etsy's current inventory limit")
    price_by_variant = {item.variant_id: item for item in prices}
    products = []
    for variant in variants:
        decision = price_by_variant.get(variant.variant_id)
        if decision is None:
            raise ValueError(f"variant {variant.variant_id} has no price decision")
        products.append(
            {
                "sku": (sku_by_variant or {}).get(variant.variant_id, str(variant.variant_id)),
                "property_values": [
                    {
                        "property_id": property_ids[axis],
                        "property_name": axis,
                        "values": [variant.options[axis]],
                        "value_ids": [],
                    }
                    for axis in axes
                ],
                "offerings": [
                    {
                        "price": f"{decision.item_price_cents / 100:.2f}",
                        "quantity": quantity,
                        "is_enabled": True,
                        "readiness_state_id": readiness_state_id,
                    }
                ],
            }
        )
    dependent = [property_ids[axis] for axis in axes]
    return {
        "products": products,
        "price_on_property": dependent if len({p.item_price_cents for p in prices}) > 1 else [],
        "quantity_on_property": dependent,
        "sku_on_property": dependent,
        "readiness_state_on_property": dependent,
    }


def verify_generic_etsy_inventory(
    inventory: dict[str, object],
    variants: list[CatalogVariant],
    prices: list[PriceDecision],
    sku_by_variant: dict[int, str] | None = None,
) -> None:
    expected = {
        (sku_by_variant or {}).get(
            variant.variant_id, str(variant.variant_id)
        ): price.item_price_cents
        for variant in variants
        for price in prices
        if price.variant_id == variant.variant_id
    }
    actual: dict[str, int] = {}
    products = inventory.get("products")
    if not isinstance(products, list):
        raise ValueError("Etsy inventory has no products array")
    for raw in products:
        if not isinstance(raw, dict) or raw.get("is_deleted"):
            continue
        sku = str(raw.get("sku") or "")
        offerings = [
            value
            for value in raw.get("offerings", [])
            if isinstance(value, dict)
            and value.get("is_enabled") is not False
            and not value.get("is_deleted")
        ]
        if not sku or len(offerings) != 1:
            raise ValueError("Etsy inventory lacks one enabled offering and SKU per variant")
        price = offerings[0].get("price")
        if isinstance(price, dict):
            amount = int(price.get("amount", 0))
            divisor = int(price.get("divisor", 100))
            cents = round(amount * 100 / divisor)
        else:
            cents = round(float(str(price)) * 100)
        if int(offerings[0].get("quantity") or 0) < 1:
            raise ValueError("Etsy enabled variant has no inventory")
        actual[sku] = cents
    if actual != expected:
        raise ValueError("Etsy inventory identity, variants, SKUs, or prices differ")
