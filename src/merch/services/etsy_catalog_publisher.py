"""Checkpointed direct Etsy publication for generic Printify catalog products."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from merch.domain.etsy_inventory import (
    build_generic_etsy_inventory,
    verify_generic_etsy_inventory,
)
from merch.schemas import MarketplaceListing, PriceDecision, ProductPlanV2
from merch.services.printify import PrintifyClient
from merch.services.storefront import (
    EtsyStorefrontClient,
    StorefrontVerificationError,
    download_mockup,
    verify_etsy_listing,
    verify_etsy_variation_images,
)


async def _exact_title_listings(etsy: EtsyStorefrontClient, title: str) -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for state in ("active", "draft", "inactive", "sold_out", "expired"):
        for listing in await etsy.shop_listings(state):
            if listing.get("title") == title:
                listing_id = int(listing["listing_id"])
                found[listing_id] = await etsy.listing(listing_id)
    return list(found.values())


def _sku_map(product: dict[str, Any], plan: ProductPlanV2) -> dict[int, str]:
    variants = {
        int(item["id"]): str(item.get("sku") or "")
        for item in product.get("variants", [])
        if item.get("id") is not None
    }
    expected = {item.variant_id for item in plan.variants}
    if set(variants) & expected != expected:
        raise StorefrontVerificationError("Printify product lacks planned variants")
    selected = {variant_id: variants[variant_id] for variant_id in expected}
    if any(not sku for sku in selected.values()) or len(set(selected.values())) != len(selected):
        raise StorefrontVerificationError("Printify catalog SKUs must be present and unique")
    return selected


def _gallery_sources(product: dict[str, Any], plan: ProductPlanV2) -> list[dict[str, Any]]:
    planned = set(plan.gallery_variant_ids)
    sources = [
        item
        for item in product.get("images", [])
        if item.get("src")
        and (
            not item.get("variant_ids")
            or planned & {int(value) for value in item.get("variant_ids", [])}
        )
    ]
    unique: dict[str, dict[str, Any]] = {}
    for item in sources:
        unique.setdefault(str(item["src"]), item)
    required_positions = {
        surface.position
        for variant in plan.variants
        for surface in variant.surfaces
        if surface.required
    }
    available_positions = {
        str(item.get("position")) for item in unique.values() if item.get("position")
    }
    if len(required_positions) > 1 and not available_positions:
        raise StorefrontVerificationError(
            "Printify gallery does not identify images for required print surfaces"
        )
    if available_positions and not required_positions.issubset(available_positions):
        raise StorefrontVerificationError("Printify gallery omits a required print surface")
    if not unique:
        raise StorefrontVerificationError("Printify did not generate catalog mockups")
    return list(unique.values())[:20]


async def publish_direct_catalog_etsy(
    *,
    etsy: EtsyStorefrontClient,
    printify: PrintifyClient,
    shop_id: str,
    product_id: str,
    product: dict[str, Any],
    plan: ProductPlanV2,
    listing: MarketplaceListing,
    prices: list[PriceDecision],
    progress: dict[str, Any],
    checkpoint: Callable[..., None],
    mockup_gate: Callable[[bytes], Awaitable[dict[str, Any]]] | None = None,
    served_image_gate: Callable[
        [list[dict[str, Any]], list[dict[str, Any]]], Awaitable[dict[str, Any]]
    ]
    | None = None,
) -> tuple[dict[str, Any], int, dict[str, Any]]:
    sku_by_variant = _sku_map(product, plan)
    inventory_payload = build_generic_etsy_inventory(
        plan.variants,
        prices,
        plan.etsy_profile.variation_property_ids,
        quantity=plan.etsy_profile.quantity,
        readiness_state_id=plan.etsy_profile.readiness_state_id,
        sku_by_variant=sku_by_variant,
    )
    listing_id = int(progress.get("etsy_listing_id") or 0)
    owned = bool(progress.get("etsy_listing_owned"))
    if listing_id:
        remote_listing = await etsy.listing(listing_id)
        if remote_listing.get("title") != listing.title or int(
            remote_listing.get("shop_id") or 0
        ) != int(etsy.settings.etsy_shop_id or 0):
            raise StorefrontVerificationError("Saved Etsy catalog listing identity changed")
    else:
        candidates = await _exact_title_listings(etsy, listing.title)
        if len(candidates) > 1:
            raise StorefrontVerificationError("Multiple Etsy listings have the approved title")
        if candidates:
            candidate = candidates[0]
            if (
                candidate.get("description") != listing.long_description
                or int(candidate.get("taxonomy_id") or 0) != plan.etsy_profile.taxonomy_id
            ):
                raise StorefrontVerificationError(
                    "Existing Etsy listing with this title is not the approved package"
                )
            listing_id = int(candidate["listing_id"])
        elif progress.get("draft_create_started"):
            raise StorefrontVerificationError(
                "Etsy catalog draft creation outcome is unknown; refusing a duplicate"
            )
        else:
            checkpoint(stage="creating_etsy_draft", draft_create_started=True)
            created = await etsy.create_draft(
                {
                    "quantity": str(plan.etsy_profile.quantity),
                    "title": listing.title,
                    "description": listing.long_description,
                    "price": f"{min(item.item_price_cents for item in prices) / 100:.2f}",
                    "who_made": "someone_else",
                    "when_made": "made_to_order",
                    "taxonomy_id": str(plan.etsy_profile.taxonomy_id),
                    "shipping_profile_id": str(plan.etsy_profile.shipping_profile_id),
                    "return_policy_id": str(plan.etsy_profile.return_policy_id),
                    "readiness_state_id": str(plan.etsy_profile.readiness_state_id),
                    "production_partner_ids": ",".join(
                        str(value) for value in plan.etsy_profile.production_partner_ids
                    ),
                }
            )
            listing_id = int(created["listing_id"])
            owned = True
        checkpoint(
            stage="etsy_draft_created",
            etsy_listing_id=listing_id,
            etsy_listing_owned=owned,
        )

    remote_listing = await etsy.listing(listing_id)
    if sorted(remote_listing.get("tags") or []) != sorted(listing.tags):
        await etsy.update_listing(listing_id, {"tags": ",".join(listing.tags)})
    inventory = await etsy.inventory(listing_id)
    try:
        verify_generic_etsy_inventory(
            inventory, plan.variants, prices, sku_by_variant=sku_by_variant
        )
    except ValueError, StorefrontVerificationError:
        checkpoint(stage="writing_etsy_inventory")
        await etsy.update_inventory(
            listing_id,
            inventory_payload,
            max_variations_supported=plan.etsy_profile.max_variations_supported,
        )
        inventory = await etsy.inventory(listing_id)
        verify_generic_etsy_inventory(
            inventory, plan.variants, prices, sku_by_variant=sku_by_variant
        )
    checkpoint(stage="etsy_inventory_verified")

    gallery = _gallery_sources(product, plan)
    image_ids: list[int] = [int(value) for value in progress.get("etsy_image_ids", [])]
    mockup_originality: list[dict[str, Any]] = list(progress.get("mockup_originality_reports", []))
    for rank, mockup in enumerate(gallery, start=1):
        prepared: tuple[bytes, str] | None = None
        if mockup_gate is not None and rank <= 3 and rank > len(mockup_originality):
            prepared = await download_mockup(str(mockup["src"]))
            mockup_originality.append(await mockup_gate(prepared[0]))
            checkpoint(
                stage="final_mockup_originality_verified",
                mockup_originality_reports=mockup_originality.copy(),
            )
        existing_images = await etsy.images(listing_id)
        if rank <= len(image_ids) and any(
            int(item.get("listing_image_id") or 0) == image_ids[rank - 1]
            for item in existing_images
        ):
            continue
        marker = f"catalog mockup {rank} for {listing.alt_text}"[:500]
        matching = [item for item in existing_images if item.get("alt_text") == marker]
        if len(matching) > 1:
            raise StorefrontVerificationError("Etsy catalog gallery contains duplicate images")
        if matching:
            image_id = int(matching[0]["listing_image_id"])
        else:
            if int(progress.get("image_upload_started_rank") or 0) == rank:
                raise StorefrontVerificationError(
                    "Etsy catalog image upload outcome is unknown; refusing a duplicate"
                )
            checkpoint(stage="uploading_etsy_gallery", image_upload_started_rank=rank)
            image, content_type = prepared or await download_mockup(str(mockup["src"]))
            image_id = await etsy.upload_mockup(listing_id, image, content_type, marker, rank)
        if rank > len(image_ids):
            image_ids.append(image_id)
        else:
            image_ids[rank - 1] = image_id
        checkpoint(
            stage="uploading_etsy_gallery",
            etsy_image_ids=image_ids.copy(),
            image_upload_started_rank=None,
        )
    if not image_ids:
        raise StorefrontVerificationError("Etsy catalog gallery has no verified images")

    axes = sorted({axis for variant in plan.variants for axis in variant.options})
    meaningful = [
        axis
        for axis in axes
        if any(
            token in axis.casefold()
            for token in ("color", "device", "finish", "style", "material", "size")
        )
        and len({variant.options.get(axis) for variant in plan.variants}) > 1
    ]
    if meaningful:
        axis = meaningful[0]
        property_id = plan.etsy_profile.variation_property_ids[axis]
        value_ids: dict[str, int] = {}
        inventory = await etsy.inventory(listing_id)
        for item in inventory.get("products", []):
            for prop in item.get("property_values", []):
                values = prop.get("values") or []
                ids = prop.get("value_ids") or []
                if (
                    int(prop.get("property_id") or 0) == property_id
                    and len(values) == len(ids) == 1
                ):
                    value_ids[str(values[0])] = int(ids[0])
        variants = {item.variant_id: item for item in plan.variants}
        value_images: dict[str, int] = {}
        for mockup, image_id in zip(gallery, image_ids, strict=True):
            for raw_variant_id in mockup.get("variant_ids", []):
                variant = variants.get(int(raw_variant_id))
                if variant is not None and axis in variant.options:
                    value_images.setdefault(variant.options[axis], image_id)
                    break
        expected_values = {item.options[axis] for item in plan.variants}
        if set(value_ids) != expected_values or set(value_images) != expected_values:
            raise StorefrontVerificationError(
                f"Etsy gallery cannot link every visually meaningful {axis} option"
            )
        expected_links = [
            {
                "property_id": property_id,
                "value_id": value_ids[value],
                "image_id": value_images[value],
            }
            for value in sorted(expected_values)
        ]
        try:
            verify_etsy_variation_images(await etsy.variation_images(listing_id), expected_links)
        except StorefrontVerificationError:
            await etsy.update_variation_images(listing_id, expected_links)
            verify_etsy_variation_images(await etsy.variation_images(listing_id), expected_links)
        checkpoint(stage="etsy_variation_images_verified", variation_axis=axis)

    image_evidence: dict[str, Any] | None = None
    if served_image_gate is not None:
        image_evidence = await served_image_gate(gallery, await etsy.images(listing_id))
        checkpoint(stage="etsy_draft_gallery_verified", image=image_evidence)

    if remote_listing.get("state") != "active":
        checkpoint(stage="activating_etsy_listing")
        await etsy.update_listing(listing_id, {"state": "active"})
    verify_etsy_listing(
        await etsy.listing(listing_id),
        listing_id,
        int(etsy.settings.etsy_shop_id or 0),
        listing.title,
    )
    checkpoint(stage="etsy_listing_active")
    handle = f"https://www.etsy.com/listing/{listing_id}"
    try:
        await printify.publishing_succeeded(shop_id, product_id, listing_id, handle)
    except Exception:
        if owned:
            await etsy.update_listing(listing_id, {"state": "inactive"})
        raise
    linked = await printify.product(shop_id, product_id)
    if int((linked.get("external") or {}).get("id") or 0) != listing_id:
        raise StorefrontVerificationError("Printify did not retain the Etsy catalog link")
    verification = {
        "listing_id": listing_id,
        "gallery_image_ids": image_ids,
        "gallery_sources": [str(item["src"]) for item in gallery],
        "gallery_count": len(image_ids),
        "mockup_originality_reports": mockup_originality,
        "image": image_evidence,
        "publication_mode": "direct_etsy_catalog",
    }
    checkpoint(stage="etsy_catalog_verified", verification=verification)
    return linked, listing_id, verification
