"""Catalog-wide research, gated product preparation, and Etsy-only release."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from typing import Any

from PIL import Image
from sqlalchemy import select

from merch.config import Settings, get_settings
from merch.database import session_scope
from merch.defaults import fixture_product_template
from merch.domain.catalog_prepress import validate_surface_artwork
from merch.domain.etsy_inventory import inventory_product_limit
from merch.domain.opportunities import (
    build_opportunities,
    evidence_is_fresh,
    reduce_variation_axes,
)
from merch.domain.originality import evaluate_originality, make_contact_sheet
from merch.domain.pricing import comparable_median_delivered, competitive_price
from merch.domain.seo import build_seo_evidence, copied_listing_wording, validate_seo_listing
from merch.models import ArtifactRecord, OpportunityRecord, ProductTemplateRecord
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import (
    ApprovalSignal,
    CandidateConcept,
    CatalogVariant,
    Channel,
    ConceptScores,
    DesignMode,
    Evidence,
    IPScreeningReport,
    OriginalityReport,
    ProductOpportunity,
    ProductPlanV2,
    ReferenceAnalysis,
    RunStatus,
    SurfaceArtwork,
)
from merch.services.browser_session import collect_printify_costs
from merch.services.catalog import (
    CatalogProductUnavailable,
    refresh_catalog_candidates,
    refresh_catalog_product,
)
from merch.services.etsy_catalog import resolve_etsy_profile
from merch.services.marketplace_research import collect_marketplace_evidence
from merch.services.openai_service import OpenAIService
from merch.services.printify import PRINTIFY_MAX_ENABLED_VARIANTS
from merch.services.reference_assets import (
    acquire_reference_images,
    select_reference_listings,
)
from merch.services.storage import ArtifactStorage


class OpportunityRejected(RuntimeError):
    """A candidate failed a hard gate and the workflow should try the next one."""


def _reusable_ip_report(
    payload: dict[str, Any] | None,
    provider_calls: list[dict[str, Any]] | None,
    opportunity_id: str,
) -> IPScreeningReport | None:
    if payload is None:
        return None
    latest = next(
        (
            call
            for call in reversed(provider_calls or [])
            if call.get("stage") == "catalog_ip_screen"
        ),
        None,
    )
    if latest is None or str(latest.get("opportunity_id") or "") != opportunity_id:
        return None
    return IPScreeningReport.model_validate(payload)


def _reusable_reference_analysis(
    payload: dict[str, Any] | None,
    provider_calls: list[dict[str, Any]] | None,
    opportunity_id: str,
) -> ReferenceAnalysis | None:
    if payload is None:
        return None
    latest = next(
        (
            call
            for call in reversed(provider_calls or [])
            if call.get("stage") == "reference_analysis"
        ),
        None,
    )
    if latest is None or str(latest.get("opportunity_id") or "") != opportunity_id:
        return None
    return ReferenceAnalysis.model_validate(payload)


def _ensure_template(settings: Settings) -> None:
    if settings.provider_mode != "fake":
        return
    with session_scope() as session:
        if session.scalar(select(ProductTemplateRecord.id).limit(1)) is None:
            ConfigurationRepository(session).save_template(fixture_product_template())


async def research_catalog_run(run_id: str, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    _ensure_template(settings)
    with session_scope() as session:
        RunRepository(session).status(run_id, RunStatus.RESEARCHING)
    candidate_catalog, catalog_refresh = await refresh_catalog_candidates(
        settings.research_query_limit, settings
    )
    costed_catalog = []
    for product in candidate_catalog:
        costs = await collect_printify_costs(product, settings)
        costed_catalog.append(
            product.model_copy(
                update={
                    "variants": [
                        variant.model_copy(
                            update={"production_cost_cents": costs.get(variant.variant_id)}
                        )
                        for variant in product.variants
                    ]
                }
            )
        )
    candidate_catalog = costed_catalog
    evidence_by_product: dict[tuple[int, int], list[Any]] = {}
    for product in candidate_catalog:
        snapshots = await collect_marketplace_evidence(product.title, settings)
        now = datetime.now(UTC)
        fresh = [
            item
            for item in snapshots
            if evidence_is_fresh(
                item,
                now=now,
                direct_hours=settings.competitor_direct_freshness_hours,
                fallback_hours=settings.competitor_fallback_freshness_hours,
            )
        ]
        evidence_by_product[(product.blueprint_id, product.print_provider_id)] = fresh
    opportunities = build_opportunities(candidate_catalog, evidence_by_product, count=25)
    with session_scope() as session:
        repository = RunRepository(session)
        repository.store_opportunities(run_id, opportunities)
        repository.audit(
            run_id,
            "worker",
            "catalog.researched",
            {
                "catalog_products": catalog_refresh["active_products"],
                "researched_products": len(candidate_catalog),
                "catalog_refreshed_products": catalog_refresh["refreshed_products"],
                "catalog_skipped_products": catalog_refresh["skipped_products"],
                "oldest_catalog_age_hours": catalog_refresh["oldest_catalog_age_hours"],
                "opportunities": len(opportunities),
                "listing_evidence": sum(len(items) for items in evidence_by_product.values()),
                "sources": ["etsy", "amazon_us", "tiktok_shop", "walmart", "ebay"],
            },
        )
        if not opportunities:
            repository.status(
                run_id,
                RunStatus.NO_QUALIFIED_OPPORTUNITY,
                "No opportunity met the listing-specific evidence and catalog-match gates",
            )
        else:
            repository.status(run_id, RunStatus.RANKING)
    return len(opportunities)


def resume_catalog_research(run_id: str) -> int:
    """Reuse a failed v2 run's committed opportunities without recollecting evidence."""
    with session_scope() as session:
        repository = RunRepository(session)
        record = repository.get(run_id, full=True)
        count = len(record.opportunities)
        if count == 0:
            raise ValueError("Catalog research cannot resume without saved opportunities")
        repository.status(run_id, RunStatus.RANKING)
        repository.audit(
            run_id,
            "worker",
            "catalog.research_reused",
            {
                "opportunities": count,
                "eligible_opportunities": sum(item.eligible for item in record.opportunities),
            },
        )
        return count


