"""Resolve Etsy taxonomy and reusable shop profiles for catalog products."""

from __future__ import annotations

import re
from typing import Any

from merch.config import Settings
from merch.schemas import CatalogProduct, Channel, EtsyProductProfile, ProductTemplate
from merch.services.storefront import EtsyStorefrontClient

CUSTOM_VARIATION_IDS = (513, 514, 516)


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _flatten_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    queue = list(nodes)
    while queue:
        node = queue.pop(0)
        result.append(node)
        children = node.get("children")
        if isinstance(children, list):
            queue.extend(item for item in children if isinstance(item, dict))
    return result


def _variation_properties(axes: list[str], properties: list[dict[str, Any]]) -> dict[str, int]:
    supported = [item for item in properties if item.get("supports_variations")]
    assigned: dict[str, int] = {}
    used: set[int] = set()
    for axis in axes:
        axis_tokens = _tokens(axis)
        best = max(
            supported,
            key=lambda item: len(
                axis_tokens & _tokens(str(item.get("display_name") or item.get("name") or ""))
            ),
            default=None,
        )
        if best is not None:
            property_id = int(best.get("property_id") or 0)
            overlap = axis_tokens & _tokens(str(best.get("display_name") or best.get("name") or ""))
            if property_id > 0 and overlap and property_id not in used:
                assigned[axis] = property_id
                used.add(property_id)
    custom_ids = iter(value for value in CUSTOM_VARIATION_IDS if value not in used)
    for axis in axes:
        if axis not in assigned:
            assigned[axis] = next(custom_ids)
    return assigned


def _etsy_defaults(template: ProductTemplate) -> Any:
    channel = next(
        (item for item in template.channels if item.channel == Channel.ETSY and item.enabled),
        None,
    )
    return channel.etsy_listing_defaults if channel is not None else None


def _money_cents(value: Any) -> int | None:
    if isinstance(value, dict):
        amount = value.get("amount")
        divisor = value.get("divisor")
        if isinstance(amount, int) and isinstance(divisor, int) and divisor > 0:
            return round(amount * 100 / divisor)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value) * 100)
    return None


def _us_primary_shipping_cents(profile: dict[str, Any]) -> int | None:
    destinations = profile.get("shipping_profile_destinations") or profile.get("destinations")
    if not isinstance(destinations, list):
        return None
    for destination in destinations:
        if not isinstance(destination, dict):
            continue
        country = str(
            destination.get("destination_country_iso")
            or destination.get("destination_country")
            or ""
        ).upper()
        if country in {"US", "USA"}:
            return _money_cents(destination.get("primary_cost"))
    return None


async def resolve_etsy_profile(
    product: CatalogProduct,
    variants_axes: list[str],
    template: ProductTemplate,
    settings: Settings,
) -> EtsyProductProfile:
    defaults = _etsy_defaults(template)
    if settings.provider_mode == "fake":
        return EtsyProductProfile(
            taxonomy_id=defaults.taxonomy_id if defaults else 1,
            shipping_profile_id=defaults.shipping_profile_id if defaults else 1,
            return_policy_id=defaults.return_policy_id if defaults else 1,
            readiness_state_id=defaults.readiness_state_id if defaults else 1,
            production_partner_ids=(defaults.production_partner_ids if defaults else [1]),
            max_variations_supported=settings.etsy_max_variations_supported,
            variation_property_ids={
                axis: CUSTOM_VARIATION_IDS[index] for index, axis in enumerate(variants_axes)
            },
            quantity=defaults.quantity if defaults else 999,
            customer_shipping_cents=settings.etsy_customer_shipping_cents,
        )
    if defaults is None:
        raise RuntimeError(
            "Etsy shipping, return, readiness, and production-partner profiles must be "
            "imported before catalog publication"
        )
    client = EtsyStorefrontClient(settings)
    try:
        nodes = _flatten_nodes(await client.seller_taxonomy_nodes())
        product_tokens = _tokens(" ".join([product.title, product.description, *product.tags]))
        ranked = sorted(
            nodes,
            key=lambda item: (
                -len(product_tokens & _tokens(str(item.get("name") or ""))),
                -int(item.get("level") or 0),
                int(item.get("id") or 0),
            ),
        )
        node = next(
            (item for item in ranked if product_tokens & _tokens(str(item.get("name") or ""))),
            None,
        )
        if node is None:
            raise RuntimeError("No Etsy taxonomy node matches the selected Printify product")
        taxonomy_id = int(node["id"])
        properties = await client.taxonomy_properties(taxonomy_id)
        variation_property_ids = _variation_properties(variants_axes, properties)
        shipping_profile = await client.shipping_profile(defaults.shipping_profile_id)
        if int(shipping_profile.get("shipping_profile_id") or 0) != defaults.shipping_profile_id:
            raise RuntimeError("Etsy returned a different shipping profile identity")
        customer_shipping_cents = _us_primary_shipping_cents(shipping_profile)
        if customer_shipping_cents is None:
            raise RuntimeError("Etsy shipping profile has no complete US primary shipping price")
        if customer_shipping_cents != settings.etsy_customer_shipping_cents:
            raise RuntimeError(
                "Configured Etsy customer shipping does not match the reused shipping profile"
            )
        return_policy = await client.return_policy(defaults.return_policy_id)
        if int(return_policy.get("return_policy_id") or 0) != defaults.return_policy_id:
            raise RuntimeError("Etsy returned a different return policy identity")
        readiness_id = defaults.readiness_state_id
        states = await client.readiness_states()
        if not any(int(item.get("readiness_state_id") or 0) == readiness_id for item in states):
            created = await client.create_readiness_state()
            readiness_id = int(created["readiness_state_id"])
        return EtsyProductProfile(
            taxonomy_id=taxonomy_id,
            shipping_profile_id=defaults.shipping_profile_id,
            return_policy_id=defaults.return_policy_id,
            readiness_state_id=readiness_id,
            production_partner_ids=defaults.production_partner_ids,
            max_variations_supported=settings.etsy_max_variations_supported,
            variation_property_ids=variation_property_ids,
            quantity=defaults.quantity,
            customer_shipping_cents=customer_shipping_cents,
        )
    finally:
        await client.close()
