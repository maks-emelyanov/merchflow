"""Resumable direct Etsy publication for a failed Printify storefront handoff."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from merch.schemas import EtsyListingDefaults, MarketplaceListing, PriceQuote, ProductTemplate
from merch.services.mockup_selection import mockup_plan as mockup_plan
from merch.services.mockup_verification import (
    PreparedMockup,
    download_etsy_image,
    prepare_mockups,
    verify_etsy_mockups,
)
from merch.services.printify import PrintifyClient
from merch.services.storefront import (
    EtsyStorefrontClient,
    StorefrontVerificationError,
    download_mockup,
    normalize_etsy_inventory_dependencies,
    selector_labels_are_exact,
    verify_etsy_inventory,
    verify_etsy_listing,
    verify_etsy_variation_images,
)


def direct_inventory(
    product: dict[str, Any], template: ProductTemplate, quotes: list[PriceQuote],
    defaults: EtsyListingDefaults,
) -> dict[str, Any]:
    prices = {item.variant_id: item.retail_price_cents for item in quotes}
    variants = {int(item["id"]): item for item in product.get("variants", [])}
    products: list[dict[str, Any]] = []
    skus: set[str] = set()
    for variant in template.variants:
        if not variant.enabled:
            continue
        remote = variants.get(variant.variant_id)
        if remote is None or variant.variant_id not in prices:
            raise StorefrontVerificationError("Printify variant is missing an approved Etsy price")
        sku = str(remote.get("sku") or "")
        if not sku or sku in skus:
            raise StorefrontVerificationError("Printify variant SKUs must be present and unique")
        skus.add(sku)
        products.append({
            "sku": sku,
            "offerings": [{
                "price": prices[variant.variant_id] / 100,
                "quantity": defaults.quantity,
                "is_enabled": True,
                "readiness_state_id": defaults.readiness_state_id,
            }],
            "property_values": [
                {"property_id": 513, "property_name": "Size", "value_ids": [],
                 "values": [variant.size], "scale_id": None},
                {"property_id": 514, "property_name": "Color", "value_ids": [],
                 "values": [variant.color], "scale_id": None},
            ],
        })
    if len(products) > 100:
        raise StorefrontVerificationError("Etsy supports at most 100 variants for this product")
    enabled_prices = {
        prices[variant.variant_id] for variant in template.variants if variant.enabled
    }
    # SKUs depend on both variations, so Etsy requires each nonempty property
    # dependency to include both, even when prices vary only by size or color.
    price_properties = [513, 514] if len(enabled_prices) > 1 else []
    return normalize_etsy_inventory_dependencies({
        "products": products,
        "price_on_property": price_properties,
        "quantity_on_property": [],
        "sku_on_property": [513, 514],
        "readiness_state_on_property": [],
    })


async def _exact_title_listings(
    etsy: EtsyStorefrontClient, title: str
) -> list[dict[str, Any]]:
    candidates: dict[int, dict[str, Any]] = {}
    for state in ("active", "draft", "inactive", "sold_out", "expired"):
        for item in await etsy.shop_listings(state):
            if item.get("title") == title:
                listing_id = int(item["listing_id"])
                candidates[listing_id] = await etsy.listing(listing_id)
    return list(candidates.values())


def _expected_skus(payload: dict[str, Any]) -> set[str]:
    return {str(item["sku"]) for item in payload["products"]}


async def _find_reusable_listing(
    etsy: EtsyStorefrontClient, listing: MarketplaceListing,
    defaults: EtsyListingDefaults, inventory_payload: dict[str, Any],
    draft_started: bool,
) -> tuple[int, bool] | None:
    candidates = await _exact_title_listings(etsy, listing.title)
    if not candidates:
        if draft_started:
            raise StorefrontVerificationError(
                "Etsy draft creation outcome is unknown; do not create a second listing"
            )
        return None
    if len(candidates) != 1:
        raise StorefrontVerificationError("Multiple Etsy listings have the approved title")
    candidate = candidates[0]
    listing_id = int(candidate["listing_id"])
    if (
        candidate.get("description") != listing.long_description
        or int(candidate.get("taxonomy_id") or 0) != defaults.taxonomy_id
    ):
        raise StorefrontVerificationError("Existing Etsy listing with this title is not the approved product")
    inventory = await etsy.inventory(listing_id)
    actual_skus = {str(item.get("sku") or "") for item in inventory.get("products", [])}
    expected_skus = _expected_skus(inventory_payload)
    if actual_skus == expected_skus:
        return listing_id, False
    if draft_started and candidate.get("state") == "draft" and len(actual_skus) <= 1:
        return listing_id, True
    raise StorefrontVerificationError("Existing Etsy listing has ambiguous variant inventory")


async def publish_direct_etsy(
    etsy: EtsyStorefrontClient,
    printify: PrintifyClient,
    shop_id: str,
    product_id: str,
    product: dict[str, Any],
    template: ProductTemplate,
    listing: MarketplaceListing,
    quotes: list[PriceQuote],
    defaults: EtsyListingDefaults,
    progress: dict[str, Any],
    checkpoint: Callable[..., None],
    *,
    prepared_mockups: list[PreparedMockup] | None = None,
    evidence_writer: Callable[[bytes, str], str] | None = None,
) -> tuple[dict[str, Any], int, dict[str, int]]:
    """Publish or resume a run-owned Etsy listing, then confirm its Printify link."""
    inventory_payload = direct_inventory(product, template, quotes, defaults)
    images_to_upload = prepared_mockups
    if images_to_upload is None:
        images_to_upload = await prepare_mockups(
            product, template, downloader=download_mockup,
            evidence_writer=evidence_writer, checkpoint=checkpoint,
        )
    if [(item.color, item.source) for item in images_to_upload] != mockup_plan(product, template):
        raise StorefrontVerificationError("Prepared mockups no longer match the approved product")
    listing_id = int(progress.get("etsy_listing_id") or 0)
    owned = bool(progress.get("etsy_listing_owned"))
    if listing_id:
        remote = await etsy.listing(listing_id)
        if remote.get("title") != listing.title or int(remote.get("shop_id") or 0) != int(etsy.settings.etsy_shop_id or 0):
            raise StorefrontVerificationError("Saved Etsy listing no longer matches this run")
    else:
        existing = await _find_reusable_listing(
            etsy, listing, defaults, inventory_payload,
            bool(progress.get("draft_create_started")),
        )
        if existing:
            listing_id, owned = existing
        else:
            checkpoint(stage="creating_draft", draft_create_started=True)
            progress["draft_create_started"] = True
            created = await etsy.create_draft({
                "quantity": str(defaults.quantity),
                "title": listing.title,
                "description": listing.long_description,
                "price": str(min(item.retail_price_cents for item in quotes) / 100),
                "who_made": "someone_else",
                "when_made": "made_to_order",
                "taxonomy_id": str(defaults.taxonomy_id),
                "shipping_profile_id": str(defaults.shipping_profile_id),
                "return_policy_id": str(defaults.return_policy_id),
                "readiness_state_id": str(defaults.readiness_state_id),
                "production_partner_ids": ",".join(str(value) for value in defaults.production_partner_ids),
            })
            listing_id = int(created["listing_id"])
            owned = True
        checkpoint(stage="draft_created", etsy_listing_id=listing_id, etsy_listing_owned=owned)
        progress.update(etsy_listing_id=listing_id, etsy_listing_owned=owned)

    remote = await etsy.listing(listing_id)
    if remote.get("state") == "active":
        verify_etsy_listing(
            remote, listing_id, int(etsy.settings.etsy_shop_id or 0), listing.title
        )
        inventory = await etsy.inventory(listing_id)
        verify_etsy_inventory(inventory, product, template, quotes)
        if not selector_labels_are_exact(inventory):
            raise StorefrontVerificationError("Existing Etsy selectors differ from Size and Color")
        verified = await verify_etsy_mockups(
            etsy, listing_id, template, images_to_upload, inventory,
            downloader=download_etsy_image, evidence_writer=evidence_writer,
            checkpoint=checkpoint,
        )
        linked = await _link_printify_listing(
            etsy, printify, shop_id, product_id, listing_id, owned, checkpoint
        )
        await verify_etsy_mockups(
            etsy, listing_id, template, images_to_upload, await etsy.inventory(listing_id),
            image_ids=verified["image_ids"], downloader=download_etsy_image,
            evidence_writer=evidence_writer, checkpoint=checkpoint,
        )
        checkpoint(stage="active_listing_verified" if owned else "adopted_existing_listing")
        return linked, verified["featured_image_id"], verified["image_ids"]

    if sorted(remote.get("tags") or []) != sorted(listing.tags):
        await etsy.update_listing(listing_id, {"tags": ",".join(listing.tags)})
    inventory = await etsy.inventory(listing_id)
    try:
        verify_etsy_inventory(inventory, product, template, quotes)
        if not selector_labels_are_exact(inventory):
            raise StorefrontVerificationError("Etsy selectors need updating")
    except StorefrontVerificationError:
        checkpoint(stage="writing_inventory")
        await etsy.update_inventory(listing_id, inventory_payload)
        inventory = await etsy.inventory(listing_id)
        verify_etsy_inventory(inventory, product, template, quotes)
        if not selector_labels_are_exact(inventory):
            raise StorefrontVerificationError("Etsy did not save Size and Color selectors") from None
    checkpoint(stage="inventory_verified")

    image_ids: dict[str, int] = {
        str(color): int(image_id)
        for color, image_id in (progress.get("etsy_color_image_ids") or {}).items()
    }
    for rank, mockup in enumerate(images_to_upload, start=1):
        color = mockup.color
        images = await etsy.images(listing_id)
        saved = image_ids.get(color)
        if saved and any(int(item.get("listing_image_id") or 0) == saved for item in images):
            continue
        alt_text = f"{listing.alt_text} on {color} shirt"[:500]
        existing_images = [
            item for item in images if item.get("alt_text") == alt_text
        ]
        if len(existing_images) > 1:
            raise StorefrontVerificationError(f"Etsy has duplicate {color} mockups")
        if existing_images:
            image_id = int(existing_images[0]["listing_image_id"])
        else:
            if progress.get("image_upload_started_color") == color:
                raise StorefrontVerificationError(
                    f"Etsy {color} image upload outcome is unknown; do not upload a duplicate"
                )
            checkpoint(stage="uploading_images", image_upload_started_color=color)
            image_id = await etsy.upload_mockup(
                listing_id, mockup.image, mockup.content_type, alt_text, rank,
            )
        image_ids[color] = image_id
        checkpoint(
            stage="uploading_images", etsy_color_image_ids=image_ids.copy(),
            image_upload_started_color=None,
        )
    featured_color = images_to_upload[0].color
    featured_id = image_ids[featured_color]

    color_value_ids: dict[str, int] = {}
    for item in inventory.get("products", []):
        for prop in item.get("property_values", []):
            if int(prop.get("property_id") or 0) == 514:
                values, ids = prop.get("values") or [], prop.get("value_ids") or []
                if len(values) == 1 and len(ids) == 1:
                    color_value_ids[str(values[0])] = int(ids[0])
    if set(color_value_ids) != set(image_ids):
        raise StorefrontVerificationError("Etsy color values do not match gallery colors")
    expected_links = [
        {"property_id": 514, "value_id": color_value_ids[color], "image_id": image_ids[color]}
        for color in image_ids
    ]
    current_links = await etsy.variation_images(listing_id)
    try:
        verify_etsy_variation_images(current_links, expected_links)
    except StorefrontVerificationError:
        await etsy.update_variation_images(listing_id, expected_links)
        verify_etsy_variation_images(await etsy.variation_images(listing_id), expected_links)
    await verify_etsy_mockups(
        etsy, listing_id, template, images_to_upload, inventory,
        image_ids=image_ids, downloader=download_etsy_image,
        evidence_writer=evidence_writer, checkpoint=checkpoint,
    )
    checkpoint(stage="images_verified", featured_image_id=featured_id)
    checkpoint(stage="draft_verified")

    try:
        remote = await etsy.listing(listing_id)
        if remote.get("state") != "active":
            checkpoint(stage="activating_listing")
            await etsy.update_listing(listing_id, {"state": "active"})
        verify_etsy_listing(
            await etsy.listing(listing_id), listing_id,
            int(etsy.settings.etsy_shop_id or 0), listing.title,
        )
        checkpoint(stage="listing_active")
    except Exception as exc:
        if owned:
            try:
                await etsy.update_listing(listing_id, {"state": "inactive"})
                checkpoint(stage="activation_unverified_listing_inactive")
            except Exception:
                pass
        raise StorefrontVerificationError(f"Etsy activation could not be confirmed: {exc}") from exc

    linked = await _link_printify_listing(
        etsy, printify, shop_id, product_id, listing_id, owned, checkpoint
    )
    await verify_etsy_mockups(
        etsy, listing_id, template, images_to_upload, await etsy.inventory(listing_id),
        image_ids=image_ids, downloader=download_etsy_image,
        evidence_writer=evidence_writer, checkpoint=checkpoint,
    )
    matching = await _exact_title_listings(etsy, listing.title)
    if len([item for item in matching if item.get("state") == "active"]) != 1:
        raise StorefrontVerificationError("Etsy has an ambiguous number of active approved listings")
    return linked, featured_id, image_ids


async def _link_printify_listing(
    etsy: EtsyStorefrontClient, printify: PrintifyClient, shop_id: str,
    product_id: str, listing_id: int, owned: bool, checkpoint: Callable[..., None],
) -> dict[str, Any]:
    try:
        linked = await printify.product(shop_id, product_id)
    except Exception as exc:
        if owned:
            await etsy.update_listing(listing_id, {"state": "inactive"})
            checkpoint(stage="link_unverified_listing_inactive")
        raise StorefrontVerificationError(f"Printify link could not be read: {exc}") from exc
    external = linked.get("external") or {}
    if external.get("id") and str(external["id"]) != str(listing_id):
        if owned:
            await etsy.update_listing(listing_id, {"state": "inactive"})
            checkpoint(stage="link_mismatch_listing_inactive")
        raise StorefrontVerificationError("Printify is linked to a different Etsy listing")
    if not external.get("id"):
        try:
            await printify.publish(shop_id, product_id)
            await printify.publishing_succeeded(
                shop_id, product_id, listing_id,
                str((await etsy.listing(listing_id)).get("url") or f"https://www.etsy.com/listing/{listing_id}"),
            )
            linked = await printify.product(shop_id, product_id)
            if str((linked.get("external") or {}).get("id") or "") != str(listing_id):
                raise StorefrontVerificationError("Printify did not save the Etsy listing link")
        except Exception as exc:
            try:
                linked = await printify.product(shop_id, product_id)
            except Exception:
                linked = {}
            if str((linked.get("external") or {}).get("id") or "") != str(listing_id):
                if owned:
                    await etsy.update_listing(listing_id, {"state": "inactive"})
                    checkpoint(stage="link_failed_listing_inactive")
                raise StorefrontVerificationError(f"Printify link could not be confirmed: {exc}") from exc
    checkpoint(stage="printify_link_verified")
    return linked