def _ip_concept(opportunity: ProductOpportunity) -> CandidateConcept:
    evidence = [
        Evidence(
            claim="Specific marketplace listing used as demand-pattern evidence",
            title=item.title,
            url=item.url,
            accessed_at=item.collected_at,
            kind="marketplace_proxy",
            supports=["demand", "competition"],
            limitations=item.limitations,
        )
        for item in opportunity.comparable_listings
    ]
    return CandidateConcept(
        concept_name=opportunity.concept_name,
        target_customer=opportunity.target_customer,
        customer_motivation="Identity, gifting, or functional purchase intent in the evidence set",
        trend_evidence=[item.title for item in opportunity.comparable_listings],
        why_now="Current listing evidence passed the configured freshness gate",
        slogan_if_any=None,
        visual_concept=opportunity.visual_direction,
        design_mode=DesignMode.ILLUSTRATION,
        graphic_style="Original product-appropriate commercial illustration",
        palette=["#274C77", "#A3CEF1", "#E7ECEF"],
        recommended_shirt_colors=[],
        seasonality="evidence-led",
        estimated_trend_window="current evidence window",
        competitive_advantage="Original execution with evidence-backed product and buyer intent",
        risks=["Marketplace demand signals are estimates, not guaranteed sales"],
        scores=ConceptScores(
            demand=opportunity.scores.demand,
            trend_velocity=opportunity.scores.trend_velocity,
            novelty=opportunity.scores.competition_gap,
            purchase_intent=opportunity.scores.purchase_intent,
            printability=90,
            competition=100 - opportunity.scores.competition_gap,
            longevity=opportunity.scores.longevity,
            ip_risk=0,
        ),
        evidence=evidence,
    )


