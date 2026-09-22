"""Reviewable, resumable text refresh for products that are already live."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from merch.config import Settings, get_settings
from merch.database import session_scope
from merch.domain.listing_copy import normalize_listing_copy, validate_listing_copy
from merch.models import (
    CopyRefreshBatchRecord,
    CopyRefreshItemRecord,
    ProductMappingRecord,
    PublishRecord,
    RunRecord,
)
from merch.repository import RunRepository
from merch.schemas import (
    Channel,
    CopyRefreshEdit,
    CreativeBrief,
    MarketplaceListing,
    MarketplaceListingSet,
    PriceQuote,
    ProductTemplate,
)
from merch.services.etsy_auth import etsy_access_token
from merch.services.openai_service import OpenAIService
from merch.services.printify import PrintifyClient, channel_shop
from merch.services.storefront import (
    EtsyStorefrontClient,
    StorefrontVerificationError,
    verify_etsy_inventory,
    verify_featured_image,
    verify_printify_product,
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _copy_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(value.get("title") or ""),
        "description": str(value.get("description") or value.get("long_description") or ""),
        "tags": sorted(str(tag) for tag in (value.get("tags") or [])),
    }


def _listing_fields(listing: MarketplaceListing) -> dict[str, Any]:
    return {
        "title": listing.title,
        "description": listing.long_description,
        "tags": sorted(listing.tags),
    }


def _invariants(
    product: dict[str, Any], inventory: dict[str, Any],
    images: list[dict[str, Any]], variation_images: list[dict[str, Any]],
    *, version: int = 2,
) -> dict[str, str | int]:
    enabled_variants = sorted(
        (
            int(item["id"]), str(item.get("sku") or ""), int(item.get("price") or 0),
            bool(item.get("is_default")),
        )
        for item in product.get("variants", []) if item.get("is_enabled")
    )
    etsy_images = sorted(
        (int(item.get("listing_image_id") or 0), int(item.get("rank") or 0))
        for item in images
    )
    common: dict[str, str | int] = {
        "printify_variants": _digest(enabled_variants),
        "etsy_inventory": _digest(inventory.get("products") or []),
        "etsy_images": _digest(etsy_images),
        "etsy_color_links": _digest(variation_images),
    }
    if version == 1:
        front_images = sorted(
            (
                str(item.get("mockup_id") or ""), str(item.get("src") or ""),
                tuple(sorted(int(value) for value in item.get("variant_ids") or [])),
            )
            for item in product.get("images", []) if item.get("position") == "front"
        )
        return {**common, "printify_mockups": _digest(front_images)}
    if version != 2:
        raise ValueError("unsupported copy refresh invariant version")
    print_areas = sorted(
        (
            tuple(sorted(int(value) for value in area.get("variant_ids") or [])),
            tuple(sorted(
                (
                    str(placeholder.get("position") or ""),
                    tuple(sorted(
                        (
                            str(image.get("id") or ""),
                            image.get("x"), image.get("y"),
                            image.get("scale"), image.get("angle"),
                        )
                        for image in placeholder.get("images") or []
                    )),
                )
                for placeholder in area.get("placeholders") or []
            )),
        )
        for area in product.get("print_areas") or []
    )
    return {
        **common,
        "schema_version": 2,
        "printify_print_areas": _digest(print_areas),
    }


def _batch_digest(items: list[CopyRefreshItemRecord]) -> str:
    return _digest(sorted(
        ((item.run_id, item.channel, item.after_json) for item in items),
        key=lambda value: (value[0], value[1]),
    ))


def _load_batch(batch_id: str) -> CopyRefreshBatchRecord:
    with session_scope() as session:
        batch = session.scalar(
            select(CopyRefreshBatchRecord)
            .where(CopyRefreshBatchRecord.id == batch_id)
            .options(selectinload(CopyRefreshBatchRecord.items))
        )
        if batch is None:
            raise KeyError(f"copy refresh batch {batch_id} not found")
        return batch


def batch_payload(batch: CopyRefreshBatchRecord) -> dict[str, Any]:
    return {
        "id": batch.id, "status": batch.status, "version": batch.version,
        "digest": batch.digest, "error": batch.error,
        "approved_at": batch.approved_at.isoformat() if batch.approved_at else None,
        "items": [
            {
                "id": item.id, "run_id": item.run_id, "channel": item.channel,
                "printify_product_id": item.printify_product_id,
                "marketplace_listing_id": item.marketplace_listing_id,
                "before": item.before_json, "after": item.after_json,
                "status": item.status, "stage": item.stage, "error": item.error,
                "warning": (item.generation_json or {}).get("warning"),
            }
            for item in sorted(batch.items, key=lambda value: value.run_id)
        ],
    }


def get_copy_refresh_batch(batch_id: str) -> dict[str, Any]:
    return batch_payload(_load_batch(batch_id))


def latest_copy_refresh_batch() -> dict[str, Any] | None:
    with session_scope() as session:
        batch_id = session.scalar(
            select(CopyRefreshBatchRecord.id)
            .order_by(CopyRefreshBatchRecord.created_at.desc()).limit(1)
        )
    return get_copy_refresh_batch(batch_id) if batch_id else None


def effective_approved_listing(
    run_id: str, channel: Channel, original: MarketplaceListing
) -> MarketplaceListing:
    with session_scope() as session:
        item = session.scalar(
            select(CopyRefreshItemRecord)
            .where(
                CopyRefreshItemRecord.run_id == run_id,
                CopyRefreshItemRecord.channel == channel.value,
                CopyRefreshItemRecord.status == "applied",
            )
            .order_by(CopyRefreshItemRecord.created_at.desc()).limit(1)
        )
        return MarketplaceListing.model_validate(item.after_json) if item and item.after_json else original


def _product_context(template: ProductTemplate) -> dict[str, Any]:
    variants = [item for item in template.variants if item.enabled]
    return {
        "name": template.name,
        "decoration_method": template.decoration_method,
        "print_width": template.print_width,
        "print_height": template.print_height,
        "colors": {item.color: item.color_hex for item in variants},
        "sizes": sorted({item.size for item in variants}),
        "channels": [item.channel.value for item in template.channels if item.enabled],
        "etsy_production_partner_confirmed": template.etsy_production_partner_confirmed,
        "garment_facts": (
            template.garment_facts.model_dump(mode="json") if template.garment_facts else None
        ),
    }


async def prepare_copy_refresh_batch(
    settings: Settings | None = None,
    *,
    run_id: str | None = None,
) -> str:
    """Stage one review batch for live published Etsy products without changing them."""
    settings = settings or get_settings()
    if settings.provider_mode != "live" or settings.publish_mode != "live":
        raise ValueError("published copy refresh requires live providers and publishing")
    with session_scope() as session:
        active_query = (
            select(CopyRefreshBatchRecord.id)
            .where(CopyRefreshBatchRecord.status.in_(("preparing", "pending_review", "applying", "verification_required")))
        )
        if run_id is not None:
            active_query = active_query.join(CopyRefreshItemRecord).where(
                CopyRefreshItemRecord.run_id == run_id
            )
        active = session.scalar(
            active_query.order_by(CopyRefreshBatchRecord.created_at.desc()).limit(1)
        )
        if active:
            batch_id = active
        else:
            target_query = (
                select(PublishRecord, RunRecord)
                .join(RunRecord, PublishRecord.run_id == RunRecord.id)
                .where(
                    PublishRecord.channel == Channel.ETSY.value,
                    PublishRecord.status == "succeeded",
                    RunRecord.status == "published",
                )
            )
            if run_id is not None:
                target_query = target_query.where(RunRecord.id == run_id)
            targets = list(session.execute(target_query))
            if not targets:
                suffix = f" for run {run_id}" if run_id is not None else ""
                raise ValueError(
                    f"no live published Etsy products are available for copy refresh{suffix}"
                )
            batch = CopyRefreshBatchRecord(status="preparing", version=1)
            session.add(batch)
            session.flush()
            batch_id = batch.id
            for publish, run in targets:
                mapping = session.scalar(select(ProductMappingRecord).where(
                    ProductMappingRecord.run_id == run.id,
                    ProductMappingRecord.channel == Channel.ETSY.value,
                    ProductMappingRecord.printify_product_id == publish.printify_product_id,
                ))
                listing_id = publish.external_product_id
                mapped_ids = (
                    {value for value in (mapping.marketplace_listing_id,
                                         mapping.marketplace_product_id) if value}
                    if mapping else set()
                )
                if (
                    not publish.printify_product_id or not listing_id or not mapping
                    or mapped_ids != {listing_id} or not run.template_snapshot
                ):
                    raise ValueError(f"run {run.id} has incomplete Etsy product mappings")
                template = ProductTemplate.model_validate(run.template_snapshot)
                session.add(CopyRefreshItemRecord(
                    batch_id=batch_id, run_id=run.id, channel=Channel.ETSY.value,
                    printify_product_id=publish.printify_product_id,
                    marketplace_listing_id=listing_id,
                    printify_shop_id=channel_shop(template, Channel.ETSY),
                    status="preparing",
                ))
    batch = _load_batch(batch_id)
    if batch.status != "preparing":
        return batch_id
    ai = OpenAIService(settings)
    printify = PrintifyClient(settings)
    etsy = EtsyStorefrontClient(settings, await etsy_access_token(settings))
    try:
        for item in batch.items:
            if item.status == "pending_review":
                continue
            try:
                await _prepare_item(item.id, ai, printify, etsy, settings)
            except Exception as exc:
                with session_scope() as session:
                    current = session.get(CopyRefreshItemRecord, item.id)
                    if current:
                        current.error = f"{type(exc).__name__}: {exc}"
                        current.stage = "preparation_failed"
                raise
    finally:
        await printify.close()
        await etsy.close()
    with session_scope() as session:
        current_batch = session.scalar(
            select(CopyRefreshBatchRecord)
            .where(CopyRefreshBatchRecord.id == batch_id)
            .options(selectinload(CopyRefreshBatchRecord.items))
        )
        if current_batch is None or any(item.status != "pending_review" for item in current_batch.items):
            raise RuntimeError("copy refresh preparation is incomplete")
        current_batch.digest = _batch_digest(current_batch.items)
        current_batch.status = "pending_review"
        current_batch.error = None
    return batch_id


async def _prepare_item(
    item_id: str, ai: OpenAIService, printify: PrintifyClient,
    etsy: EtsyStorefrontClient, settings: Settings,
) -> None:
    with session_scope() as session:
        item = session.get(CopyRefreshItemRecord, item_id)
        if item is None:
            raise KeyError(item_id)
        run = session.get(RunRecord, item.run_id)
        if run is None or not run.creative_brief or not run.listings or not run.template_snapshot:
            raise ValueError("published run lacks the brief or approved listing package")
        source: dict[str, Any] = {
            "run_id": run.id,
            "brief": run.creative_brief,
            "listings": run.listings,
            "research_summary": (run.research_report or {}).get("market_summary", ""),
            "template": run.publication_template_snapshot or run.template_snapshot,
            "shop_id": item.printify_shop_id,
            "product_id": item.printify_product_id,
            "listing_id": int(item.marketplace_listing_id),
            "generation": dict(item.generation_json or {}),
        }
    product = await printify.product(source["shop_id"], source["product_id"])
    listing = await etsy.listing(source["listing_id"])
    if (
        str((product.get("external") or {}).get("id") or "") != str(source["listing_id"])
        or int(listing.get("listing_id") or 0) != int(source["listing_id"])
        or int(listing.get("shop_id") or 0) != int(settings.etsy_shop_id or 0)
        or listing.get("state") != "active"
    ):
        raise StorefrontVerificationError("published Etsy and Printify identities do not match")
    with session_scope() as session:
        mapping = session.scalar(select(ProductMappingRecord).where(
            ProductMappingRecord.run_id == source["run_id"],
            ProductMappingRecord.channel == Channel.ETSY.value,
            ProductMappingRecord.printify_product_id == source["product_id"],
        ))
        if mapping is None:
            raise StorefrontVerificationError("published product mapping disappeared")
        mapping.marketplace_product_id = str(source["listing_id"])
        mapping.marketplace_listing_id = str(source["listing_id"])
    inventory = await etsy.inventory(source["listing_id"])
    images = await etsy.images(source["listing_id"])
    variation_images = await etsy.variation_images(source["listing_id"])
    baseline = _invariants(product, inventory, images, variation_images)
    template = ProductTemplate.model_validate(source["template"])
    brief = CreativeBrief.model_validate(source["brief"])
    brief = brief.model_copy(update={
        "shirt_colors": sorted({variant.color for variant in template.variants if variant.enabled})
    })
    context = _product_context(template)
    generation = source["generation"]
    if generation.get("draft"):
        draft = MarketplaceListingSet.model_validate(generation["draft"])
    else:
        generated = await ai.listings(context, brief, source["research_summary"])
        draft = normalize_listing_copy(generated.value)
        generation["draft"] = draft.model_dump(mode="json")
        with session_scope() as session:
            current = session.get(CopyRefreshItemRecord, item_id)
            if current:
                current.generation_json = dict(generation)
                RunRepository(session).provider_call(source["run_id"], "copy_refresh_listings", generated.metadata)
    if generation.get("polished"):
        polished = MarketplaceListingSet.model_validate(generation["polished"])
    else:
        try:
            result = await ai.polish_listings(context, brief, draft)
            polished = normalize_listing_copy(result.value)
            generation["polished"] = polished.model_dump(mode="json")
            with session_scope() as session:
                current = session.get(CopyRefreshItemRecord, item_id)
                if current:
                    current.generation_json = dict(generation)
                    RunRepository(session).provider_call(source["run_id"], "copy_refresh_polish", result.metadata)
        except Exception as exc:
            generation["warning"] = f"Listing polish failed: {type(exc).__name__}: {exc}"
            polished = draft
    try:
        validate_listing_copy(polished)
    except ValueError as exc:
        validate_listing_copy(draft)
        generation["warning"] = f"Polished copy failed validation: {exc}"
        polished = draft
    revised = next(value for value in polished.listings if value.channel == Channel.ETSY)
    with session_scope() as session:
        current = session.get(CopyRefreshItemRecord, item_id)
        if current is None:
            raise KeyError(item_id)
        current.before_json = {"printify": _copy_fields(product), "etsy": _copy_fields(listing)}
        current.after_json = revised.model_dump(mode="json")
        current.baseline_json = baseline
        current.generation_json = dict(generation)
        current.status = "pending_review"
        current.stage = "prepared"
        current.error = None


def edit_copy_refresh_item(batch_id: str, item_id: str, edit: CopyRefreshEdit) -> dict[str, Any]:
    with session_scope() as session:
        batch = session.scalar(
            select(CopyRefreshBatchRecord)
            .where(CopyRefreshBatchRecord.id == batch_id)
            .options(selectinload(CopyRefreshBatchRecord.items))
        )
        if batch is None:
            raise KeyError(batch_id)
        if batch.status != "pending_review" or batch.version != edit.expected_version:
            raise ValueError("copy refresh review version changed")
        item = next((value for value in batch.items if value.id == item_id), None)
        if item is None or not item.after_json:
            raise KeyError(item_id)
        revised = MarketplaceListing.model_validate({
            **item.after_json,
            "title": edit.title,
            "long_description": edit.long_description,
            "tags": edit.tags,
            "alt_text": edit.alt_text or item.after_json["alt_text"],
        })
        generation = item.generation_json or {}
        original = MarketplaceListingSet.model_validate(
            generation.get("polished") or generation.get("draft")
        )
        validation_set = MarketplaceListingSet(listings=[
            revised if value.channel == Channel.ETSY else value
            for value in original.listings
        ])
        validate_listing_copy(validation_set)
        item.after_json = revised.model_dump(mode="json")
        batch.version += 1
        batch.digest = _batch_digest(batch.items)
        RunRepository(session).audit(item.run_id, "admin", "copy_refresh.edited", {
            "batch_id": batch.id, "version": batch.version,
        })
        return batch_payload(batch)


def approve_copy_refresh_batch(batch_id: str, expected_version: int, digest: str) -> None:
    with session_scope() as session:
        batch = session.scalar(
            select(CopyRefreshBatchRecord)
            .where(CopyRefreshBatchRecord.id == batch_id)
            .options(selectinload(CopyRefreshBatchRecord.items))
        )
        if batch is None:
            raise KeyError(batch_id)
        if (
            batch.status != "pending_review" or batch.version != expected_version
            or batch.digest != digest or not batch.items
            or any(item.status != "pending_review" or not item.after_json for item in batch.items)
        ):
            raise ValueError("copy refresh draft changed or is not ready for approval")
        batch.status = "applying"
        batch.approved_at = datetime.now(UTC)
        batch.approved_by = "admin"
        for item in batch.items:
            item.status = "applying"
            item.stage = "approved"
            RunRepository(session).audit(item.run_id, "admin", "copy_refresh.approved", {
                "batch_id": batch.id, "digest": digest,
            })


def retry_copy_refresh_batch(batch_id: str) -> None:
    with session_scope() as session:
        batch = session.scalar(
            select(CopyRefreshBatchRecord)
            .where(CopyRefreshBatchRecord.id == batch_id)
            .options(selectinload(CopyRefreshBatchRecord.items))
        )
        if batch is None:
            raise KeyError(batch_id)
        if batch.status not in ("verification_required", "applying") or batch.approved_at is None:
            raise ValueError("copy refresh has no approved update to retry")
        batch.status = "applying"
        batch.error = None
        for item in batch.items:
            if item.status == "verification_required":
                item.status = "applying"
                item.error = None


async def apply_copy_refresh_batch(batch_id: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    batch = _load_batch(batch_id)
    if batch.status != "applying" or batch.approved_at is None:
        raise ValueError("copy refresh batch is not approved for application")
    printify = PrintifyClient(settings)
    etsy = EtsyStorefrontClient(settings, await etsy_access_token(settings))
    errors: list[str] = []
    try:
        for item in batch.items:
            if item.status == "applied":
                continue
            try:
                await _apply_item(item.id, printify, etsy, settings)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                errors.append(f"{item.run_id}: {error}")
                with session_scope() as session:
                    current = session.get(CopyRefreshItemRecord, item.id)
                    if current:
                        current.status = "verification_required"
                        current.error = error
    finally:
        await printify.close()
        await etsy.close()
    with session_scope() as session:
        current_batch = session.scalar(
            select(CopyRefreshBatchRecord)
            .where(CopyRefreshBatchRecord.id == batch_id)
            .options(selectinload(CopyRefreshBatchRecord.items))
        )
        if current_batch is None:
            raise KeyError(batch_id)
        current_batch.status = "applied" if all(item.status == "applied" for item in current_batch.items) else "verification_required"
        current_batch.error = "; ".join(errors) or None
        return current_batch.status


async def _apply_item(
    item_id: str, printify: PrintifyClient,
    etsy: EtsyStorefrontClient, settings: Settings,
) -> None:
    with session_scope() as session:
        item = session.get(CopyRefreshItemRecord, item_id)
        if item is None or not item.before_json or not item.after_json or not item.baseline_json:
            raise ValueError("copy refresh item is incomplete")
        run = session.get(RunRecord, item.run_id)
        publish = session.scalar(select(PublishRecord).where(
            PublishRecord.run_id == item.run_id,
            PublishRecord.channel == item.channel,
        ))
        if run is None or publish is None or run.status != "published":
            raise ValueError("source product is no longer published")
        source: dict[str, Any] = {
            "run_id": item.run_id,
            "batch_id": item.batch_id,
            "shop_id": item.printify_shop_id,
            "product_id": item.printify_product_id,
            "listing_id": int(item.marketplace_listing_id),
            "before": item.before_json,
            "after": item.after_json,
            "baseline": item.baseline_json,
            "template": run.publication_template_snapshot or run.template_snapshot,
            "quotes": run.price_quotes,
            "upload_id": publish.artwork_upload_id,
            "featured_image_id": (publish.response_data or {}).get("featured_image_id"),
        }
    desired = MarketplaceListing.model_validate(source["after"])
    target = _listing_fields(desired)
    product = await printify.product(source["shop_id"], source["product_id"])
    listing = await etsy.listing(source["listing_id"])
    inventory = await etsy.inventory(source["listing_id"])
    images = await etsy.images(source["listing_id"])
    variation_images = await etsy.variation_images(source["listing_id"])
    if (
        str((product.get("external") or {}).get("id") or "") != str(source["listing_id"])
        or int(listing.get("listing_id") or 0) != source["listing_id"]
        or int(listing.get("shop_id") or 0) != int(settings.etsy_shop_id or 0)
        or listing.get("state") != "active"
    ):
        raise StorefrontVerificationError("Etsy identity, active state, or Printify link changed")
    invariant_version = int(source["baseline"].get("schema_version") or 1)
    if _invariants(
        product, inventory, images, variation_images, version=invariant_version
    ) != source["baseline"]:
        raise StorefrontVerificationError("variants, prices, mockups, inventory, or photos changed")
    _verify_approved_state(source, product, inventory, images)
    if _copy_fields(product) not in (source["before"]["printify"], target):
        raise StorefrontVerificationError("Printify copy changed since review")
    if _copy_fields(listing) not in (source["before"]["etsy"], target):
        raise StorefrontVerificationError("Etsy copy changed since review")

    if _copy_fields(product) != target:
        with session_scope() as session:
            current = session.get(CopyRefreshItemRecord, item_id)
            if current:
                current.stage = "updating_printify"
        await printify.update_product_copy(
            source["shop_id"], source["product_id"], desired.title,
            desired.long_description, desired.tags,
        )
        product = await printify.product(source["shop_id"], source["product_id"])
    if _copy_fields(product) != target:
        raise StorefrontVerificationError("Printify copy update was not confirmed")
    with session_scope() as session:
        current = session.get(CopyRefreshItemRecord, item_id)
        if current:
            current.stage = "printify_verified"

    if _copy_fields(listing) != target:
        with session_scope() as session:
            current = session.get(CopyRefreshItemRecord, item_id)
            if current:
                current.stage = "updating_etsy"
        await etsy.update_listing(source["listing_id"], {
            "title": desired.title,
            "description": desired.long_description,
            "tags": ",".join(desired.tags),
        })
        listing = await etsy.listing(source["listing_id"])
    if _copy_fields(listing) != target:
        raise StorefrontVerificationError("Etsy copy update was not confirmed")
    with session_scope() as session:
        current = session.get(CopyRefreshItemRecord, item_id)
        if current:
            current.stage = "etsy_verified"

    product = await printify.product(source["shop_id"], source["product_id"])
    listing = await etsy.listing(source["listing_id"])
    inventory = await etsy.inventory(source["listing_id"])
    images = await etsy.images(source["listing_id"])
    variation_images = await etsy.variation_images(source["listing_id"])
    if (
        _copy_fields(product) != target or _copy_fields(listing) != target
        or str((product.get("external") or {}).get("id") or "") != str(source["listing_id"])
        or listing.get("state") != "active"
        or _invariants(
            product, inventory, images, variation_images, version=invariant_version
        ) != source["baseline"]
    ):
        raise StorefrontVerificationError("final Etsy and Printify copy readback differs")
    _verify_approved_state(source, product, inventory, images)
    with session_scope() as session:
        current = session.get(CopyRefreshItemRecord, item_id)
        publish = session.scalar(select(PublishRecord).where(
            PublishRecord.run_id == source["run_id"], PublishRecord.channel == Channel.ETSY.value
        ))
        if current is None or publish is None:
            raise KeyError(item_id)
        current.status = "applied"
        current.stage = "verified"
        current.error = None
        if source["template"] and source["quotes"] and source["upload_id"]:
            template = ProductTemplate.model_validate(source["template"])
            quotes = [
                PriceQuote.model_validate(value) for value in source["quotes"]
                if value["channel"] == Channel.ETSY.value
            ]
            publish.product_fingerprint = printify.product_fingerprint(
                template, desired, quotes, source["upload_id"]
            )
        publish.response_data = {
            **(publish.response_data or {}), "copy_refresh_batch_id": source["batch_id"]
        }
        RunRepository(session).audit(source["run_id"], "worker", "copy_refresh.applied", {
            "batch_id": source["batch_id"], "listing_id": source["listing_id"],
        })


def _verify_approved_state(
    source: dict[str, Any], product: dict[str, Any],
    inventory: dict[str, Any], images: list[dict[str, Any]],
) -> None:
    if not source["template"] or not source["quotes"]:
        raise StorefrontVerificationError("approved template or prices are missing")
    template = ProductTemplate.model_validate(source["template"])
    quotes = [
        PriceQuote.model_validate(value) for value in source["quotes"]
        if value["channel"] == Channel.ETSY.value
    ]
    verify_printify_product(product, template, quotes)
    verify_etsy_inventory(inventory, product, template, quotes)
    if not images:
        raise StorefrontVerificationError("Etsy listing has no photos")
    if source["featured_image_id"]:
        verify_featured_image(images, int(source["featured_image_id"]))
