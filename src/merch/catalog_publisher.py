"""Checkpointed Printify-to-Etsy publication for catalog v2 packages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from merch.config import Settings, get_settings
from merch.database import session_scope
from merch.domain.etsy_inventory import verify_generic_etsy_inventory
from merch.domain.originality import (
    evaluate_originality,
    make_contact_sheet,
    perceptual_hash_distance,
)
from merch.models import RunRecord
from merch.repository import CatalogRepository, ConfigurationRepository, RunRepository
from merch.schemas import (
    Channel,
    MarketplaceListing,
    OriginalityReport,
    PriceDecision,
    ProductOpportunity,
    ProductPlanV2,
    PublishStatus,
    RunStatus,
    SEOEvidence,
)
from merch.services.browser_session import collect_printify_costs
from merch.services.etsy_auth import etsy_access_token
from merch.services.etsy_catalog_publisher import publish_direct_catalog_etsy
from merch.services.openai_service import OpenAIService
from merch.services.printify import (
    AmbiguousCreateError,
    PrintifyClient,
    channel_shop,
)
from merch.services.storage import ArtifactStorage
from merch.services.storefront import (
    EtsyStorefrontClient,
    StorefrontVerificationError,
    download_mockup,
    verify_etsy_listing,
)


def _package_digest(record: RunRecord) -> str:
    if not all(
        [
            record.selected_opportunity,
            record.product_plan,
            record.reference_analysis,
            record.ip_report,
            record.originality_report,
            record.seo_evidence,
            record.price_decisions,
            record.listings,
            record.qa_report,
        ]
    ):
        raise ValueError("catalog publication package is incomplete")
    listings = record.listings
    qa_report = record.qa_report
    assert listings is not None and qa_report is not None
    listing = next(item for item in listings["listings"] if item["channel"] == Channel.ETSY.value)
    package = {
        "opportunity": record.selected_opportunity,
        "product_plan": record.product_plan,
        "reference_analysis": record.reference_analysis,
        "ip_report": record.ip_report,
        "originality": record.originality_report,
        "seo": record.seo_evidence,
        "prices": record.price_decisions,
        "listing": listing,
        "gates": qa_report["gates"],
    }
    return hashlib.sha256(
        json.dumps(package, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _verify_printify_catalog_product(
    product: dict[str, Any], plan: ProductPlanV2, prices: list[PriceDecision]
) -> None:
    expected_prices = {item.variant_id: item.item_price_cents for item in prices}
    enabled = {
        int(item["id"]): item for item in product.get("variants", []) if item.get("is_enabled")
    }
    if set(enabled) != {item.variant_id for item in plan.variants}:
        raise StorefrontVerificationError("Printify enabled catalog variants changed")
    if any(int(enabled[key].get("price", -1)) != value for key, value in expected_prices.items()):
        raise StorefrontVerificationError("Printify catalog prices changed")
    defaults = {key for key, item in enabled.items() if item.get("is_default")}
    if defaults != {plan.featured_variant_id}:
        raise StorefrontVerificationError("Printify catalog default variant changed")


async def _etsy_image(url: str, http: httpx.AsyncClient) -> bytes:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (
        host.endswith(".etsystatic.com")
        or host == "etsystatic.com"
        or host.endswith(".etsy.com")
        or host == "etsy.com"
    ):
        raise StorefrontVerificationError("Etsy served image is outside Etsy")
    response = await http.get(url)
    response.raise_for_status()
    if not 0 < len(response.content) <= 15_000_000:
        raise StorefrontVerificationError("Etsy served image size is invalid")
    return response.content


async def _verify_served_image(
    printify_product: dict[str, Any],
    etsy_images: list[dict[str, Any]],
    *,
    expected_count: int,
    source_urls: list[str] | None = None,
) -> dict[str, Any]:
    sources = source_urls or [
        str(item.get("src")) for item in printify_product.get("images", []) if item.get("src")
    ]
    served_items = sorted(etsy_images, key=lambda item: int(item.get("rank") or 10_000))
    served = [
        str(item.get("url_fullxfull") or item.get("url_570xN") or "") for item in served_items
    ]
    if expected_count < 1 or len(sources) < expected_count or len(served) < expected_count:
        raise StorefrontVerificationError("Printify or Etsy gallery images are missing")
    checks = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as http:
        for index in range(expected_count):
            source, _ = await download_mockup(sources[index])
            destination = await _etsy_image(served[index], http)
            distance = perceptual_hash_distance(source, destination)
            if distance > 12:
                raise StorefrontVerificationError(
                    f"Etsy served gallery image {index + 1} differs from Printify"
                )
            checks.append(
                {
                    "rank": index + 1,
                    "printify_source": sources[index],
                    "etsy_image_id": int(served_items[index].get("listing_image_id") or 0),
                    "perceptual_hash_distance": distance,
                }
            )
    return {
        "count": len(checks),
        "checks": checks,
    }


async def publish_catalog_run(run_id: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    storage = ArtifactStorage(settings)
    with session_scope() as session:
        repository = RunRepository(session)
        run = repository.get(run_id, full=True)
        existing = next(
            (item for item in run.publishes if item.channel == Channel.ETSY.value), None
        )
        if (
            run.status == RunStatus.PUBLISHED.value
            and existing
            and existing.status in {PublishStatus.SUCCEEDED.value, PublishStatus.DRY_RUN.value}
        ):
            return existing.status
        if run.status != RunStatus.PUBLISHING.value:
            raise ValueError("catalog run is not approved for publication")
        if run.pipeline_version != 2:
            raise ValueError("catalog publisher only accepts v2 run payloads")
        expected_digest = str((run.qa_report or {}).get("package_digest") or "")
        if not expected_digest or _package_digest(run) != expected_digest:
            raise ValueError("catalog package digest changed after system approval")
        if not all((run.qa_report or {}).get("gates", {}).values()):
            raise ValueError("one or more catalog publication gates did not pass")
        originality = OriginalityReport.model_validate(run.originality_report)
        if not originality.passed or originality.copying_risk > 20:
            raise ValueError("originality package is not eligible for publication")
        opportunity = ProductOpportunity.model_validate(run.selected_opportunity)
        plan = ProductPlanV2.model_validate(run.product_plan)
        prices = [PriceDecision.model_validate(item) for item in run.price_decisions or []]
        listings = run.listings
        assert listings is not None
        listing = MarketplaceListing.model_validate(listings["listings"][0])
        SEOEvidence.model_validate(run.seo_evidence)
        template = ConfigurationRepository(session).get_template()
        product = CatalogRepository(session).get(plan.blueprint_id, plan.print_provider_id)
        artifacts = {
            item.id: item
            for item in run.artifacts
            if item.id in {surface.artifact_id for surface in plan.surface_artworks}
        }
        reference_prefix = f"v2-refimg-{opportunity.opportunity_id[:8]}-"
        reference_artifacts = sorted(
            (
                item
                for item in run.artifacts
                if item.kind.startswith(reference_prefix)
                and item.metadata_json.get("reference_listing_id")
            ),
            key=lambda item: item.kind,
        )
        if existing and existing.status in {
            PublishStatus.SUCCEEDED.value,
            PublishStatus.DRY_RUN.value,
        }:
            return existing.status
        progress = dict(existing.response_data or {}) if existing else {}
        existing_product_id = existing.printify_product_id if existing else None
    if listing.channel != Channel.ETSY:
        raise ValueError("v2 publication is Etsy-only")
    if set(artifacts) != {item.artifact_id for item in plan.surface_artworks}:
        raise ValueError("approved surface artwork is missing")

    # This is intentionally recollected at the publication boundary. Search or API
    # estimates never substitute for the authenticated account dashboard.
    current_costs = await collect_printify_costs(product, settings)
    approved_costs = {item.variant_id: item.production_cost_cents for item in prices}
    if any(current_costs.get(key) != value for key, value in approved_costs.items()):
        with session_scope() as session:
            repository = RunRepository(session)
            repository.reject_opportunity(
                run_id,
                opportunity.opportunity_id,
                "Printify costs changed at the prepublication refresh",
            )
            repository.status(
                run_id,
                RunStatus.RANKING,
                "Printify costs changed at the prepublication refresh",
            )
        return "hard_gate_failed"

    printify = PrintifyClient(settings)
    try:
        shop_id = channel_shop(template, Channel.ETSY)
        upload_ids = {
            str(key): str(value)
            for key, value in (progress.get("surface_upload_ids") or {}).items()
        }
        for surface in plan.surface_artworks:
            if surface.surface_signature in upload_ids:
                continue
            artifact = artifacts[surface.artifact_id]
            upload = await printify.upload_image(
                f"merch-{run_id}-{surface.artifact_id}.png",
                storage.get(artifact.object_key),
            )
            upload_ids[surface.surface_signature] = str(upload["id"])
        fingerprint = printify.catalog_product_fingerprint(plan, listing, prices, upload_ids)
        with session_scope() as session:
            session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
            publish = RunRepository(session).publish_record(run_id, Channel.ETSY.value, fingerprint)
            if publish.status in {
                PublishStatus.SUCCEEDED.value,
                PublishStatus.DRY_RUN.value,
            }:
                return publish.status
            progress = {**dict(publish.response_data or {}), "surface_upload_ids": upload_ids}
            publish.artwork_upload_id = next(iter(upload_ids.values()))
            publish.response_data = progress
            publish.status = PublishStatus.CREATING.value
            publish.error = None
            existing_product_id = publish.printify_product_id or existing_product_id

        product_result: dict[str, Any]
        if existing_product_id:
            product_result = {"id": existing_product_id}
        elif progress.get("product_create_started"):
            matches = await printify.reconcile_product(
                shop_id, next(iter(upload_ids.values())), listing.title
            )
            if len(matches) != 1:
                with session_scope() as session:
                    publish = RunRepository(session).publish_record(
                        run_id, Channel.ETSY.value, fingerprint
                    )
                    publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
                    publish.error = "Ambiguous catalog product creation requires reconciliation"
                return PublishStatus.RECONCILIATION_REQUIRED.value
            product_result = matches[0]
        else:
            with session_scope() as session:
                publish = RunRepository(session).publish_record(
                    run_id, Channel.ETSY.value, fingerprint
                )
                progress = {**dict(publish.response_data or {}), "product_create_started": True}
                publish.response_data = progress
            payload = printify.catalog_product_payload(plan, listing, prices, upload_ids)
            try:
                product_result = await printify.create_product(shop_id, payload)
            except AmbiguousCreateError:
                matches = await printify.reconcile_product(
                    shop_id, next(iter(upload_ids.values())), listing.title
                )
                if len(matches) != 1:
                    with session_scope() as session:
                        publish = RunRepository(session).publish_record(
                            run_id, Channel.ETSY.value, fingerprint
                        )
                        publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
                        publish.error = "Ambiguous catalog product creation requires reconciliation"
                    return PublishStatus.RECONCILIATION_REQUIRED.value
                product_result = matches[0]

        with session_scope() as session:
            publish = RunRepository(session).publish_record(run_id, Channel.ETSY.value, fingerprint)
            publish.printify_product_id = str(product_result["id"])
            publish.status = PublishStatus.PUBLISHING.value
            progress = {**dict(publish.response_data or {}), "product": product_result}
            publish.response_data = progress

        if settings.publish_mode == "dry_run":
            response = progress.get("publish_response")
            if response is None:
                with session_scope() as session:
                    publish = RunRepository(session).publish_record(
                        run_id, Channel.ETSY.value, fingerprint
                    )
                    publish.response_data = {
                        **dict(publish.response_data or {}),
                        "publish_started": True,
                        "publish_started_at": datetime.now(UTC).isoformat(),
                    }
                response = await printify.publish(shop_id, str(product_result["id"]))
                with session_scope() as session:
                    publish = RunRepository(session).publish_record(
                        run_id, Channel.ETSY.value, fingerprint
                    )
                    progress = {
                        **dict(publish.response_data or {}),
                        "publish_response": response,
                    }
                    publish.response_data = progress
            final = PublishStatus.DRY_RUN
            verification: dict[str, Any] = {
                "mode": "dry_run",
                "package_digest": expected_digest,
                "variant_count": len(plan.variants),
                "surface_count": len(plan.surface_artworks),
                "verified_at": datetime.now(UTC).isoformat(),
            }
            remote = product_result
        else:
            draft_product = await printify.product(shop_id, str(product_result["id"]))
            ready_deadline = time.monotonic() + settings.etsy_native_publish_grace_seconds
            planned_ids = {item.variant_id for item in plan.variants}
            while not draft_product.get("images") or any(
                int(item.get("id") or 0) in planned_ids and not item.get("sku")
                for item in draft_product.get("variants", [])
            ):
                if time.monotonic() >= ready_deadline:
                    raise StorefrontVerificationError(
                        "Printify catalog mockups or SKUs were not ready before the deadline"
                    )
                await asyncio.sleep(10)
                draft_product = await printify.product(shop_id, str(product_result["id"]))
            _verify_printify_catalog_product(draft_product, plan, prices)
            token = await etsy_access_token(settings)
            etsy = EtsyStorefrontClient(settings, access_token=token)
            reference_images = [
                (
                    str(item.metadata_json["reference_listing_id"]),
                    storage.get(item.object_key),
                )
                for item in reference_artifacts[:3]
            ]
            if len(reference_images) != 3:
                raise StorefrontVerificationError(
                    "final mockup originality requires three immutable references"
                )
            title_by_id = {
                item.external_listing_id: item.title for item in opportunity.comparable_listings
            }
            ai = OpenAIService(settings)

            async def final_mockup_gate(mockup: bytes) -> dict[str, Any]:
                comparison = make_contact_sheet(
                    [("FINAL PRINTIFY MOCKUP", mockup), *reference_images]
                )
                vision = await ai.originality_assessment(
                    comparison, [item[0] for item in reference_images]
                )
                with session_scope() as session:
                    RunRepository(session).provider_call(
                        run_id, "final_printify_mockup_originality", vision.metadata
                    )
                report = evaluate_originality(
                    generated_image=mockup,
                    generated_wording="",
                    references=[
                        (reference_id, image, title_by_id.get(reference_id, ""))
                        for reference_id, image in reference_images
                    ],
                    vision=vision.value,
                    perceptual_block_distance=settings.perceptual_hash_block_distance,
                    minimum_originality_score=settings.originality_min_score,
                    maximum_copying_risk=settings.originality_max_copying_risk,
                )
                if not report.passed:
                    raise StorefrontVerificationError(
                        "final Printify mockup failed originality comparison"
                    )
                return report.model_dump(mode="json")

            async def draft_gallery_gate(
                gallery: list[dict[str, Any]], etsy_images: list[dict[str, Any]]
            ) -> dict[str, Any]:
                return await _verify_served_image(
                    draft_product,
                    etsy_images,
                    expected_count=len(gallery),
                    source_urls=[str(item["src"]) for item in gallery],
                )

            def checkpoint(**updates: Any) -> None:
                nonlocal progress
                progress.update(updates)
                with session_scope() as session:
                    publish = RunRepository(session).publish_record(
                        run_id, Channel.ETSY.value, fingerprint
                    )
                    publish.response_data = {
                        **dict(publish.response_data or {}),
                        **updates,
                    }

            try:
                remote, listing_id, direct_verification = await publish_direct_catalog_etsy(
                    etsy=etsy,
                    printify=printify,
                    shop_id=shop_id,
                    product_id=str(product_result["id"]),
                    product=draft_product,
                    plan=plan,
                    listing=listing,
                    prices=prices,
                    progress=progress,
                    checkpoint=checkpoint,
                    mockup_gate=final_mockup_gate,
                    served_image_gate=draft_gallery_gate,
                )
                live_listing = await etsy.listing(listing_id)
                verify_etsy_listing(
                    live_listing, listing_id, int(settings.etsy_shop_id or 0), listing.title
                )
                inventory = await etsy.inventory(listing_id)
                sku_by_variant = {
                    int(item["id"]): str(item.get("sku") or "")
                    for item in remote.get("variants", [])
                    if int(item.get("id") or 0) in {value.variant_id for value in plan.variants}
                }
                verify_generic_etsy_inventory(
                    inventory,
                    plan.variants,
                    prices,
                    sku_by_variant=sku_by_variant,
                )
            finally:
                await etsy.close()
            verification = {
                "mode": "live",
                "listing_id": listing_id,
                "package_digest": expected_digest,
                "variant_count": len(plan.variants),
                "surface_count": len(plan.surface_artworks),
                **direct_verification,
                "verified_at": datetime.now(UTC).isoformat(),
            }
            final = PublishStatus.SUCCEEDED

        with session_scope() as session:
            repository = RunRepository(session)
            publish = repository.publish_record(run_id, Channel.ETSY.value, fingerprint)
            publish.status = final.value
            publish.response_data = {
                **dict(publish.response_data or {}),
                "verification": verification,
            }
            publish.external_product_id = (
                str((remote.get("external") or {}).get("id") or "") or None
            )
            publish.error = None
            repository.save_product_mapping(
                run_id, Channel.ETSY.value, str(product_result["id"]), remote
            )
            repository.status(run_id, RunStatus.PUBLISHED)
            repository.audit(run_id, "worker", "catalog.etsy_published", verification)
        return final.value
    except (StorefrontVerificationError, httpx.HTTPError, KeyError, ValueError) as exc:
        with session_scope() as session:
            repository = RunRepository(session)
            publish = repository.publish_record(run_id, Channel.ETSY.value, "pending")
            if publish.printify_product_id or (publish.response_data or {}).get("publish_started"):
                publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
                publish.error = str(exc)
                repository.status(run_id, RunStatus.VERIFICATION_REQUIRED, str(exc))
                return PublishStatus.RECONCILIATION_REQUIRED.value
        raise
    finally:
        await printify.close()