def _upsert_artifact(
    run_id: str,
    *,
    kind: str,
    revision: int,
    data: bytes,
    width: int,
    height: int,
    metadata: dict[str, Any],
    storage: ArtifactStorage,
) -> ArtifactRecord:
    key, digest = storage.put(data)
    with session_scope() as session:
        existing = session.scalar(
            select(ArtifactRecord).where(
                ArtifactRecord.run_id == run_id,
                ArtifactRecord.kind == kind,
                ArtifactRecord.revision == revision,
            )
        )
        if existing is not None:
            if existing.sha256 != digest:
                raise RuntimeError("replayed catalog activity produced different artwork")
            return existing
        return RunRepository(session).add_artifact(
            run_id,
            kind=kind,
            revision=revision,
            object_key=key,
            sha256=digest,
            width=width,
            height=height,
            metadata=metadata,
        )


def _representative_mockup(artwork: bytes) -> bytes:
    with Image.open(io.BytesIO(artwork)) as source:
        foreground = source.convert("RGBA")
        foreground.thumbnail((760, 760), Image.Resampling.LANCZOS)
    background = Image.new("RGBA", (1000, 1000), "#F4F1EA")
    background.alpha_composite(
        foreground,
        ((1000 - foreground.width) // 2, (1000 - foreground.height) // 2),
    )
    output = io.BytesIO()
    background.convert("RGB").save(output, format="PNG")
    return output.getvalue()


def _merge_originality(reports: list[OriginalityReport]) -> OriginalityReport:
    return OriginalityReport(
        passed=all(item.passed for item in reports),
        originality_score=min(item.originality_score for item in reports),
        copying_risk=max(item.copying_risk for item in reports),
        findings=[finding for item in reports for finding in item.findings],
        checked_at=datetime.now(UTC),
    )


def _gallery_variant_ids(variants: list[CatalogVariant]) -> list[int]:
    """Choose a compact gallery set that represents meaningful option values."""
    visual_tokens = ("color", "device", "finish", "style", "material", "size")
    axes = sorted(
        {
            axis
            for variant in variants
            for axis in variant.options
            if any(token in axis.casefold() for token in visual_tokens)
        }
    )
    chosen: list[int] = [variants[0].variant_id]
    for axis in axes:
        for value in sorted({item.options[axis] for item in variants if axis in item.options}):
            match = next(item for item in variants if item.options.get(axis) == value)
            if match.variant_id not in chosen:
                chosen.append(match.variant_id)
            if len(chosen) == 20:
                return chosen
    for variant in variants:
        if variant.variant_id not in chosen:
            chosen.append(variant.variant_id)
        if len(chosen) == min(10, len(variants)):
            break
    return chosen


async def _prepare_opportunity(
    run_id: str,
    opportunity: ProductOpportunity,
    settings: Settings,
) -> None:
    with session_scope() as session:
        repository = RunRepository(session)
        repository.select_opportunity(run_id, opportunity)
        repository.status(run_id, RunStatus.SCREENING)
        record = repository.get(run_id)
        if not settings.ip_check_enabled:
            record.ip_report = None
        if not settings.originality_check_enabled:
            record.originality_report = None
        template = ConfigurationRepository(session).get_template()

    try:
        product = await refresh_catalog_product(
            opportunity.matched_blueprint_id,
            opportunity.matched_print_provider_id,
            settings,
        )
    except CatalogProductUnavailable as exc:
        raise OpportunityRejected(str(exc)) from exc

    fake_references = settings.provider_mode == "fake"
    reference_listings = select_reference_listings(
        opportunity,
        fake=fake_references,
    )
    reference_keys = {(item.marketplace, item.external_listing_id) for item in reference_listings}
    opportunity = opportunity.model_copy(
        update={
            "comparable_listings": reference_listings
            + [
                item
                for item in opportunity.comparable_listings
                if (item.marketplace, item.external_listing_id) not in reference_keys
            ]
        }
    )
    ai = OpenAIService(settings)
    ip_report: IPScreeningReport | None = None
    if settings.ip_check_enabled:
        with session_scope() as session:
            record = RunRepository(session).get(run_id)
            saved_ip = _reusable_ip_report(
                record.ip_report,
                record.provider_calls,
                opportunity.opportunity_id,
            )
        if saved_ip is not None:
            ip_report = saved_ip
        else:
            ip_result = await ai.ip_screen(_ip_concept(opportunity))
            ip_report = ip_result.value
            with session_scope() as session:
                repository = RunRepository(session)
                repository.get(run_id).ip_report = ip_report.model_dump(mode="json")
                repository.provider_call(
                    run_id,
                    "catalog_ip_screen",
                    {"opportunity_id": opportunity.opportunity_id, **ip_result.metadata},
                )
        if ip_report.status != "pass" or ip_report.risk_score > settings.ip_risk_threshold:
            raise OpportunityRejected(
                "IP screen did not return pass within the configured risk threshold"
            )

    costs = await collect_printify_costs(product, settings)
    costed_variants = [
        item.model_copy(update={"production_cost_cents": costs.get(item.variant_id)})
        for item in product.variants
        if item.available and item.variant_id in costs
    ]
    if not costed_variants:
        raise OpportunityRejected("no current account-specific Printify costs were collected")
    axes_before = sorted({key for item in costed_variants for key in item.options})
    maximum_axes = min(settings.etsy_max_variations_supported, 3)
    selected = reduce_variation_axes(
        costed_variants,
        max_axes=maximum_axes,
        maximum_products=min(
            PRINTIFY_MAX_ENABLED_VARIANTS,
            inventory_product_limit(min(len(axes_before), maximum_axes)),
        ),
    )
    axes = sorted({key for item in selected for key in item.options})
    if not selected or len(axes) > maximum_axes:
        raise OpportunityRejected("Printify options could not be reduced to an Etsy-safe offering")
    unsupported_methods = sorted(
        {
            surface.decoration_method
            for variant in selected
            for surface in variant.surfaces
            if surface.placement == "unsupported"
        }
    )
    if unsupported_methods:
        raise OpportunityRejected(
            "unsupported Printify decoration methods: " + ", ".join(unsupported_methods)
        )
    profile = await resolve_etsy_profile(product, axes, template, settings)

    storage = ArtifactStorage(settings)
    storage.ensure_bucket()
    expected_reference_ids = [
        item.external_listing_id for item in opportunity.comparable_listings[:3]
    ]
    saved_references: dict[int, tuple[str, bytes]] = {}
    with session_scope() as session:
        for index, reference_id in enumerate(expected_reference_ids, start=1):
            artifact = session.scalar(
                select(ArtifactRecord).where(
                    ArtifactRecord.run_id == run_id,
                    ArtifactRecord.kind == f"v2-refimg-{opportunity.opportunity_id[:8]}-{index}",
                    ArtifactRecord.revision == 1,
                )
            )
            if artifact is not None:
                if artifact.metadata_json.get("reference_listing_id") != reference_id:
                    raise RuntimeError("saved reference identity changed during activity replay")
                saved_references[index] = (reference_id, storage.get(artifact.object_key))
    acquired = (
        await acquire_reference_images(opportunity, fake=fake_references)
        if len(saved_references) < len(expected_reference_ids)
        else []
    )
    references = []
    for index, reference_id in enumerate(expected_reference_ids, start=1):
        reference = saved_references.get(index)
        if reference is None:
            acquired_reference = acquired[index - 1]
            if acquired_reference[0] != reference_id:
                raise RuntimeError("reference downloader changed listing order")
            reference = acquired_reference
        references.append(reference)
    reference_sheet = make_contact_sheet(references)
    _upsert_artifact(
        run_id,
        kind=f"v2-ref-{opportunity.opportunity_id[:12]}",
        revision=1,
        data=reference_sheet,
        width=512 * len(references),
        height=554,
        metadata={
            "listing_ids": [item[0] for item in references],
            "immutable_evidence": True,
        },
        storage=storage,
    )
    for index, (reference_id, reference_image) in enumerate(references, start=1):
        with Image.open(io.BytesIO(reference_image)) as source:
            reference_width, reference_height = source.size
        if index not in saved_references:
            _upsert_artifact(
                run_id,
                kind=f"v2-refimg-{opportunity.opportunity_id[:8]}-{index}",
                revision=1,
                data=reference_image,
                width=reference_width,
                height=reference_height,
                metadata={
                    "reference_listing_id": reference_id,
                    "immutable_evidence": True,
                },
                storage=storage,
            )
    with session_scope() as session:
        record = RunRepository(session).get(run_id)
        saved_analysis = _reusable_reference_analysis(
            record.reference_analysis,
            record.provider_calls,
            opportunity.opportunity_id,
        )
    if saved_analysis is not None:
        analysis = saved_analysis
    else:
        reference_analysis_result = await ai.analyze_references(opportunity, reference_sheet)
        analysis = reference_analysis_result.value
        with session_scope() as session:
            repository = RunRepository(session)
            repository.get(run_id).reference_analysis = analysis.model_dump(mode="json")
            repository.provider_call(
                run_id,
                "reference_analysis",
                {
                    "opportunity_id": opportunity.opportunity_id,
                    **reference_analysis_result.metadata,
                },
            )

    surface_by_signature = {
        surface.signature: surface
        for variant in selected
        for surface in variant.surfaces
        if surface.required
    }
    if not surface_by_signature:
        raise OpportunityRejected("selected variants expose no supported required print surfaces")
    artworks: list[SurfaceArtwork] = []
    artwork_bytes: dict[str, bytes] = {}
    with session_scope() as session:
        RunRepository(session).status(run_id, RunStatus.GENERATING)
    for signature, surface in sorted(surface_by_signature.items()):
        artifact_kind = (
            f"v2-art-{opportunity.opportunity_id[:6]}-"
            f"{hashlib.sha256(signature.encode()).hexdigest()[:8]}"
        )
        with session_scope() as session:
            existing_artifact = session.scalar(
                select(ArtifactRecord).where(
                    ArtifactRecord.run_id == run_id,
                    ArtifactRecord.kind == artifact_kind,
                    ArtifactRecord.revision == 1,
                )
            )
        if existing_artifact is not None:
            saved_artwork = storage.get(existing_artifact.object_key)
            saved_issues = validate_surface_artwork(saved_artwork, surface)
            if saved_issues:
                raise RuntimeError("saved surface artwork no longer passes prepress")
            artworks.append(
                SurfaceArtwork(
                    surface_signature=signature,
                    artifact_id=existing_artifact.id,
                    placement=surface.placement,
                )
            )
            artwork_bytes[signature] = saved_artwork
            continue
        generated: bytes | None = None
        metadata: dict[str, Any] = {}
        issues: list[str] = []
        for _revision in range(1, settings.max_revision_attempts + 1):
            candidate, metadata = await ai.catalog_artwork(
                opportunity, analysis, surface, reference_sheet
            )
            issues = validate_surface_artwork(candidate, surface)
            if not issues:
                generated = candidate
                break
        if generated is None:
            raise OpportunityRejected(
                f"surface {signature} failed method-aware prepress: {'; '.join(issues)}"
            )
        artifact = _upsert_artifact(
            run_id,
            kind=artifact_kind,
            revision=1,
            data=generated,
            width=surface.width,
            height=surface.height,
            metadata={**metadata, "surface": surface.model_dump(mode="json")},
            storage=storage,
        )
        artworks.append(
            SurfaceArtwork(
                surface_signature=signature,
                artifact_id=artifact.id,
                placement=surface.placement,
            )
        )
        artwork_bytes[signature] = generated

    plan = ProductPlanV2(
        blueprint_id=product.blueprint_id,
        print_provider_id=product.print_provider_id,
        product_title=product.title,
        variants=selected,
        surface_artworks=artworks,
        featured_variant_id=selected[0].variant_id,
        gallery_variant_ids=_gallery_variant_ids(selected),
        etsy_profile=profile,
        generated_at=datetime.now(UTC),
    )

    originality: OriginalityReport | None = None
    if settings.originality_check_enabled:
        originality_reports: list[OriginalityReport] = []
        reference_lookup = {
            listing.external_listing_id: image
            for (reference_id, image), listing in zip(
                references, opportunity.comparable_listings[:3], strict=True
            )
            if reference_id == listing.external_listing_id
        }
        originality_references = [
            (
                item.external_listing_id,
                reference_lookup.get(item.external_listing_id),
                item.title,
            )
            for item in opportunity.comparable_listings[:3]
        ]
        for signature, artwork in artwork_bytes.items():
            comparison = make_contact_sheet([(f"PROPOSED {signature}", artwork), *references])
            vision = await ai.originality_assessment(comparison, analysis.reference_listing_ids)
            with session_scope() as session:
                RunRepository(session).provider_call(
                    run_id, "surface_originality", vision.metadata
                )
            originality_reports.append(
                evaluate_originality(
                    generated_image=artwork,
                    generated_wording="",
                    references=originality_references,
                    vision=vision.value,
                    perceptual_block_distance=settings.perceptual_hash_block_distance,
                    minimum_originality_score=settings.originality_min_score,
                    maximum_copying_risk=settings.originality_max_copying_risk,
                )
            )
        first_art = next(iter(artwork_bytes.values()))
        representative_mockup = _representative_mockup(first_art)
        mockup_comparison = make_contact_sheet(
            [("PROPOSED PRODUCT MOCKUP", representative_mockup), *references]
        )
        mockup_vision = await ai.originality_assessment(
            mockup_comparison, analysis.reference_listing_ids
        )
        with session_scope() as session:
            RunRepository(session).provider_call(
                run_id, "representative_mockup_originality", mockup_vision.metadata
            )
        originality_reports.append(
            evaluate_originality(
                generated_image=representative_mockup,
                generated_wording="",
                references=originality_references,
                vision=mockup_vision.value,
                perceptual_block_distance=settings.perceptual_hash_block_distance,
                minimum_originality_score=settings.originality_min_score,
                maximum_copying_risk=settings.originality_max_copying_risk,
            )
        )
        originality = _merge_originality(originality_reports)
        if not originality.passed:
            raise OpportunityRejected("flat artwork or final mockup failed originality gates")

    benchmark = comparable_median_delivered(opportunity.comparable_listings)
    prices = []
    for variant in selected:
        if variant.production_cost_cents is None or variant.shipping_cost_cents is None:
            raise OpportunityRejected(
                "selected variant lacks current production or US shipping cost"
            )
        prices.append(
            competitive_price(
                variant_id=variant.variant_id,
                production_cost_cents=variant.production_cost_cents,
                fulfillment_shipping_cents=variant.shipping_cost_cents,
                customer_shipping_cents=profile.customer_shipping_cents,
                percent_fee=settings.etsy_percent_fee,
                fixed_fee_cents=settings.etsy_fixed_fee_cents,
                benchmark_median_delivered_cents=benchmark,
                target_margin=settings.target_margin,
                discount_cents=settings.configured_discount_cents,
            )
        )
    seo = build_seo_evidence(opportunity)
    listing_result = await ai.etsy_catalog_listing(opportunity, plan, seo, analysis)
    with session_scope() as session:
        RunRepository(session).provider_call(
            run_id, "etsy_catalog_listing", listing_result.metadata
        )
    listing = listing_result.value.listings[0]
    validate_seo_listing(
        listing,
        seo,
        check_competitor_terms=settings.originality_check_enabled,
    )
    if settings.originality_check_enabled:
        copied_reference = copied_listing_wording(listing, opportunity)
        if copied_reference is not None:
            raise OpportunityRejected(
                f"listing wording is too similar to competitor reference {copied_reference}"
            )

    optional_gates: dict[str, bool] = {}
    if settings.ip_check_enabled:
        optional_gates["ip"] = True
    if settings.originality_check_enabled:
        optional_gates.update(originality_flat=True, originality_mockup=True)

    package = {
        "opportunity": opportunity.model_dump(mode="json"),
        "product_plan": plan.model_dump(mode="json"),
        "reference_analysis": analysis.model_dump(mode="json"),
        "ip_report": ip_report.model_dump(mode="json") if ip_report is not None else None,
        "originality": originality.model_dump(mode="json") if originality is not None else None,
        "seo": seo.model_dump(mode="json"),
        "prices": [item.model_dump(mode="json") for item in prices],
        "listing": listing.model_dump(mode="json"),
        "gates": {
            "evidence": True,
            "catalog_match": True,
            "current_costs": True,
            "production_feasibility": True,
            **optional_gates,
            "margin": all(item.contribution_margin >= settings.target_margin for item in prices),
            "seo": True,
            "etsy_only": listing.channel == Channel.ETSY,
        },
    }
    digest = hashlib.sha256(
        json.dumps(package, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with session_scope() as session:
        repository = RunRepository(session)
        record = repository.get(run_id)
        record.ip_report = ip_report.model_dump(mode="json") if ip_report is not None else None
        record.qa_report = {
            "passed": True,
            "method_aware": True,
            "surface_count": len(artworks),
            "package_digest": digest,
            "gates": package["gates"],
        }
        repository.store_v2_package(
            run_id,
            opportunity=opportunity,
            reference_analysis=analysis,
            plan=plan,
            originality=originality,
            seo=seo,
            prices=prices,
            listings=listing_result.value.model_dump(mode="json"),
        )
        repository.status(run_id, RunStatus.AWAITING_APPROVAL)
        repository.audit(
            run_id,
            "system",
            "catalog.package_approved",
            {
                "digest": digest,
                "version": record.version,
                "gates": package["gates"],
                "channel": Channel.ETSY.value,
            },
        )
        repository.approve(
            run_id,
            ApprovalSignal(
                channels=[Channel.ETSY],
                expected_version=record.version,
                ip_attested=settings.ip_check_enabled,
                actor="system",
            ),
        )
        repository.status(run_id, RunStatus.PUBLISHING)


async def attempt_catalog_opportunity(
    run_id: str,
    rank: int,
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    with session_scope() as session:
        item = session.scalar(
            select(OpportunityRecord).where(
                OpportunityRecord.run_id == run_id,
                OpportunityRecord.rank == rank,
            )
        )
        if item is None or not item.eligible:
            return False
        opportunity = ProductOpportunity.model_validate(item.data)
        record = RunRepository(session).get(run_id)
        if (
            (record.selected_opportunity or {}).get("opportunity_id") == opportunity.opportunity_id
            and record.product_plan
            and record.status == RunStatus.PUBLISHING.value
        ):
            return True
    try:
        await _prepare_opportunity(run_id, opportunity, settings)
        return True
    except (OpportunityRejected, RuntimeError, ValueError) as exc:
        with session_scope() as session:
            repository = RunRepository(session)
            repository.reject_opportunity(run_id, opportunity.opportunity_id, str(exc))
            repository.audit(
                run_id,
                "worker",
                "opportunity.rejected",
                {
                    "opportunity_id": opportunity.opportunity_id,
                    "rank": rank,
                    "reason": str(exc),
                },
            )
            repository.status(run_id, RunStatus.RANKING, str(exc))
        return False


def finish_no_qualified_opportunity(run_id: str) -> None:
    with session_scope() as session:
        RunRepository(session).status(
            run_id,
            RunStatus.NO_QUALIFIED_OPPORTUNITY,
            "The top opportunities exhausted the enabled production, review, pricing, or SEO gates",
        )
