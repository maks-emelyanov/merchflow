"""Fail-closed, in-place artwork replacement for an already published Etsy run.

The operation deliberately never creates a Printify product or Etsy listing.  It
first proves the database and both providers refer to the same product, retains
the current gallery as evidence, and prepares every replacement mockup before it
makes the Etsy listing inactive.  A failure after the first write leaves an
explicit reconciliation checkpoint and, whenever possible, an inactive listing.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select

from merch.config import Settings, get_settings
from merch.database import session_scope
from merch.domain.prepress import deterministic_qa
from merch.models import (
    ArtifactRecord,
    CopyRefreshItemRecord,
    ProductMappingRecord,
    PublishRecord,
    RunRecord,
)
from merch.repository import RunRepository
from merch.schemas import (
    Channel,
    MarketplaceListing,
    PriceQuote,
    ProductTemplate,
    PublishStatus,
    RunStatus,
)
from merch.services.etsy_auth import etsy_access_token
from merch.services.mockup_verification import (
    Downloader,
    PreparedMockup,
    download_etsy_image,
    prepare_mockups,
    verify_etsy_mockups,
)
from merch.services.printify import PrintifyClient, ProviderConfigurationError, channel_shop
from merch.services.storage import ArtifactStorage
from merch.services.storefront import (
    EtsyStorefrontClient,
    StorefrontVerificationError,
    selector_labels_are_exact,
    verify_etsy_inventory,
    verify_etsy_listing,
    verify_printify_product,
)

Sleep = Callable[[float], Awaitable[None]]
REPLACEMENT_ARTIFACT_KIND_PREFIX = "etsy-replacement-v"
RECONCILIATION_LEASE_TIMEOUT_SECONDS = 30 * 60
UNRESOLVED_REPLACEMENT_STATUSES = frozenset({"in_progress", "reconciling", "failed"})


class ArtworkReplacementError(StorefrontVerificationError):
    """The existing listing cannot be changed safely or verified completely."""


def has_unresolved_artwork_replacement(response_data: dict[str, Any] | None) -> bool:
    """Return whether the Etsy publication is owned by the replacement workflow."""
    replacement = (response_data or {}).get("artwork_replacement") or {}
    return replacement.get("status") in UNRESOLVED_REPLACEMENT_STATUSES


@dataclass(frozen=True)
class _ReplacementContext:
    run_id: str
    run_version: int
    template: ProductTemplate
    listing: MarketplaceListing
    quotes: list[PriceQuote]
    shop_id: str
    product_id: str
    listing_id: int
    artwork_upload_id: str
    product_fingerprint: str
    publish_response: dict[str, Any]
    previous_artifact_id: str | None


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _load_context(
    run_id: str,
    *,
    reconciliation: bool = False,
) -> _ReplacementContext:
    with session_scope() as session:
        run = session.scalar(
            select(RunRecord).where(RunRecord.id == run_id)
        )
        if run is None:
            raise ArtworkReplacementError("Published run does not exist")
        publish = session.scalar(
            select(PublishRecord).where(
                PublishRecord.run_id == run_id,
                PublishRecord.channel == Channel.ETSY.value,
            )
        )
        if (
            publish is None
            or not publish.printify_product_id
            or not publish.external_product_id
            or not publish.artwork_upload_id
        ):
            raise ArtworkReplacementError(
                "Run has no matching Etsy publication checkpoint"
                if reconciliation
                else "Run has no verified, successful Etsy publication to update"
            )
        replacement_state = (publish.response_data or {}).get("artwork_replacement") or {}
        if reconciliation:
            durable_reconciliation = (
                run.status == RunStatus.VERIFICATION_REQUIRED.value
                and publish.status == PublishStatus.RECONCILIATION_REQUIRED.value
            )
            legacy_interrupted = (
                run.status == RunStatus.PUBLISHED.value
                and publish.status == PublishStatus.SUCCEEDED.value
                and replacement_state.get("status") == "in_progress"
                and _initial_replacement_is_stale(replacement_state)
            )
            if not (durable_reconciliation or legacy_interrupted):
                raise ArtworkReplacementError(
                    "Run has no failed artwork replacement to reconcile"
                )
        elif (
            run.status != RunStatus.PUBLISHED.value
            or publish.status != PublishStatus.SUCCEEDED.value
            or replacement_state.get("status") == "in_progress"
        ):
            raise ArtworkReplacementError(
                "Only a fully published run without an unfinished replacement can be updated"
            )
        mapping = session.scalar(
            select(ProductMappingRecord).where(
                ProductMappingRecord.run_id == run_id,
                ProductMappingRecord.channel == Channel.ETSY.value,
                ProductMappingRecord.printify_product_id == publish.printify_product_id,
            )
        )
        if (
            mapping is None
            or str(mapping.marketplace_listing_id or "") != str(publish.external_product_id)
        ):
            raise ArtworkReplacementError("Database product mapping does not match the publication")
        if not run.listings or not run.price_quotes:
            raise ArtworkReplacementError("Published run is missing its approved listing package")
        try:
            template_data = run.publication_template_snapshot or run.template_snapshot
            template = ProductTemplate.model_validate(template_data)
            original_listing = MarketplaceListing.model_validate(next(
                item
                for item in run.listings["listings"]
                if item["channel"] == Channel.ETSY.value
            ))
            copy_refresh = session.scalar(
                select(CopyRefreshItemRecord)
                .where(
                    CopyRefreshItemRecord.run_id == run_id,
                    CopyRefreshItemRecord.channel == Channel.ETSY.value,
                    CopyRefreshItemRecord.status == "applied",
                )
                .order_by(CopyRefreshItemRecord.created_at.desc())
                .limit(1)
            )
            listing = (
                MarketplaceListing.model_validate(copy_refresh.after_json)
                if copy_refresh is not None and copy_refresh.after_json
                else original_listing
            )
            quotes = [
                PriceQuote.model_validate(item)
                for item in run.price_quotes
                if item["channel"] == Channel.ETSY.value
            ]
            listing_id = int(publish.external_product_id)
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise ArtworkReplacementError("Published run package cannot be validated") from exc
        if not quotes:
            raise ArtworkReplacementError("Published run has no approved Etsy prices")
        prior_replacement = (publish.response_data or {}).get("artwork_replacement") or {}
        previous_id = str(prior_replacement.get("new_artifact_id") or "") or None
        previous = None
        if previous_id is not None:
            previous = session.scalar(
                select(ArtifactRecord).where(
                    ArtifactRecord.id == previous_id,
                    ArtifactRecord.run_id == run_id,
                )
            )
        if previous is None:
            previous = session.scalar(
                select(ArtifactRecord)
                .where(
                    ArtifactRecord.run_id == run_id,
                    ArtifactRecord.kind == f"production-v{run.version}",
                )
                .order_by(ArtifactRecord.revision.desc())
                .limit(1)
            )
        return _ReplacementContext(
            run_id=run_id,
            run_version=run.version,
            template=template,
            listing=listing,
            quotes=quotes,
            shop_id=channel_shop(template, Channel.ETSY),
            product_id=str(publish.printify_product_id),
            listing_id=listing_id,
            artwork_upload_id=str(publish.artwork_upload_id),
            product_fingerprint=publish.product_fingerprint,
            publish_response=deepcopy(publish.response_data or {}),
            previous_artifact_id=previous.id if previous is not None else None,
        )


def _validate_artwork(
    artwork: bytes, context: _ReplacementContext, settings: Settings
) -> tuple[str, dict[str, Any]]:
    digest = hashlib.sha256(artwork).hexdigest()
    if not artwork or len(artwork) > settings.max_artifact_bytes:
        raise ArtworkReplacementError("Replacement artwork is empty or exceeds the upload limit")
    try:
        with Image.open(io.BytesIO(artwork)) as image:
            image.verify()
        with Image.open(io.BytesIO(artwork)) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                raise ArtworkReplacementError("Replacement artwork must be one PNG frame")
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise ArtworkReplacementError("Replacement artwork is not a valid PNG") from exc
    report = deterministic_qa(
        artwork,
        expected_width=context.template.print_width,
        expected_height=context.template.print_height,
        shirt_colors=context.template.qa_shirt_colors(),
        revision=1,
        max_bytes=settings.max_artifact_bytes,
        enforce_composition_scale=True,
    )
    if not report.passed or not report.has_alpha:
        codes = sorted({item.code for item in report.issues if item.severity == "error"})
        reason = ", ".join(codes) if codes else "transparent background required"
        raise ArtworkReplacementError(f"Replacement artwork failed deterministic QA: {reason}")
    return digest, report.model_dump(mode="json")


def _validate_quality_attestation(attestation: dict[str, Any] | None, digest: str) -> None:
    if not isinstance(attestation, dict):
        raise ArtworkReplacementError("Live replacement requires a quality attestation")
    if attestation.get("passed") is not True:
        raise ArtworkReplacementError("Quality attestation did not approve the replacement")
    if str(attestation.get("artwork_sha256") or "") != digest:
        raise ArtworkReplacementError("Quality attestation belongs to different artwork")
    if not str(attestation.get("reviewer") or "").strip():
        raise ArtworkReplacementError("Quality attestation has no reviewer")


def _print_area_image_ids(print_areas: Any, position: str) -> list[str]:
    if not isinstance(print_areas, list) or not print_areas:
        raise ArtworkReplacementError("Printify product has no print areas")
    ids: list[str] = []
    for area in print_areas:
        if not isinstance(area, dict) or not area.get("variant_ids"):
            raise ArtworkReplacementError("Printify product has an invalid print area")
        placeholders = area.get("placeholders")
        if not isinstance(placeholders, list):
            raise ArtworkReplacementError("Printify print area has no placeholders")
        targets = [item for item in placeholders if item.get("position") == position]
        if len(targets) != 1:
            raise ArtworkReplacementError(
                f"Every Printify print area must have exactly one {position} placeholder"
            )
        for placeholder in placeholders:
            images = placeholder.get("images") or []
            if placeholder is not targets[0] and images:
                raise ArtworkReplacementError(
                    "Product contains additional artwork that cannot be replaced automatically"
                )
        images = targets[0].get("images") or []
        if len(images) != 1 or not images[0].get("id"):
            raise ArtworkReplacementError(
                "Each target placeholder must contain exactly one artwork image"
            )
        ids.append(str(images[0]["id"]))
    return ids


def _verify_print_area_variant_coverage(
    print_areas: Any,
    template: ProductTemplate,
) -> None:
    """Require the target print areas to partition the exact approved variants."""
    if not isinstance(print_areas, list) or not print_areas:
        raise ArtworkReplacementError("Printify product has no print areas")
    expected = {item.variant_id for item in template.variants if item.enabled}
    found: list[int] = []
    for area in print_areas:
        if not isinstance(area, dict) or not isinstance(area.get("variant_ids"), list):
            raise ArtworkReplacementError("Printify product has an invalid print area")
        try:
            variant_ids = [int(value) for value in area["variant_ids"]]
        except (TypeError, ValueError) as exc:
            raise ArtworkReplacementError(
                "Printify print-area variants are invalid"
            ) from exc
        if not variant_ids or any(value <= 0 for value in variant_ids):
            raise ArtworkReplacementError("Printify print-area variants are invalid")
        if len(variant_ids) != len(set(variant_ids)):
            raise ArtworkReplacementError("Printify print area repeats a variant")
        found.extend(variant_ids)
    if len(found) != len(set(found)) or set(found) != expected:
        raise ArtworkReplacementError(
            "Printify print areas do not cover the exact approved variants"
        )


def replacement_print_areas(
    print_areas: list[dict[str, Any]], position: str, upload_id: str
) -> list[dict[str, Any]]:
    """Build Printify's writable print-area shape with preserved placement.

    Product readbacks contain provider-owned fields such as ``src``, dimensions,
    layer IDs, decoration metadata, and empty placeholders.  Echoing those fields
    into the update endpoint is rejected.  Keep only the documented writable
    fields while preserving every variant assignment and transform value.
    """
    _print_area_image_ids(print_areas, position)
    updated: list[dict[str, Any]] = []
    for area in print_areas:
        target = next(item for item in area["placeholders"] if item.get("position") == position)
        current = target["images"][0]
        transform: dict[str, float | int | str] = {"id": upload_id}
        for key, default in (("x", 0.5), ("y", 0.5), ("scale", 1.0)):
            value = current.get(key, default)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise ArtworkReplacementError(
                    f"Printify artwork has an invalid {key} placement value"
                )
            transform[key] = float(value)
        angle = current.get("angle", 0)
        if (
            not isinstance(angle, (int, float))
            or isinstance(angle, bool)
            or not math.isfinite(float(angle))
            or not float(angle).is_integer()
        ):
            raise ArtworkReplacementError(
                "Printify artwork has an invalid angle placement value"
            )
        # Printify validates this field as an integer even when other placement
        # coordinates accept JSON numbers.
        transform["angle"] = int(angle)
        updated.append({
            "variant_ids": deepcopy(area["variant_ids"]),
            "placeholders": [{"position": position, "images": [transform]}],
        })
    return updated


def _product_invariants(product: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(product.get(key))
        for key in (
            "id",
            "title",
            "description",
            "tags",
            "blueprint_id",
            "print_provider_id",
            "variants",
            "external",
        )
    }


def _verify_product_identity(
    product: dict[str, Any], context: _ReplacementContext, *, expected_upload_id: str
) -> None:
    if str(product.get("id") or "") != context.product_id:
        raise ArtworkReplacementError("Printify returned a different product")
    if product.get("is_locked") is not False:
        raise ArtworkReplacementError("Printify product is locked or lock state is unknown")
    external = product.get("external") or {}
    if str(external.get("id") or "") != str(context.listing_id):
        raise ArtworkReplacementError("Printify product points to a different Etsy listing")
    if product.get("title") != context.listing.title:
        raise ArtworkReplacementError("Printify product title differs from the approved listing")
    if product.get("description") != context.listing.long_description:
        raise ArtworkReplacementError(
            "Printify product description differs from the approved listing"
        )
    remote_tags = product.get("tags")
    if (
        not isinstance(remote_tags, list)
        or sorted(str(item) for item in remote_tags) != sorted(context.listing.tags)
    ):
        raise ArtworkReplacementError("Printify product tags differ from the approved listing")
    try:
        blueprint_id = int(product.get("blueprint_id") or 0)
        provider_id = int(product.get("print_provider_id") or 0)
    except (TypeError, ValueError) as exc:
        raise ArtworkReplacementError("Printify product catalog identity is invalid") from exc
    if (
        blueprint_id != context.template.blueprint_id
        or provider_id != context.template.print_provider_id
    ):
        raise ArtworkReplacementError(
            "Printify product blueprint or provider differs from the approved template"
        )
    _verify_print_area_variant_coverage(product.get("print_areas"), context.template)
    image_ids = _print_area_image_ids(product.get("print_areas"), context.template.position)
    if set(image_ids) != {expected_upload_id}:
        raise ArtworkReplacementError("Printify product artwork differs from the expected checkpoint")
    if expected_upload_id == context.artwork_upload_id:
        expected_fingerprint = PrintifyClient.product_fingerprint(
            context.template,
            context.listing,
            context.quotes,
            expected_upload_id,
        )
        if context.product_fingerprint != expected_fingerprint:
            raise ArtworkReplacementError(
                "Saved publication fingerprint differs from the approved package"
            )
    verify_printify_product(product, context.template, context.quotes)


async def _matching_orders(printify: PrintifyClient, context: _ReplacementContext) -> list[str]:
    matches: list[str] = []
    page = 1
    last_page: int | None = None
    while page <= 100:
        response = await printify.orders(context.shop_id, page=page)
        items = response.get("data")
        if not isinstance(items, list):
            raise ArtworkReplacementError("Printify orders response is incomplete")
        for order in items:
            if any(
                str(item.get("product_id") or "") == context.product_id
                for item in (order.get("line_items") or [])
                if isinstance(item, dict)
            ):
                matches.append(str(order.get("id") or "unknown"))
        if last_page is None:
            raw_last = response.get("last_page")
            if raw_last is None:
                if not items:
                    return matches
                raise ArtworkReplacementError(
                    "Printify orders response has no complete pagination metadata"
                )
            try:
                last_page = int(raw_last)
            except (TypeError, ValueError) as exc:
                raise ArtworkReplacementError(
                    "Printify orders pagination metadata is invalid"
                ) from exc
            if last_page < page or last_page > 100:
                raise ArtworkReplacementError("Printify orders pagination is outside safety limits")
        if page >= last_page:
            return matches
        page += 1
    raise ArtworkReplacementError("Printify orders could not be exhaustively checked")


async def _matching_etsy_transactions(
    etsy: EtsyStorefrontClient,
    context: _ReplacementContext,
) -> list[str]:
    rows = await etsy.listing_transactions(context.listing_id)
    return [str(item.get("transaction_id") or "unknown") for item in rows]


def _color_value_ids(inventory: dict[str, Any], colors: set[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for product in inventory.get("products", []):
        if product.get("is_deleted"):
            continue
        props = [
            item
            for item in product.get("property_values", [])
            if int(item.get("property_id") or 0) == 514
        ]
        if len(props) != 1:
            raise ArtworkReplacementError("Etsy inventory has no unique Color selector")
        values = props[0].get("values") or []
        ids = props[0].get("value_ids") or []
        if len(values) != 1 or len(ids) != 1 or int(ids[0]) <= 0:
            raise ArtworkReplacementError("Etsy Color selector has an invalid value")
        color, value_id = str(values[0]), int(ids[0])
        if color in result and result[color] != value_id:
            raise ArtworkReplacementError("Etsy returned inconsistent Color value IDs")
        result[color] = value_id
    if set(result) != colors or len(set(result.values())) != len(colors):
        raise ArtworkReplacementError("Etsy colors do not match the approved template")
    return result


async def _replace_etsy_gallery(
    etsy: EtsyStorefrontClient,
    context: _ReplacementContext,
    inventory: dict[str, Any],
    replacement_mockups: list[PreparedMockup],
    checkpoint: Callable[..., None],
    *,
    settings: Settings,
    expected_gallery: list[dict[str, Any]],
    expected_variation_images: list[dict[str, Any]],
) -> dict[str, int]:
    """Swap a gallery without ever asking Etsy to hold zero images.

    Etsy rejects deletion of the last listing image.  Retain one old image as a
    sentinel, upload the first replacement, then remove the sentinel and finish
    the new gallery.  The listing is already inactive while this runs.
    """
    if not replacement_mockups:
        raise ArtworkReplacementError("Replacement mockup set is empty")
    barrier_inventory = await etsy.inventory(context.listing_id)
    old_images = await etsy.images(context.listing_id)
    old_links = await etsy.variation_images(context.listing_id)
    barrier_listing = await etsy.listing(context.listing_id)
    _verify_etsy_identity_for_reconciliation(barrier_listing, context, settings)
    if barrier_listing.get("state") != "inactive":
        raise ArtworkReplacementError(
            "Etsy must remain inactive immediately before gallery replacement"
        )
    if _digest(barrier_inventory) != _digest(inventory):
        raise ArtworkReplacementError(
            "Etsy inventory changed immediately before gallery replacement"
        )
    if (
        _gallery_key(old_images) != _gallery_key(expected_gallery)
        or _variation_key(old_links) != _variation_key(expected_variation_images)
    ):
        raise ArtworkReplacementError(
            "Etsy gallery or color-photo links changed before replacement"
        )
    await etsy.update_variation_images(context.listing_id, [])
    if await etsy.variation_images(context.listing_id):
        raise ArtworkReplacementError("Etsy did not clear old variation-image links")
    checkpoint(stage="old_variation_images_unlinked")

    old_ids = [int(item.get("listing_image_id") or 0) for item in old_images]
    if not old_ids or any(value <= 0 for value in old_ids) or len(set(old_ids)) != len(old_ids):
        raise ArtworkReplacementError("Etsy gallery snapshot has invalid image IDs")
    sentinel_id = old_ids[-1]
    for image_id in old_ids[:-1]:
        checkpoint(stage="deleting_etsy_gallery", image_delete_started=image_id)
        await etsy.delete_image(context.listing_id, image_id)
        remaining = await etsy.images(context.listing_id)
        if any(int(item.get("listing_image_id") or 0) == image_id for item in remaining):
            raise ArtworkReplacementError("Etsy did not delete an old gallery image")
    checkpoint(
        stage="old_gallery_reduced_to_sentinel",
        old_etsy_image_ids=old_ids,
        retained_old_etsy_image_id=sentinel_id,
        image_delete_started=None,
    )

    image_ids: dict[str, int] = {}

    async def upload(mockup: PreparedMockup, rank: int) -> int:
        checkpoint(
            stage="uploading_etsy_gallery",
            image_upload_started_color=mockup.color,
            new_etsy_image_ids=deepcopy(image_ids),
        )
        image_id = await etsy.upload_mockup(
            context.listing_id,
            mockup.image,
            mockup.content_type,
            f"{context.listing.alt_text} on {mockup.color} shirt"[:500],
            rank,
        )
        if image_id <= 0 or image_id in image_ids.values() or image_id in old_ids:
            raise ArtworkReplacementError("Etsy returned an invalid or duplicate image ID")
        image_ids[mockup.color] = image_id
        checkpoint(
            stage="uploading_etsy_gallery",
            image_upload_started_color=None,
            new_etsy_image_ids=deepcopy(image_ids),
        )
        return image_id

    first_id = await upload(replacement_mockups[0], 1)
    checkpoint(
        stage="replacement_sentinel_uploaded",
        retained_old_etsy_image_id=sentinel_id,
        new_etsy_image_ids=deepcopy(image_ids),
    )
    await etsy.delete_image(context.listing_id, sentinel_id)
    after_sentinel = await etsy.images(context.listing_id)
    if {
        int(item.get("listing_image_id") or 0) for item in after_sentinel
    } != {first_id}:
        raise ArtworkReplacementError("Etsy did not complete the sentinel image handoff")
    checkpoint(
        stage="old_gallery_deleted",
        old_etsy_image_ids=old_ids,
        retained_old_etsy_image_id=None,
        image_delete_started=None,
    )

    for rank, mockup in enumerate(replacement_mockups[1:], start=2):
        await upload(mockup, rank)
    gallery = await etsy.images(context.listing_id)
    if {int(item.get("listing_image_id") or 0) for item in gallery} != set(image_ids.values()):
        raise ArtworkReplacementError("Etsy gallery does not exactly match new uploads")
    color_ids = _color_value_ids(
        inventory,
        {item.color for item in replacement_mockups},
    )
    links = [
        {
            "property_id": 514,
            "value_id": color_ids[item.color],
            "image_id": image_ids[item.color],
        }
        for item in replacement_mockups
    ]
    await etsy.update_variation_images(context.listing_id, links)
    checkpoint(stage="variation_images_linked")
    return image_ids


def _checkpoint(
    context: _ReplacementContext,
    operation_id: str,
    *,
    stage: str,
    status: str = "in_progress",
    error: str | None = None,
    **updates: Any,
) -> None:
    with session_scope() as session:
        session.scalar(
            select(RunRecord.id).where(RunRecord.id == context.run_id).with_for_update()
        )
        publish = session.scalar(
            select(PublishRecord).where(
                PublishRecord.run_id == context.run_id,
                PublishRecord.channel == Channel.ETSY.value,
            )
        )
        if publish is None:
            raise ArtworkReplacementError("Publication disappeared during replacement")
        response = dict(publish.response_data or {})
        prior = dict(response.get("artwork_replacement") or {})
        prior_operation = str(prior.get("operation_id") or "")
        if prior_operation and prior_operation != operation_id and prior.get("status") == "in_progress":
            raise ArtworkReplacementError("Another artwork replacement is already in progress")
        prior_reconciliation = str(prior.get("reconciliation_attempt_id") or "")
        incoming_reconciliation = str(updates.get("reconciliation_attempt_id") or "")
        if prior_reconciliation and incoming_reconciliation != prior_reconciliation:
            raise ArtworkReplacementError(
                "Another artwork-replacement reconciliation owns this checkpoint"
            )
        if prior.get("status") == "reconciling" and not incoming_reconciliation:
            raise ArtworkReplacementError(
                "Another artwork-replacement reconciliation owns this checkpoint"
            )
        replacement = {
            **prior,
            **deepcopy(updates),
            "operation_id": operation_id,
            "stage": stage,
            "status": status,
            "updated_at": _utcnow(),
        }
        if error is not None:
            replacement["error"] = error[:4000]
        response["artwork_replacement"] = replacement
        publish.response_data = response
        if status == "failed":
            publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
            publish.error = (error or "Artwork replacement requires reconciliation")[:4000]
            run = session.get(RunRecord, context.run_id)
            if run is not None:
                run.status = RunStatus.VERIFICATION_REQUIRED.value
                run.error = publish.error
        elif status == "aborted":
            publish.status = PublishStatus.SUCCEEDED.value
            publish.error = None
            run = session.get(RunRecord, context.run_id)
            if run is not None:
                run.status = RunStatus.PUBLISHED.value
                run.error = None


def _initial_checkpoint(
    context: _ReplacementContext,
    operation_id: str,
    artwork_digest: str,
    deterministic_report: dict[str, Any],
    quality_attestation: dict[str, Any],
    snapshot: dict[str, Any],
    artifact_key: str,
) -> None:
    with session_scope() as session:
        session.scalar(
            select(RunRecord.id).where(RunRecord.id == context.run_id).with_for_update()
        )
        publish = session.scalar(
            select(PublishRecord).where(
                PublishRecord.run_id == context.run_id,
                PublishRecord.channel == Channel.ETSY.value,
            )
        )
        run = session.get(RunRecord, context.run_id)
        if (
            run is None
            or run.status != RunStatus.PUBLISHED.value
            or publish is None
            or publish.status != PublishStatus.SUCCEEDED.value
            or publish.printify_product_id != context.product_id
            or publish.external_product_id != str(context.listing_id)
            or publish.artwork_upload_id != context.artwork_upload_id
        ):
            raise ArtworkReplacementError("Publication changed while replacement was prepared")
        response = dict(publish.response_data or {})
        prior = response.get("artwork_replacement") or {}
        if prior.get("status") == "in_progress":
            raise ArtworkReplacementError(
                "A prior replacement is unfinished; reconcile it before another write"
            )
        response["artwork_replacement"] = {
            "operation_id": operation_id,
            "status": "in_progress",
            "stage": "preflight_complete",
            "started_at": _utcnow(),
            "updated_at": _utcnow(),
            "artwork_sha256": artwork_digest,
            "artifact_object_key": artifact_key,
            "deterministic_qa": deterministic_report,
            "quality_attestation": deepcopy(quality_attestation),
            "snapshot": deepcopy(snapshot),
        }
        publish.response_data = response
        publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
        publish.error = (
            "Artwork replacement is in progress; reconcile the durable checkpoint "
            "if the operator process is interrupted"
        )
        run.status = RunStatus.VERIFICATION_REQUIRED.value
        run.error = publish.error
        RunRepository(session).audit(
            context.run_id,
            "operator",
            "artwork_replacement.started",
            {
                "operation_id": operation_id,
                "product_id": context.product_id,
                "listing_id": context.listing_id,
                "artwork_sha256": artwork_digest,
            },
        )


async def _wait_for_replacement_mockups(
    printify: PrintifyClient,
    context: _ReplacementContext,
    upload_id: str,
    baseline: list[PreparedMockup],
    *,
    downloader: Downloader | None,
    evidence_writer: Callable[[bytes, str], str] | None,
    checkpoint: Callable[..., None] | None,
    attempts: int,
    interval_seconds: float,
    sleep: Sleep,
) -> tuple[dict[str, Any], list[PreparedMockup]]:
    old_pixels = {
        item.color: str(item.evidence.get("source_pixel_sha256") or "") for item in baseline
    }
    last_error = "Printify has not rendered replacement mockups"
    for attempt in range(attempts):
        if checkpoint is not None:
            checkpoint(
                stage="replacement_mockups_waiting",
                mockup_readback_attempt=attempt + 1,
            )
        product = await printify.product(context.shop_id, context.product_id)
        try:
            _verify_product_identity(product, context, expected_upload_id=upload_id)
            prepared = await prepare_mockups(
                product,
                context.template,
                downloader=downloader,
                evidence_writer=evidence_writer,
            )
            current_pixels = {
                item.color: str(item.evidence.get("source_pixel_sha256") or "")
                for item in prepared
            }
            if set(current_pixels) != set(old_pixels) or any(
                not current_pixels[color] or current_pixels[color] == old_pixels[color]
                for color in old_pixels
            ):
                raise ArtworkReplacementError(
                    "Printify mockups still contain the previous artwork"
                )
            return product, prepared
        except (StorefrontVerificationError, KeyError, ValueError) as exc:
            last_error = str(exc)
        if attempt + 1 < attempts:
            await sleep(interval_seconds)
    raise ArtworkReplacementError(
        f"Replacement mockups were not ready after {attempts} readbacks: {last_error}"
    )


def _existing_completed(response: dict[str, Any], artwork_digest: str) -> dict[str, Any] | None:
    replacement = response.get("artwork_replacement") or {}
    if replacement.get("status") == "completed" and replacement.get("artwork_sha256") == artwork_digest:
        return deepcopy(replacement)
    return None


def _gallery_key(items: Any) -> list[tuple[int, int, str, str]]:
    if not isinstance(items, list):
        raise ArtworkReplacementError("Etsy gallery snapshot is invalid")
    return sorted(
        (
            int(item.get("listing_image_id") or 0),
            int(item.get("rank") or 0),
            str(item.get("url_fullxfull") or item.get("url_570xN") or ""),
            str(item.get("alt_text") or ""),
        )
        for item in items
        if isinstance(item, dict)
    )


def _variation_key(items: Any) -> list[tuple[int, int, int]]:
    if not isinstance(items, list):
        raise ArtworkReplacementError("Etsy variation-image snapshot is invalid")
    return sorted(
        (
            int(item.get("property_id") or 0),
            int(item.get("value_id") or 0),
            int(item.get("image_id") or 0),
        )
        for item in items
        if isinstance(item, dict)
    )


def _source_pixel_map(items: Any) -> dict[str, str]:
    if not isinstance(items, list):
        return {}
    return {
        str(item.get("color") or ""): str(item.get("source_pixel_sha256") or "")
        for item in items
        if isinstance(item, dict) and item.get("color") and item.get("source_pixel_sha256")
    }


def _failed_replacement_context(
    run_id: str,
) -> tuple[_ReplacementContext, dict[str, Any]]:
    context = _load_context(run_id, reconciliation=True)
    replacement = deepcopy(context.publish_response.get("artwork_replacement") or {})
    status = replacement.get("status")
    stale_initial_operation = (
        status == "in_progress" and _initial_replacement_is_stale(replacement)
    )
    if not (
        (
            status == "failed"
            and replacement.get("stage") == "reconciliation_required"
        )
        or (status == "reconciling" and _reconciliation_lease_is_stale(replacement))
        or stale_initial_operation
    ):
        if status == "in_progress":
            raise ArtworkReplacementError(
                "Artwork replacement is still in progress; wait for its checkpoint lease"
            )
        if status == "reconciling":
            raise ArtworkReplacementError(
                "Artwork replacement reconciliation is already in progress"
            )
        raise ArtworkReplacementError(
            "Artwork replacement has no failed reconciliation checkpoint"
        )
    required_strings = (
        "operation_id",
        "artwork_sha256",
        "artifact_object_key",
    )
    if any(not str(replacement.get(key) or "") for key in required_strings):
        raise ArtworkReplacementError("Artwork replacement checkpoint is incomplete")
    if not isinstance(replacement.get("snapshot"), dict):
        raise ArtworkReplacementError("Artwork replacement snapshot is missing")
    if not isinstance(replacement.get("quality_attestation"), dict):
        raise ArtworkReplacementError("Artwork replacement attestation is missing")
    return context, replacement


def _checkpoint_timestamp_is_stale(
    replacement: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    raw_timestamp = replacement.get("updated_at") or replacement.get(
        "reconciliation_started_at"
    ) or replacement.get(
        "started_at"
    )
    if not isinstance(raw_timestamp, str) or not raw_timestamp:
        return False
    try:
        timestamp = datetime.fromisoformat(raw_timestamp)
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        return False
    current = now or datetime.now(UTC)
    return (current - timestamp.astimezone(UTC)).total_seconds() >= (
        RECONCILIATION_LEASE_TIMEOUT_SECONDS
    )


def _reconciliation_lease_is_stale(
    replacement: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    return replacement.get("status") == "reconciling" and _checkpoint_timestamp_is_stale(
        replacement, now=now
    )


def _initial_replacement_is_stale(
    replacement: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    return replacement.get("status") == "in_progress" and _checkpoint_timestamp_is_stale(
        replacement, now=now
    )


@dataclass(frozen=True)
class _ReconciliationInspection:
    action: str
    product: dict[str, Any]
    listing: dict[str, Any]
    inventory: dict[str, Any]
    gallery: list[dict[str, Any]]
    variation_images: list[dict[str, Any]]
    replacement_mockups: list[PreparedMockup]
    summary: dict[str, Any]


def _verify_etsy_identity_for_reconciliation(
    listing: dict[str, Any],
    context: _ReplacementContext,
    settings: Settings,
) -> None:
    tags = listing.get("tags")
    if (
        int(listing.get("listing_id") or 0) != context.listing_id
        or int(listing.get("shop_id") or 0) != int(settings.etsy_shop_id or 0)
        or listing.get("title") != context.listing.title
        or listing.get("description") != context.listing.long_description
        or not isinstance(tags, list)
        or sorted(str(item) for item in tags) != sorted(context.listing.tags)
        or listing.get("state") not in {"active", "inactive"}
    ):
        raise ArtworkReplacementError(
            "Etsy listing identity, copy, shop, or state changed"
        )


async def _verify_exact_old_remote_state(
    context: _ReplacementContext,
    snapshot: dict[str, Any],
    settings: Settings,
    printify: PrintifyClient,
    etsy: EtsyStorefrontClient,
    baseline_mockups: list[PreparedMockup],
    *,
    expected_listing_state: str,
    downloader: Downloader | None,
) -> None:
    """Prove the original fulfillment and storefront state before reactivation."""
    product = await printify.product(context.shop_id, context.product_id)
    _verify_product_identity(
        product,
        context,
        expected_upload_id=context.artwork_upload_id,
    )
    if _digest(_product_invariants(product)) != str(
        snapshot["printify"]["invariants_sha256"]
    ):
        raise ArtworkReplacementError(
            "Printify product changed before safe Etsy reactivation"
        )
    if replacement_print_areas(
        product["print_areas"],
        context.template.position,
        context.artwork_upload_id,
    ) != replacement_print_areas(
        snapshot["printify"]["print_areas"],
        context.template.position,
        context.artwork_upload_id,
    ):
        raise ArtworkReplacementError(
            "Printify print-area placement changed before safe Etsy reactivation"
        )
    listing = await etsy.listing(context.listing_id)
    _verify_etsy_identity_for_reconciliation(listing, context, settings)
    if listing.get("state") != expected_listing_state:
        raise ArtworkReplacementError(
            f"Etsy listing is not {expected_listing_state} during safety verification"
        )
    inventory = await etsy.inventory(context.listing_id)
    if _digest(inventory) != str(snapshot["etsy"]["inventory_sha256"]):
        raise ArtworkReplacementError(
            "Etsy inventory changed before safe reactivation"
        )
    verify_etsy_inventory(inventory, product, context.template, context.quotes)
    if not selector_labels_are_exact(inventory):
        raise ArtworkReplacementError("Etsy selectors are not exactly Size and Color")
    gallery = await etsy.images(context.listing_id)
    variation_images = await etsy.variation_images(context.listing_id)
    if (
        _gallery_key(gallery) != _gallery_key(snapshot["etsy"]["gallery"])
        or _variation_key(variation_images)
        != _variation_key(snapshot["etsy"]["variation_images"])
    ):
        raise ArtworkReplacementError(
            "Etsy gallery or color-photo links changed before safe reactivation"
        )
    verification = await verify_etsy_mockups(
        etsy,
        context.listing_id,
        context.template,
        baseline_mockups,
        inventory,
        image_ids=snapshot["etsy"]["baseline_verification"]["image_ids"],
        downloader=downloader or download_etsy_image,
    )
    if (
        _gallery_key(verification.get("final_gallery"))
        != _gallery_key(snapshot["etsy"]["gallery"])
        or _variation_key(verification.get("final_variation_images"))
        != _variation_key(snapshot["etsy"]["variation_images"])
        or _digest(verification.get("final_inventory"))
        != str(snapshot["etsy"]["inventory_sha256"])
    ):
        raise ArtworkReplacementError(
            "Etsy storefront changed during safe-reactivation verification"
        )


async def _inspect_failed_replacement(
    context: _ReplacementContext,
    replacement: dict[str, Any],
    settings: Settings,
    printify: PrintifyClient,
    etsy: EtsyStorefrontClient,
    *,
    evidence_writer: Callable[[bytes, str], str] | None = None,
) -> _ReconciliationInspection:
    snapshot = replacement["snapshot"]
    try:
        saved_print_areas = snapshot["printify"]["print_areas"]
        invariant_digest = str(snapshot["printify"]["invariants_sha256"])
        inventory_digest = str(snapshot["etsy"]["inventory_sha256"])
        old_gallery = snapshot["etsy"]["gallery"]
        old_links = snapshot["etsy"]["variation_images"]
        baseline_checks = snapshot["etsy"]["baseline_verification"]["checks"]
    except (KeyError, TypeError) as exc:
        raise ArtworkReplacementError(
            "Artwork replacement snapshot is incomplete"
        ) from exc

    product = await printify.product(context.shop_id, context.product_id)
    upload_ids = set(_print_area_image_ids(
        product.get("print_areas"), context.template.position
    ))
    new_upload_id = str(replacement.get("new_artwork_upload_id") or "")
    if upload_ids == {context.artwork_upload_id}:
        current_upload_id = context.artwork_upload_id
        action = "abort"
    elif upload_ids == {new_upload_id}:
        current_upload_id = new_upload_id
        action = "resume"
    else:
        raise ArtworkReplacementError(
            "Printify has mixed or unrecognized artwork upload IDs"
        )
    _verify_product_identity(
        product, context, expected_upload_id=current_upload_id
    )
    if _digest(_product_invariants(product)) != invariant_digest:
        raise ArtworkReplacementError(
            "Printify product identity, variants, or copy changed from the checkpoint"
        )
    current_writable = replacement_print_areas(
        product["print_areas"], context.template.position, current_upload_id
    )
    expected_writable = replacement_print_areas(
        saved_print_areas, context.template.position, current_upload_id
    )
    if current_writable != expected_writable:
        raise ArtworkReplacementError(
            "Printify print-area placement or variant coverage changed"
        )

    listing = await etsy.listing(context.listing_id)
    _verify_etsy_identity_for_reconciliation(listing, context, settings)
    inventory = await etsy.inventory(context.listing_id)
    if _digest(inventory) != inventory_digest:
        raise ArtworkReplacementError("Etsy inventory changed from the checkpoint")
    verify_etsy_inventory(inventory, product, context.template, context.quotes)
    if not selector_labels_are_exact(inventory):
        raise ArtworkReplacementError("Etsy selectors are not exactly Size and Color")
    orders = await _matching_orders(printify, context)
    transactions = await _matching_etsy_transactions(etsy, context)
    if orders or transactions:
        raise ArtworkReplacementError(
            "Artwork replacement cannot continue after a matching order or transaction"
        )
    gallery = await etsy.images(context.listing_id)
    variation_images = await etsy.variation_images(context.listing_id)

    replacement_mockups: list[PreparedMockup] = []
    if action == "abort":
        if (
            _gallery_key(gallery) != _gallery_key(old_gallery)
            or _variation_key(variation_images) != _variation_key(old_links)
        ):
            raise ArtworkReplacementError(
                "Etsy changed even though Printify still has the old artwork"
            )
        baseline_mockups = await prepare_mockups(product, context.template)
        await verify_etsy_mockups(
            etsy,
            context.listing_id,
            context.template,
            baseline_mockups,
            inventory,
            downloader=download_etsy_image,
        )
        gallery_state = "unchanged_old_gallery"
    else:
        if listing.get("state") != "inactive":
            raise ArtworkReplacementError(
                "Etsy must remain inactive while replacement artwork is unresolved"
            )
        saved_replacement_pixels = _source_pixel_map(
            replacement.get("replacement_mockups")
        )
        baseline_pixels = _source_pixel_map(baseline_checks)
        if not saved_replacement_pixels or set(saved_replacement_pixels) != set(baseline_pixels):
            raise ArtworkReplacementError(
                "Saved replacement mockup evidence is incomplete"
            )
        replacement_mockups = await prepare_mockups(
            product,
            context.template,
            evidence_writer=evidence_writer,
        )
        current_pixels = {
            item.color: str(item.evidence.get("source_pixel_sha256") or "")
            for item in replacement_mockups
        }
        if current_pixels != saved_replacement_pixels or any(
            current_pixels[color] == baseline_pixels[color]
            for color in current_pixels
        ):
            raise ArtworkReplacementError(
                "Current Printify mockups do not match the saved replacement evidence"
            )
        if (
            _gallery_key(gallery) == _gallery_key(old_gallery)
            and _variation_key(variation_images) == _variation_key(old_links)
        ):
            gallery_state = "unchanged_old_gallery"
        else:
            retained_id = int(replacement.get("retained_old_etsy_image_id") or 0)
            current_ids = [int(item.get("listing_image_id") or 0) for item in gallery]
            if (
                retained_id <= 0
                or current_ids != [retained_id]
                or retained_id not in {item[0] for item in _gallery_key(old_gallery)}
                or variation_images
            ):
                raise ArtworkReplacementError(
                    "Etsy gallery is partially changed and requires manual reconciliation"
                )
            gallery_state = "retained_old_sentinel"

    summary = {
        "run_id": context.run_id,
        "operation_id": replacement["operation_id"],
        "action": action,
        "printify_product_id": context.product_id,
        "etsy_listing_id": context.listing_id,
        "printify_artwork_upload_id": current_upload_id,
        "etsy_state": listing.get("state"),
        "gallery_state": gallery_state,
        "matching_orders": 0,
        "matching_etsy_transactions": 0,
        "checks_passed": True,
    }
    return _ReconciliationInspection(
        action=action,
        product=product,
        listing=listing,
        inventory=inventory,
        gallery=gallery,
        variation_images=variation_images,
        replacement_mockups=replacement_mockups,
        summary=summary,
    )


def _acquire_reconciliation_lease(
    context: _ReplacementContext,
    operation_id: str,
    *,
    stale_attempt_id: str | None = None,
) -> str:
    attempt_id = str(uuid4())
    with session_scope() as session:
        session.scalar(
            select(RunRecord.id)
            .where(RunRecord.id == context.run_id)
            .with_for_update()
        )
        run = session.get(RunRecord, context.run_id)
        publish = session.scalar(select(PublishRecord).where(
            PublishRecord.run_id == context.run_id,
            PublishRecord.channel == Channel.ETSY.value,
        ))
        if run is None or publish is None:
            raise ArtworkReplacementError("Replacement reconciliation state changed")
        response = dict(publish.response_data or {})
        replacement = dict(response.get("artwork_replacement") or {})
        prior_status = replacement.get("status")
        stale_lease = (
            prior_status == "reconciling"
            and _reconciliation_lease_is_stale(replacement)
            and bool(stale_attempt_id)
            and replacement.get("reconciliation_attempt_id") == stale_attempt_id
        )
        stale_initial_operation = (
            prior_status == "in_progress"
            and _initial_replacement_is_stale(replacement)
            and bool(stale_attempt_id)
            and replacement.get("operation_id") == stale_attempt_id
        )
        durable_reconciliation = (
            run.status == RunStatus.VERIFICATION_REQUIRED.value
            and publish.status == PublishStatus.RECONCILIATION_REQUIRED.value
        )
        legacy_interrupted = (
            run.status == RunStatus.PUBLISHED.value
            and publish.status == PublishStatus.SUCCEEDED.value
            and stale_initial_operation
        )
        if not (durable_reconciliation or legacy_interrupted):
            raise ArtworkReplacementError("Replacement reconciliation state changed")
        if replacement.get("operation_id") != operation_id or not (
            (prior_status == "failed" and stale_attempt_id is None)
            or stale_lease
            or stale_initial_operation
        ):
            raise ArtworkReplacementError(
                "Replacement is no longer available for reconciliation"
            )
        prior_attempt_id = replacement.get("reconciliation_attempt_id")
        replacement.update({
            "status": "reconciling",
            "stage": "reconciliation_preflight",
            "reconciliation_attempt_id": attempt_id,
            "reconciliation_started_at": _utcnow(),
            "updated_at": _utcnow(),
        })
        response["artwork_replacement"] = replacement
        publish.response_data = response
        publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
        publish.error = "Artwork replacement reconciliation is in progress"
        run.status = RunStatus.VERIFICATION_REQUIRED.value
        run.error = publish.error
        RunRepository(session).audit(
            context.run_id,
            "operator",
            "artwork_replacement.reconciliation_started",
            {
                "operation_id": operation_id,
                "reconciliation_attempt_id": attempt_id,
                "replaced_stale_attempt_id": (
                    prior_attempt_id if stale_lease else (
                        operation_id if stale_initial_operation else None
                    )
                ),
            },
        )
    return attempt_id


def _persist_reconciled_abort(
    context: _ReplacementContext,
    operation_id: str,
    reconciliation_attempt_id: str,
    evidence: dict[str, Any],
) -> None:
    reconciled_at = _utcnow()
    with session_scope() as session:
        session.scalar(
            select(RunRecord.id)
            .where(RunRecord.id == context.run_id)
            .with_for_update()
        )
        run = session.get(RunRecord, context.run_id)
        publish = session.scalar(select(PublishRecord).where(
            PublishRecord.run_id == context.run_id,
            PublishRecord.channel == Channel.ETSY.value,
        ))
        if run is None or publish is None:
            raise ArtworkReplacementError("Replacement disappeared during reconciliation")
        response = dict(publish.response_data or {})
        replacement = dict(response.get("artwork_replacement") or {})
        if (
            replacement.get("operation_id") != operation_id
            or replacement.get("status") != "reconciling"
            or replacement.get("reconciliation_attempt_id") != reconciliation_attempt_id
        ):
            raise ArtworkReplacementError("Replacement reconciliation lease changed")
        replacement.update({
            "status": "aborted",
            "stage": "reconciled_without_print_area_change",
            "updated_at": reconciled_at,
            "reconciled_at": reconciled_at,
            "etsy_inactive_confirmed": False,
            "orphaned_artwork_upload_id": replacement.get("new_artwork_upload_id"),
            "reconciliation_evidence": deepcopy(evidence),
        })
        response["artwork_replacement"] = replacement
        publish.response_data = response
        publish.status = PublishStatus.SUCCEEDED.value
        publish.error = None
        run.status = RunStatus.PUBLISHED.value
        run.error = None
        RunRepository(session).audit(
            context.run_id,
            "operator",
            "artwork_replacement.reconciled_abort",
            {
                "operation_id": operation_id,
                "reconciliation_attempt_id": reconciliation_attempt_id,
                "remote_state_verified": True,
            },
        )


def _persist_success(
    context: _ReplacementContext,
    operation_id: str,
    artwork: bytes,
    artwork_digest: str,
    artifact_key: str,
    deterministic_report: dict[str, Any],
    quality_attestation: dict[str, Any],
    upload_id: str,
    product: dict[str, Any],
    verification: dict[str, Any],
    *,
    reconciliation_attempt_id: str | None = None,
) -> None:
    with Image.open(io.BytesIO(artwork)) as image:
        width, height = image.size
    with session_scope() as session:
        session.scalar(
            select(RunRecord.id).where(RunRecord.id == context.run_id).with_for_update()
        )
        run = session.get(RunRecord, context.run_id)
        publish = session.scalar(
            select(PublishRecord).where(
                PublishRecord.run_id == context.run_id,
                PublishRecord.channel == Channel.ETSY.value,
            )
        )
        if run is None or publish is None:
            raise ArtworkReplacementError("Run disappeared before replacement persistence")
        response = dict(publish.response_data or {})
        replacement = dict(response.get("artwork_replacement") or {})
        if replacement.get("operation_id") != operation_id:
            raise ArtworkReplacementError("Replacement checkpoint changed before persistence")
        if reconciliation_attempt_id is None:
            if (
                replacement.get("status") != "in_progress"
                or replacement.get("reconciliation_attempt_id") is not None
            ):
                raise ArtworkReplacementError(
                    "Replacement lease changed before persistence"
                )
        elif (
            replacement.get("status") != "reconciling"
            or replacement.get("reconciliation_attempt_id") != reconciliation_attempt_id
        ):
            raise ArtworkReplacementError(
                "Replacement reconciliation lease changed before persistence"
            )
        kind = f"{REPLACEMENT_ARTIFACT_KIND_PREFIX}{context.run_version}"
        artifact_metadata = {
            "qa": deterministic_report,
            "raw_deterministic_qa": deterministic_report,
            "storefront_replacement": {
                "operation_id": operation_id,
                "completed_at": _utcnow(),
                "previous_artifact_id": context.previous_artifact_id,
                "quality_attestation": deepcopy(quality_attestation),
                "printify_product_id": context.product_id,
                "etsy_listing_id": context.listing_id,
            },
        }
        revision = int(session.scalar(
            select(func.coalesce(func.max(ArtifactRecord.revision), 0)).where(
                ArtifactRecord.run_id == context.run_id,
                ArtifactRecord.kind == kind,
            )
        ) or 0) + 1
        artifact = RunRepository(session).add_artifact(
            context.run_id,
            kind=kind,
            revision=revision,
            object_key=artifact_key,
            sha256=artwork_digest,
            width=width,
            height=height,
            metadata=artifact_metadata,
        )
        replacement.update({
            "status": "completed",
            "stage": "completed",
            "updated_at": _utcnow(),
            "completed_at": _utcnow(),
            "new_artwork_upload_id": upload_id,
            "new_artifact_id": artifact.id,
            "verification": deepcopy(verification),
        })
        response["artwork_replacement"] = replacement
        response["featured_image_id"] = verification["featured_image_id"]
        response["verification"] = {
            **dict(response.get("verification") or {}),
            "listing_id": context.listing_id,
            "printify_product_id": context.product_id,
            "featured_image_id": verification["featured_image_id"],
            "verified_at": _utcnow(),
            "artwork_sha256": artwork_digest,
        }
        publish.response_data = response
        publish.artwork_upload_id = upload_id
        publish.product_fingerprint = PrintifyClient.product_fingerprint(
            context.template, context.listing, context.quotes, upload_id
        )
        publish.status = PublishStatus.SUCCEEDED.value
        publish.error = None
        run.status = RunStatus.PUBLISHED.value
        run.error = None
        RunRepository(session).save_product_mapping(
            context.run_id, Channel.ETSY.value, context.product_id, product
        )
        RunRepository(session).audit(
            context.run_id,
            "operator",
            "artwork_replacement.completed",
            {
                "operation_id": operation_id,
                "product_id": context.product_id,
                "listing_id": context.listing_id,
                "artifact_id": artifact.id,
                "artwork_sha256": artwork_digest,
            },
        )


async def replace_published_artwork(
    run_id: str,
    artwork: bytes,
    *,
    apply: bool = False,
    confirmation: str | None = None,
    quality_attestation: dict[str, Any] | None = None,
    settings: Settings | None = None,
    printify_client: PrintifyClient | None = None,
    etsy_client: EtsyStorefrontClient | None = None,
    printify_downloader: Downloader | None = None,
    etsy_downloader: Downloader | None = None,
    mockup_attempts: int = 120,
    mockup_interval_seconds: float = 5.0,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """Plan or execute one exact in-place replacement.

    ``apply=False`` performs provider readbacks, order checks, current gallery
    verification, and local artwork QA without changing the product, listing,
    or run. OAuth refresh may still persist a rotated credential. Live
    application additionally requires ``REPLACE <run_id>`` and a digest-bound
    quality attestation.
    """
    if (
        mockup_attempts < 1
        or mockup_attempts > 3600
        or mockup_interval_seconds < 0
        or (mockup_attempts - 1) * mockup_interval_seconds > 3600
    ):
        raise ValueError("mockup polling limits are invalid")
    settings = settings or get_settings()
    context = _load_context(run_id)
    artwork_digest, deterministic_report = _validate_artwork(artwork, context, settings)
    if apply:
        if settings.publish_mode != "live" or settings.provider_mode != "live":
            raise ArtworkReplacementError("Live provider and publish modes are required")
        if confirmation != f"REPLACE {run_id}":
            raise ArtworkReplacementError(f"Confirmation must be exactly: REPLACE {run_id}")
        _validate_quality_attestation(quality_attestation, artwork_digest)

    owns_printify = printify_client is None
    owns_etsy = etsy_client is None
    printify = printify_client or PrintifyClient(settings)
    if etsy_client is None:
        token = await etsy_access_token(settings)
        etsy = EtsyStorefrontClient(settings, access_token=token)
    else:
        etsy = etsy_client
    storage = ArtifactStorage(settings)
    operation_id = str(uuid4())
    checkpoint_started = False
    fulfillment_mutation_started = False
    etsy_deactivation_started = False
    try:
        artifact_key: str | None = None
        preflight_evidence_writer: Callable[[bytes, str], str] | None = None
        if apply:
            storage.ensure_bucket()
            artifact_key, stored_digest = storage.put(
                artwork,
                suffix=f"{run_id}-storefront-replacement-{operation_id}.png",
            )
            if stored_digest != artwork_digest:
                raise ArtworkReplacementError("Stored replacement digest changed")

            def retain_preflight_evidence(data: bytes, content_type: str) -> str:
                return storage.put(
                    data,
                    suffix="png" if content_type == "image/png" else "jpg",
                    content_type=content_type,
                )[0]

            preflight_evidence_writer = retain_preflight_evidence
        product = await printify.product(context.shop_id, context.product_id)
        _verify_product_identity(
            product, context, expected_upload_id=context.artwork_upload_id
        )
        listing = await etsy.listing(context.listing_id)
        verify_etsy_listing(
            listing,
            context.listing_id,
            int(settings.etsy_shop_id or 0),
            context.listing.title,
        )
        _verify_etsy_identity_for_reconciliation(listing, context, settings)
        inventory = await etsy.inventory(context.listing_id)
        verify_etsy_inventory(inventory, product, context.template, context.quotes)
        if not selector_labels_are_exact(inventory):
            raise ArtworkReplacementError("Etsy selectors are not exactly Size and Color")
        orders = await _matching_orders(printify, context)
        etsy_transactions = await _matching_etsy_transactions(etsy, context)
        if orders or etsy_transactions:
            raise ArtworkReplacementError(
                "Refusing artwork replacement: product has "
                f"{len(orders)} matching Printify order(s) and "
                f"{len(etsy_transactions)} matching Etsy transaction(s)"
            )
        baseline_mockups = await prepare_mockups(
            product,
            context.template,
            downloader=printify_downloader,
            evidence_writer=preflight_evidence_writer,
        )
        baseline_verification = await verify_etsy_mockups(
            etsy,
            context.listing_id,
            context.template,
            baseline_mockups,
            inventory,
            downloader=etsy_downloader or download_etsy_image,
            evidence_writer=preflight_evidence_writer,
        )
        try:
            verified_gallery = deepcopy(baseline_verification["final_gallery"])
            verified_variation_images = deepcopy(
                baseline_verification["final_variation_images"]
            )
            verified_inventory = deepcopy(baseline_verification["final_inventory"])
        except KeyError as exc:
            raise ArtworkReplacementError(
                "Etsy mockup verification did not return a stable final snapshot"
            ) from exc
        if not isinstance(verified_gallery, list) or not isinstance(
            verified_variation_images, list
        ) or not isinstance(verified_inventory, dict):
            raise ArtworkReplacementError(
                "Etsy mockup verification returned an invalid final snapshot"
            )
        verify_etsy_inventory(
            verified_inventory, product, context.template, context.quotes
        )
        if not selector_labels_are_exact(verified_inventory):
            raise ArtworkReplacementError("Etsy selectors are not exactly Size and Color")
        prior_completed = _existing_completed(context.publish_response, artwork_digest)
        if prior_completed is not None:
            return {
                "status": "already_completed",
                "run_id": run_id,
                "printify_product_id": context.product_id,
                "etsy_listing_id": context.listing_id,
                "artwork_sha256": artwork_digest,
                "operation": prior_completed,
            }
        snapshot: dict[str, Any] = {
            "captured_at": _utcnow(),
            "publish": {
                "status": PublishStatus.SUCCEEDED.value,
                "artwork_upload_id": context.artwork_upload_id,
                "product_fingerprint": context.product_fingerprint,
            },
            "printify": {
                "print_areas": deepcopy(product["print_areas"]),
                "invariants": _product_invariants(product),
                "invariants_sha256": _digest(_product_invariants(product)),
            },
            "etsy": {
                "listing_state": listing.get("state"),
                "inventory": verified_inventory,
                "inventory_sha256": _digest(verified_inventory),
                "gallery": verified_gallery,
                "variation_images": verified_variation_images,
                "baseline_verification": baseline_verification,
            },
        }
        result = {
            "status": "ready" if not apply else "applying",
            "run_id": run_id,
            "printify_product_id": context.product_id,
            "etsy_listing_id": context.listing_id,
            "artwork_sha256": artwork_digest,
            "matching_orders": 0,
            "matching_etsy_transactions": 0,
            "mockup_count": len(baseline_mockups),
            "deterministic_qa": deterministic_report,
            "changes": [
                "replace Printify print-area image IDs in place",
                "replace the existing Etsy gallery in place",
                "relink Color variations and verify pixels",
            ],
        }
        if not apply:
            return result

        assert artifact_key is not None
        assert quality_attestation is not None
        _initial_checkpoint(
            context,
            operation_id,
            artwork_digest,
            deterministic_report,
            quality_attestation,
            snapshot,
            artifact_key,
        )
        checkpoint_started = True
        upload = await printify.upload_image(
            f"merch-{run_id}-replacement-{artwork_digest[:12]}.png", artwork
        )
        upload_id = str(upload.get("id") or "")
        if not upload_id:
            raise ArtworkReplacementError("Printify did not return an artwork upload ID")
        _checkpoint(
            context,
            operation_id,
            stage="artwork_uploaded",
            new_artwork_upload_id=upload_id,
        )
        # Make the listing unavailable before fulfillment artwork changes.  This
        # prevents an active old-gallery/new-print mismatch while Printify renders.
        etsy_deactivation_started = True
        await etsy.update_listing(context.listing_id, {"state": "inactive"})
        inactive = await etsy.listing(context.listing_id)
        if inactive.get("state") != "inactive":
            raise ArtworkReplacementError("Etsy did not make the listing inactive")
        _checkpoint(context, operation_id, stage="etsy_inactive")
        # Close the active-listing race with both fulfillment systems. Etsy is
        # authoritative for a just-placed transaction that may not have reached
        # Printify yet; Printify remains the authoritative fulfillment history.
        late_orders = await _matching_orders(printify, context)
        late_transactions = await _matching_etsy_transactions(etsy, context)
        if late_orders or late_transactions:
            raise ArtworkReplacementError(
                "Refusing artwork replacement after deactivation: product acquired "
                f"{len(late_orders)} matching Printify order(s) and "
                f"{len(late_transactions)} matching Etsy transaction(s)"
            )

        # Deactivation closes checkout, but it is not a lock on either provider.
        # Re-read every identity-bearing field and the exact verified storefront
        # snapshot immediately before the first fulfillment mutation.
        current_product = await printify.product(context.shop_id, context.product_id)
        _verify_product_identity(
            current_product,
            context,
            expected_upload_id=context.artwork_upload_id,
        )
        if _product_invariants(current_product) != snapshot["printify"]["invariants"]:
            raise ArtworkReplacementError(
                "Printify product identity, variants, or copy changed during preflight"
            )
        if replacement_print_areas(
            current_product["print_areas"],
            context.template.position,
            context.artwork_upload_id,
        ) != replacement_print_areas(
            snapshot["printify"]["print_areas"],
            context.template.position,
            context.artwork_upload_id,
        ):
            raise ArtworkReplacementError(
                "Printify print-area placement changed during preflight"
            )
        current_listing = await etsy.listing(context.listing_id)
        _verify_etsy_identity_for_reconciliation(current_listing, context, settings)
        if current_listing.get("state") != "inactive":
            raise ArtworkReplacementError("Etsy did not remain inactive")
        current_inventory = await etsy.inventory(context.listing_id)
        if _digest(current_inventory) != snapshot["etsy"]["inventory_sha256"]:
            raise ArtworkReplacementError("Etsy inventory changed during preflight")
        verify_etsy_inventory(
            current_inventory, current_product, context.template, context.quotes
        )
        if not selector_labels_are_exact(current_inventory):
            raise ArtworkReplacementError("Etsy selectors are not exactly Size and Color")
        current_gallery = await etsy.images(context.listing_id)
        current_variation_images = await etsy.variation_images(context.listing_id)
        if (
            _gallery_key(current_gallery) != _gallery_key(snapshot["etsy"]["gallery"])
            or _variation_key(current_variation_images)
            != _variation_key(snapshot["etsy"]["variation_images"])
        ):
            raise ArtworkReplacementError(
                "Etsy gallery or color-photo links changed during preflight"
            )
        product = current_product
        inventory = current_inventory
        final_orders = await _matching_orders(printify, context)
        final_transactions = await _matching_etsy_transactions(etsy, context)
        if final_orders or final_transactions:
            raise ArtworkReplacementError(
                "Refusing fulfillment change: a matching order or transaction appeared "
                "during the final state readback"
            )
        print_areas = replacement_print_areas(
            product["print_areas"], context.template.position, upload_id
        )
        fulfillment_mutation_started = True
        try:
            await printify.update_product_print_areas(
                context.shop_id, context.product_id, print_areas
            )
        except ProviderConfigurationError:
            # Even a rejected request can be ambiguous at a proxy boundary. Only
            # restore the listing when a fresh read proves the exact old state.
            try:
                rejected_readback = await printify.product(
                    context.shop_id, context.product_id
                )
                _verify_product_identity(
                    rejected_readback,
                    context,
                    expected_upload_id=context.artwork_upload_id,
                )
                if (
                    _product_invariants(rejected_readback) == _product_invariants(product)
                    and replacement_print_areas(
                        rejected_readback["print_areas"],
                        context.template.position,
                        context.artwork_upload_id,
                    )
                    == replacement_print_areas(
                        product["print_areas"],
                        context.template.position,
                        context.artwork_upload_id,
                    )
                ):
                    fulfillment_mutation_started = False
            except Exception:
                pass
            raise
        _checkpoint(context, operation_id, stage="print_areas_updated")
        def evidence_writer(data: bytes, content_type: str) -> str:
            return storage.put(
                data,
                suffix="png" if content_type == "image/png" else "jpg",
                content_type=content_type,
            )[0]
        replacement_product, replacement_mockups = await _wait_for_replacement_mockups(
            printify,
            context,
            upload_id,
            baseline_mockups,
            downloader=printify_downloader,
            evidence_writer=evidence_writer,
            checkpoint=lambda **updates: _checkpoint(
                context, operation_id, **updates
            ),
            attempts=mockup_attempts,
            interval_seconds=mockup_interval_seconds,
            sleep=sleep,
        )
        if _product_invariants(replacement_product) != _product_invariants(product):
            raise ArtworkReplacementError(
                "Printify changed product identity, variants, copy, or storefront link"
            )
        _checkpoint(
            context,
            operation_id,
            stage="replacement_mockups_prepared",
            replacement_mockups=[deepcopy(item.evidence) for item in replacement_mockups],
        )

        delayed_orders = await _matching_orders(printify, context)
        delayed_transactions = await _matching_etsy_transactions(etsy, context)
        if delayed_orders or delayed_transactions:
            raise ArtworkReplacementError(
                "Refusing gallery replacement: a matching order or transaction "
                "appeared while Printify rendered the new mockups"
            )
        replacement_product = await printify.product(
            context.shop_id, context.product_id
        )
        _verify_product_identity(
            replacement_product, context, expected_upload_id=upload_id
        )
        if _product_invariants(replacement_product) != snapshot["printify"]["invariants"]:
            raise ArtworkReplacementError(
                "Printify product changed while replacement mockups were rendered"
            )
        expected_replacement_areas = replacement_print_areas(
            snapshot["printify"]["print_areas"],
            context.template.position,
            upload_id,
        )
        if replacement_print_areas(
            replacement_product["print_areas"],
            context.template.position,
            upload_id,
        ) != expected_replacement_areas:
            raise ArtworkReplacementError(
                "Printify print-area placement changed while mockups were rendered"
            )
        pre_gallery_listing = await etsy.listing(context.listing_id)
        _verify_etsy_identity_for_reconciliation(
            pre_gallery_listing, context, settings
        )
        if pre_gallery_listing.get("state") != "inactive":
            raise ArtworkReplacementError(
                "Etsy did not remain inactive while mockups were rendered"
            )
        inventory = await etsy.inventory(context.listing_id)
        if _digest(inventory) != snapshot["etsy"]["inventory_sha256"]:
            raise ArtworkReplacementError(
                "Etsy inventory changed while mockups were rendered"
            )
        verify_etsy_inventory(
            inventory, replacement_product, context.template, context.quotes
        )
        if not selector_labels_are_exact(inventory):
            raise ArtworkReplacementError("Etsy selectors are not exactly Size and Color")
        pre_gallery = await etsy.images(context.listing_id)
        pre_gallery_links = await etsy.variation_images(context.listing_id)
        if (
            _gallery_key(pre_gallery) != _gallery_key(snapshot["etsy"]["gallery"])
            or _variation_key(pre_gallery_links)
            != _variation_key(snapshot["etsy"]["variation_images"])
        ):
            raise ArtworkReplacementError(
                "Etsy gallery or color-photo links changed while mockups were rendered"
            )
        image_ids = await _replace_etsy_gallery(
            etsy,
            context,
            inventory,
            replacement_mockups,
            lambda **updates: _checkpoint(context, operation_id, **updates),
            settings=settings,
            expected_gallery=pre_gallery,
            expected_variation_images=pre_gallery_links,
        )
        verification = await verify_etsy_mockups(
            etsy,
            context.listing_id,
            context.template,
            replacement_mockups,
            await etsy.inventory(context.listing_id),
            image_ids=image_ids,
            downloader=etsy_downloader or download_etsy_image,
            evidence_writer=evidence_writer,
        )
        _checkpoint(
            context,
            operation_id,
            stage="inactive_storefront_verified",
            replacement_verification=verification,
        )
        pre_activation_orders = await _matching_orders(printify, context)
        pre_activation_transactions = await _matching_etsy_transactions(etsy, context)
        if pre_activation_orders or pre_activation_transactions:
            raise ArtworkReplacementError(
                "Refusing reactivation: a matching order or transaction appeared "
                "during gallery replacement"
            )
        await etsy.update_listing(context.listing_id, {"state": "active"})
        final_listing = await etsy.listing(context.listing_id)
        verify_etsy_listing(
            final_listing,
            context.listing_id,
            int(settings.etsy_shop_id or 0),
            context.listing.title,
        )
        _verify_etsy_identity_for_reconciliation(final_listing, context, settings)
        final_inventory = await etsy.inventory(context.listing_id)
        verify_etsy_inventory(
            final_inventory, replacement_product, context.template, context.quotes
        )
        final_verification = await verify_etsy_mockups(
            etsy,
            context.listing_id,
            context.template,
            replacement_mockups,
            final_inventory,
            image_ids=image_ids,
            downloader=etsy_downloader or download_etsy_image,
            evidence_writer=evidence_writer,
        )
        final_product = await printify.product(context.shop_id, context.product_id)
        _verify_product_identity(final_product, context, expected_upload_id=upload_id)
        if _product_invariants(final_product) != _product_invariants(product):
            raise ArtworkReplacementError("Printify product changed during final verification")
        _checkpoint(
            context,
            operation_id,
            stage="storefront_verified",
            replacement_verification=final_verification,
        )
        _persist_success(
            context,
            operation_id,
            artwork,
            artwork_digest,
            artifact_key,
            deterministic_report,
            quality_attestation,
            upload_id,
            final_product,
            final_verification,
        )
        return {
            **result,
            "status": "completed",
            "operation_id": operation_id,
            "new_artwork_upload_id": upload_id,
            "new_etsy_image_ids": image_ids,
            "featured_image_id": final_verification["featured_image_id"],
        }
    except Exception as exc:
        if checkpoint_started:
            safety_error: str | None = None
            safe_state_confirmed = False
            inactive_confirmed = False
            if fulfillment_mutation_started and etsy_deactivation_started:
                try:
                    await etsy.update_listing(context.listing_id, {"state": "inactive"})
                    inactive_confirmed = (
                        await etsy.listing(context.listing_id)
                    ).get("state") == "inactive"
                    safe_state_confirmed = inactive_confirmed
                    if not inactive_confirmed:
                        safety_error = "inactive state could not be confirmed"
                except Exception as inactive_exc:  # fail-closed checkpoint must survive provider errors
                    safety_error = str(inactive_exc)
            elif etsy_deactivation_started:
                try:
                    await _verify_exact_old_remote_state(
                        context,
                        snapshot,
                        settings,
                        printify,
                        etsy,
                        baseline_mockups,
                        expected_listing_state="inactive",
                        downloader=etsy_downloader,
                    )
                    await etsy.update_listing(context.listing_id, {"state": "active"})
                    await _verify_exact_old_remote_state(
                        context,
                        snapshot,
                        settings,
                        printify,
                        etsy,
                        baseline_mockups,
                        expected_listing_state="active",
                        downloader=etsy_downloader,
                    )
                    safe_state_confirmed = True
                except Exception as active_exc:
                    safety_error = str(active_exc)
                    try:
                        await etsy.update_listing(context.listing_id, {"state": "inactive"})
                        inactive_confirmed = (
                            await etsy.listing(context.listing_id)
                        ).get("state") == "inactive"
                        if not inactive_confirmed:
                            safety_error += "; inactive state could not be confirmed"
                    except Exception as inactive_exc:
                        safety_error += f"; safety deactivation failed: {inactive_exc}"
            else:
                safe_state_confirmed = True
            detail = str(exc)
            if safety_error:
                action = "deactivation" if fulfillment_mutation_started else "reactivation"
                detail += f"; Etsy safety {action} failed: {safety_error}"
            failed = fulfillment_mutation_started or not safe_state_confirmed
            try:
                _checkpoint(
                    context,
                    operation_id,
                    stage=("reconciliation_required" if failed else "aborted"),
                    status=("failed" if failed else "aborted"),
                    error=detail,
                    etsy_inactive_confirmed=inactive_confirmed,
                )
            except Exception:
                pass
        if isinstance(exc, ArtworkReplacementError):
            raise
        if isinstance(exc, StorefrontVerificationError):
            raise ArtworkReplacementError(str(exc)) from exc
        raise ArtworkReplacementError(f"Artwork replacement failed: {exc}") from exc
    finally:
        if owns_etsy:
            await etsy.close()
        if owns_printify:
            await printify.close()


async def reconcile_published_artwork(
    run_id: str,
    *,
    apply: bool = False,
    confirmation: str | None = None,
    settings: Settings | None = None,
    printify_client: PrintifyClient | None = None,
    etsy_client: EtsyStorefrontClient | None = None,
) -> dict[str, Any]:
    """Inspect or recover one failed replacement from its durable checkpoint.

    Automation is intentionally limited to two provable states: an entirely
    unchanged old storefront can be reactivated and aborted, while an inactive
    storefront with the exact old gallery (or its checkpointed final sentinel)
    can finish a Printify replacement whose mockups were already retained.
    An authorized apply attempt deactivates every mixed or partial state for
    manual reconciliation; inspection alone never changes listing state.
    """
    settings = settings or get_settings()
    context, replacement = _failed_replacement_context(run_id)
    stale_attempt_id: str | None = None
    if replacement.get("status") == "reconciling":
        stale_attempt_id = str(replacement.get("reconciliation_attempt_id") or "") or None
    elif replacement.get("status") == "in_progress":
        stale_attempt_id = str(replacement.get("operation_id") or "") or None
    required_confirmation = (
        f"TAKEOVER {run_id} {stale_attempt_id}"
        if stale_attempt_id is not None
        else f"RECONCILE {run_id}"
    )
    if apply:
        if settings.publish_mode != "live" or settings.provider_mode != "live":
            raise ArtworkReplacementError("Live provider and publish modes are required")
        if confirmation != required_confirmation:
            raise ArtworkReplacementError(
                f"Confirmation must be exactly: {required_confirmation}"
            )
    operation_id = str(replacement["operation_id"])
    owns_printify = printify_client is None
    owns_etsy = etsy_client is None
    printify = printify_client or PrintifyClient(settings)
    if etsy_client is None:
        token = await etsy_access_token(settings)
        etsy = EtsyStorefrontClient(settings, access_token=token)
    else:
        etsy = etsy_client
    storage = ArtifactStorage(settings)
    reconciliation_attempt_id: str | None = None
    action: str | None = None
    try:
        if apply:
            reconciliation_attempt_id = _acquire_reconciliation_lease(
                context,
                operation_id,
                stale_attempt_id=stale_attempt_id,
            )
        inspection = await _inspect_failed_replacement(
            context, replacement, settings, printify, etsy
        )
        action = inspection.action
        artwork: bytes | None = None
        artwork_digest = str(replacement["artwork_sha256"])
        deterministic_report: dict[str, Any] | None = None
        if action == "resume":
            artwork = await asyncio.to_thread(
                storage.get, str(replacement["artifact_object_key"])
            )
            actual_digest, deterministic_report = _validate_artwork(
                artwork, context, settings
            )
            if actual_digest != artwork_digest:
                raise ArtworkReplacementError(
                    "Stored replacement artwork differs from the checkpoint"
                )
            _validate_quality_attestation(
                replacement.get("quality_attestation"), artwork_digest
            )
        result = {
            **inspection.summary,
            "status": "ready" if not apply else "reconciling",
            "required_confirmation": required_confirmation,
            "stale_lease_takeover": stale_attempt_id is not None,
        }
        if not apply:
            return result

        assert reconciliation_attempt_id is not None

        def evidence_writer(data: bytes, content_type: str) -> str:
            return storage.put(
                data,
                suffix=(
                    f"{run_id}-reconciliation-{reconciliation_attempt_id}.png"
                    if content_type == "image/png"
                    else f"{run_id}-reconciliation-{reconciliation_attempt_id}.jpg"
                ),
                content_type=content_type,
            )[0]

        current = await _inspect_failed_replacement(
            context,
            replacement,
            settings,
            printify,
            etsy,
            evidence_writer=evidence_writer if action == "resume" else None,
        )
        if current.action != action:
            raise ArtworkReplacementError(
                "Replacement state changed while reconciliation was being reserved"
            )
        if action == "abort":
            if current.listing.get("state") != "active":
                await etsy.update_listing(context.listing_id, {"state": "active"})
            final = await _inspect_failed_replacement(
                context, replacement, settings, printify, etsy
            )
            if final.action != "abort" or final.listing.get("state") != "active":
                raise ArtworkReplacementError(
                    "Old storefront could not be fully reverified after activation"
                )
            verify_etsy_listing(
                final.listing,
                context.listing_id,
                int(settings.etsy_shop_id or 0),
                context.listing.title,
            )
            evidence = {**final.summary, "reconciliation_attempt_id": reconciliation_attempt_id}
            _persist_reconciled_abort(
                context,
                operation_id,
                reconciliation_attempt_id,
                evidence,
            )
            return {**result, **evidence, "status": "aborted"}

        assert artwork is not None
        assert deterministic_report is not None
        _checkpoint(
            context,
            operation_id,
            stage="reconciliation_gallery_resume",
            status="reconciling",
            reconciliation_attempt_id=reconciliation_attempt_id,
            reconciliation_preflight=deepcopy(current.summary),
        )
        pre_gallery_orders = await _matching_orders(printify, context)
        pre_gallery_transactions = await _matching_etsy_transactions(etsy, context)
        if pre_gallery_orders or pre_gallery_transactions:
            raise ArtworkReplacementError(
                "Artwork replacement cannot continue after a matching order or transaction"
            )
        pre_gallery_product = await printify.product(
            context.shop_id, context.product_id
        )
        _verify_product_identity(
            pre_gallery_product,
            context,
            expected_upload_id=str(replacement["new_artwork_upload_id"]),
        )
        if _digest(_product_invariants(pre_gallery_product)) != str(
            replacement["snapshot"]["printify"]["invariants_sha256"]
        ):
            raise ArtworkReplacementError(
                "Printify product changed before gallery reconciliation"
            )
        pre_gallery_listing = await etsy.listing(context.listing_id)
        _verify_etsy_identity_for_reconciliation(
            pre_gallery_listing, context, settings
        )
        if pre_gallery_listing.get("state") != "inactive":
            raise ArtworkReplacementError(
                "Etsy must remain inactive during gallery reconciliation"
            )
        pre_gallery_inventory = await etsy.inventory(context.listing_id)
        if _digest(pre_gallery_inventory) != str(
            replacement["snapshot"]["etsy"]["inventory_sha256"]
        ):
            raise ArtworkReplacementError(
                "Etsy inventory changed before gallery reconciliation"
            )
        verify_etsy_inventory(
            pre_gallery_inventory,
            pre_gallery_product,
            context.template,
            context.quotes,
        )
        image_ids = await _replace_etsy_gallery(
            etsy,
            context,
            pre_gallery_inventory,
            current.replacement_mockups,
            lambda **updates: _checkpoint(
                context,
                operation_id,
                status="reconciling",
                reconciliation_attempt_id=reconciliation_attempt_id,
                **updates,
            ),
            settings=settings,
            expected_gallery=current.gallery,
            expected_variation_images=current.variation_images,
        )
        verification = await verify_etsy_mockups(
            etsy,
            context.listing_id,
            context.template,
            current.replacement_mockups,
            await etsy.inventory(context.listing_id),
            image_ids=image_ids,
            downloader=download_etsy_image,
            evidence_writer=evidence_writer,
        )
        _checkpoint(
            context,
            operation_id,
            stage="reconciliation_inactive_storefront_verified",
            status="reconciling",
            reconciliation_attempt_id=reconciliation_attempt_id,
            replacement_verification=verification,
        )
        pre_activation_orders = await _matching_orders(printify, context)
        pre_activation_transactions = await _matching_etsy_transactions(etsy, context)
        if pre_activation_orders or pre_activation_transactions:
            raise ArtworkReplacementError(
                "Artwork replacement cannot reactivate after a matching order or transaction"
            )
        await etsy.update_listing(context.listing_id, {"state": "active"})
        final_listing = await etsy.listing(context.listing_id)
        verify_etsy_listing(
            final_listing,
            context.listing_id,
            int(settings.etsy_shop_id or 0),
            context.listing.title,
        )
        _verify_etsy_identity_for_reconciliation(final_listing, context, settings)
        final_product = await printify.product(context.shop_id, context.product_id)
        _verify_product_identity(
            final_product,
            context,
            expected_upload_id=str(replacement["new_artwork_upload_id"]),
        )
        if _digest(_product_invariants(final_product)) != str(
            replacement["snapshot"]["printify"]["invariants_sha256"]
        ):
            raise ArtworkReplacementError(
                "Printify product changed during reconciliation"
            )
        final_inventory = await etsy.inventory(context.listing_id)
        verify_etsy_inventory(
            final_inventory, final_product, context.template, context.quotes
        )
        final_verification = await verify_etsy_mockups(
            etsy,
            context.listing_id,
            context.template,
            current.replacement_mockups,
            final_inventory,
            image_ids=image_ids,
            downloader=download_etsy_image,
            evidence_writer=evidence_writer,
        )
        _checkpoint(
            context,
            operation_id,
            stage="reconciliation_storefront_verified",
            status="reconciling",
            reconciliation_attempt_id=reconciliation_attempt_id,
            replacement_verification=final_verification,
        )
        _persist_success(
            context,
            operation_id,
            artwork,
            artwork_digest,
            str(replacement["artifact_object_key"]),
            deterministic_report,
            replacement["quality_attestation"],
            str(replacement["new_artwork_upload_id"]),
            final_product,
            final_verification,
            reconciliation_attempt_id=reconciliation_attempt_id,
        )
        return {
            **result,
            "status": "completed",
            "reconciliation_attempt_id": reconciliation_attempt_id,
            "new_etsy_image_ids": image_ids,
            "featured_image_id": final_verification["featured_image_id"],
        }
    except Exception as exc:
        if reconciliation_attempt_id is not None:
            detail = str(exc)
            inactive_confirmed = False
            try:
                await etsy.update_listing(context.listing_id, {"state": "inactive"})
                inactive_confirmed = (
                    await etsy.listing(context.listing_id)
                ).get("state") == "inactive"
            except Exception as inactive_exc:
                detail += f"; Etsy safety deactivation failed: {inactive_exc}"
            try:
                _checkpoint(
                    context,
                    operation_id,
                    stage="reconciliation_required",
                    status="failed",
                    error=detail,
                    reconciliation_attempt_id=reconciliation_attempt_id,
                    etsy_inactive_confirmed=inactive_confirmed,
                )
            except Exception:
                pass
        if isinstance(exc, ArtworkReplacementError):
            raise
        if isinstance(exc, StorefrontVerificationError):
            raise ArtworkReplacementError(str(exc)) from exc
        raise ArtworkReplacementError(
            f"Artwork replacement reconciliation failed: {exc}"
        ) from exc
    finally:
        if owns_etsy:
            await etsy.close()
        if owns_printify:
            await printify.close()


async def replace_published_artwork_file(
    run_id: str,
    artwork_file: Path,
    *,
    apply: bool = False,
    confirmation: str | None = None,
    quality_attestation_file: Path | None = None,
    mockup_timeout_seconds: float = 600.0,
    mockup_interval_seconds: float = 5.0,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Filesystem adapter used by the operator CLI."""
    if mockup_timeout_seconds <= 0 or mockup_interval_seconds <= 0:
        raise ArtworkReplacementError("Mockup timeout and interval must be positive")
    if not await asyncio.to_thread(artwork_file.is_file):
        raise ArtworkReplacementError("Artwork file does not exist")
    attestation: dict[str, Any] | None = None
    if quality_attestation_file is not None:
        try:
            value = json.loads(await asyncio.to_thread(quality_attestation_file.read_text))
        except (OSError, ValueError) as exc:
            raise ArtworkReplacementError("Quality attestation is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ArtworkReplacementError("Quality attestation must be a JSON object")
        attestation = value
    return await replace_published_artwork(
        run_id,
        await asyncio.to_thread(artwork_file.read_bytes),
        apply=apply,
        confirmation=confirmation,
        quality_attestation=attestation,
        mockup_attempts=max(1, math.ceil(mockup_timeout_seconds / mockup_interval_seconds)),
        mockup_interval_seconds=mockup_interval_seconds,
        settings=settings,
    )
