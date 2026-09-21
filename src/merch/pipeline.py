from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy import select

from merch.artwork_replacement import has_unresolved_artwork_replacement
from merch.config import Settings, get_settings
from merch.copy_refresh import effective_approved_listing
from merch.database import session_scope
from merch.defaults import fixture_product_template
from merch.domain.artwork_recovery import (
    ArtworkFailure,
    RecoveryStrategy,
    build_recovery_context,
    build_safe_layout_fallback,
    normalize_issue_family,
    structural_rewrite_is_material,
    typography_fallback_required,
    unsupported_brief_requirement_codes,
)
from merch.domain.featured_color import (
    color_candidates,
    render_color_preview,
    saved_selection_matches,
    select_featured_color,
)
from merch.domain.ip_screening import ip_report_eligible, screen_concept, weighted_concept_score
from merch.domain.listing_copy import normalize_listing_copy, validate_listing_copy
from merch.domain.prepress import deterministic_qa, largest_generation_size, prepare_artwork
from merch.domain.pricing import quote_price
from merch.domain.product_options import (
    catalog_replacement_groups,
    exclude_low_contrast_colors,
    full_color_publication_template,
    publication_template,
    replacement_color_target,
)
from merch.models import (
    ArtifactRecord,
    AuditEvent,
    OrderRecord,
    ProductTemplateRecord,
    PublishRecord,
    RunRecord,
)
from merch.repository import ConfigurationRepository, MetricsRepository, RunRepository
from merch.schemas import (
    ApprovalSignal,
    CandidateConcept,
    Channel,
    CreativeBrief,
    DesignMode,
    EtsyListingDefaults,
    IPScreeningReport,
    MarketplaceListing,
    MarketplaceListingSet,
    PriceQuote,
    ProductTemplate,
    PublishStatus,
    QAIssue,
    QAReport,
    RejectedConcept,
    RunInput,
    RunStatus,
    SelectionDecision,
    TypographyProposal,
    TypographySpec,
)
from merch.services.analytics import (
    AmazonAnalyticsClient,
    EtsyAnalyticsClient,
    ShopifyAnalyticsClient,
)
from merch.services.credentials import CredentialCipher, CredentialStore
from merch.services.etsy_auth import etsy_access_token
from merch.services.etsy_publisher import publish_direct_etsy
from merch.services.mockup_selection import mockup_plan
from merch.services.mockup_verification import (
    PreparedMockup,
    prepare_mockups,
    verify_etsy_mockups,
)
from merch.services.openai_service import ModelResult, OpenAIService, canonicalize_typography
from merch.services.printify import (
    AmbiguousCreateError,
    PrintifyClient,
    ProviderConfigurationError,
    channel_shop,
)
from merch.services.storage import ArtifactStorage
from merch.services.storefront import (
    EtsyStorefrontClient,
    StorefrontVerificationError,
    build_etsy_selector_inventory,
    plan_etsy_variation_images,
    printify_listing_id,
    remap_etsy_variation_images,
    selector_labels_are_exact,
    verify_etsy_inventory,
    verify_etsy_listing,
    verify_etsy_selector_labels,
    verify_etsy_variation_images,
    verify_printify_product,
)


class NoSafeCandidate(RuntimeError):
    pass


class ApprovalInvalid(RuntimeError):
    pass


def _same_garment(first: ProductTemplate, second: ProductTemplate) -> bool:
    return (
        first.blueprint_id, first.print_provider_id, first.position, first.decoration_method,
    ) == (
        second.blueprint_id, second.print_provider_id, second.position, second.decoration_method,
    )


EFFECT_RECOVERY_CODES = {
    "TYPOGRAPHY_LAYOUT", "TYPOGRAPHY_READABILITY", "DISTRESS_PRINTABILITY",
    # Saved artifacts and deterministic QA use this legacy exact-slogan code.
    "TEXT_EQUALITY",
}


def _effect_failures(report: QAReport) -> set[str]:
    return {
        issue.code for issue in report.issues
        if issue.severity == "error" and issue.code.upper() in EFFECT_RECOVERY_CODES
    }


def ensure_fixture_template(settings: Settings) -> None:
    if settings.provider_mode != "fake":
        return
    with session_scope() as session:
        if session.scalar(select(ProductTemplateRecord.id).limit(1)) is None:
            ConfigurationRepository(session).save_template(fixture_product_template())


def create_run(value: RunInput, workflow_id: str) -> None:
    with session_scope() as session:
        RunRepository(session).create(value, workflow_id)


def set_status(run_id: str, status: RunStatus, error: str | None = None) -> None:
    with session_scope() as session:
        RunRepository(session).status(run_id, status, error)


def _record_call(repo: RunRepository, run_id: str, stage: str, result: ModelResult[Any]) -> None:
    repo.provider_call(run_id, stage, result.metadata)


async def _select_featured_color_run(
    run_id: str,
    version: int,
    production_artifact: ArtifactRecord,
    template: ProductTemplate,
    catalog_template: ProductTemplate,
    storage: ArtifactStorage,
    ai: OpenAIService,
) -> ProductTemplate:
    saved = production_artifact.metadata_json.get("featured_color_selection")
    catalog_featured = catalog_template.featured_variant()
    if isinstance(saved, dict) and saved_selection_matches(
        saved, template, production_artifact.sha256, catalog_featured
    ):
        data = template.model_dump(mode="json")
        data["featured_variant_id"] = saved["selected_variant_id"]
        return ProductTemplate.model_validate(data)

    artwork = storage.get(production_artifact.object_key)
    candidates = color_candidates(template, catalog_featured.size)
    preview_kind = f"color-preview-v{version}"
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.get(run_id, full=True)
        preview_artifact = next(
            (
                item for item in run.artifacts
                if item.kind == preview_kind and item.revision == production_artifact.revision
            ),
            None,
        )
    if preview_artifact is None:
        preview = render_color_preview(artwork, candidates)
        object_key, digest = storage.put(
            preview, suffix=f"{run_id}-color-preview-v{version}-r{production_artifact.revision}.png"
        )
        with session_scope() as session:
            preview_artifact = RunRepository(session).add_artifact(
                run_id,
                kind=preview_kind,
                revision=production_artifact.revision,
                object_key=object_key,
                sha256=digest,
                width=330 * min(4, len(candidates)),
                height=390 * ((len(candidates) + min(4, len(candidates)) - 1) // min(4, len(candidates))),
                metadata={"artwork_sha256": production_artifact.sha256},
            )
            preview_id = preview_artifact.id
    else:
        preview_id = preview_artifact.id
        preview = storage.get(preview_artifact.object_key)

    ranking_result = None
    ranking_error = None
    if len(candidates) > 1:
        try:
            ranking_result = await ai.rank_shirt_colors(preview, candidates)
        except Exception as exc:
            ranking_error = f"Vision ranking failed: {type(exc).__name__}: {exc}"
            logging.getLogger(__name__).warning("%s", ranking_error)
    else:
        ranking_error = "Only one approved shirt color"
    selected, selection = select_featured_color(
        artwork,
        template,
        ranking_result.value if ranking_result else None,
        artwork_sha256=production_artifact.sha256,
        preview_artifact_id=preview_id,
        fallback_reason=ranking_error,
        catalog_featured=catalog_featured,
    )
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.get(run_id, full=True)
        saved_artifact = next(item for item in run.artifacts if item.id == production_artifact.id)
        saved_artifact.metadata_json = {
            **saved_artifact.metadata_json,
            "featured_color_selection": selection,
        }
        if ranking_result:
            _record_call(repo, run_id, "featured_color", ranking_result)
        repo.audit(
            run_id,
            "worker",
            "product.featured_color_selected",
            {
                "color": selection["selected_color"],
                "variant_id": selection["selected_variant_id"],
                "method": selection["method"],
            },
        )
    return selected


async def research_run(run_id: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    with session_scope() as session:
        repo = RunRepository(session)
        if repo.get(run_id).research_report:
            return
        repo.status(run_id, RunStatus.RESEARCHING)
        performance = MetricsRepository(session).summary(90)
        recent_concepts = repo.recent_concepts(run_id)
        try:
            template = ConfigurationRepository(session).get_template()
        except RuntimeError:
            product_context = {}
        else:
            product_context = _prompt_product_context(template)
    result = await OpenAIService(settings).research(
        date.today(), performance, product_context=product_context, recent_concepts=recent_concepts,
    )
    with session_scope() as session:
        repo = RunRepository(session)
        repo.store_research(run_id, result.value.model_dump(mode="json"))
        _record_call(repo, run_id, "research", result)


async def screen_and_select_run(run_id: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    with session_scope() as session:
        repo = RunRepository(session)
        if repo.get(run_id).selected_concept:
            return True
        repo.status(run_id, RunStatus.SCREENING if settings.ip_check_enabled else RunStatus.RANKING)
        record = repo.get(run_id)
        report = record.research_report
        prior_attempts = list((record.ip_report or {}).get("candidate_reports", []))
        if not report:
            raise RuntimeError("research report is missing")
    candidates = [CandidateConcept.model_validate(item) for item in report["candidates"]]
    if not settings.ip_check_enabled:
        model_decision = await OpenAIService(settings).select(candidates)
        unscored_selected = next(
            (
                item
                for item in candidates
                if item.concept_name == model_decision.value.selected_concept_name
            ),
            max(candidates, key=lambda item: weighted_concept_score(item, False)),
        )
        plain_decision = model_decision.value.model_copy(
            update={
                "selected_concept_name": unscored_selected.concept_name,
                "weighted_score": weighted_concept_score(unscored_selected, False),
            }
        )
        plain_eligibility: dict[str, tuple[bool, str | None, float]] = {
            item.concept_name: (True, None, weighted_concept_score(item, False))
            for item in candidates
        }
        with session_scope() as session:
            repo = RunRepository(session)
            _record_call(repo, run_id, "selection", model_decision)
            repo.store_selection(
                run_id,
                plain_decision.model_dump(mode="json"),
                unscored_selected,
                plain_eligibility,
                None,
            )
        return True
    previously_blocked = {
        item["concept_name"]
        for item in prior_attempts
        if not ip_report_eligible(
            IPScreeningReport.model_validate(item["report"]), settings.ip_risk_threshold
        )
    }
    eligible = []
    eligibility: dict[str, tuple[bool, str | None, float]] = {}
    for candidate in candidates:
        deterministic = screen_concept(candidate, settings.ip_risk_threshold)
        score = weighted_concept_score(candidate)
        accepted = (
            deterministic.status != "block" and candidate.concept_name not in previously_blocked
        )
        reason = None if accepted else "Blocked by deterministic or prior enhanced IP screening"
        eligibility[candidate.concept_name] = (accepted, reason, score)
        if accepted:
            eligible.append(candidate)
    if not eligible:
        set_status(run_id, RunStatus.NO_SAFE_CANDIDATE, "All candidates failed IP screening")
        return False
    set_status(run_id, RunStatus.RANKING)
    ai = OpenAIService(settings)
    remaining = list(eligible)
    screening_attempts: list[dict[str, Any]] = prior_attempts
    selected: CandidateConcept | None = None
    decision: SelectionDecision | None = None
    enhanced_ip = None
    while remaining:
        if not screening_attempts:
            model_decision = await ai.select(remaining)
            with session_scope() as session:
                _record_call(RunRepository(session), run_id, "selection", model_decision)
            proposed = next(
                (
                    item
                    for item in remaining
                    if item.concept_name == model_decision.value.selected_concept_name
                ),
                max(remaining, key=weighted_concept_score),
            )
            decision = model_decision.value.model_copy(
                update={
                    "selected_concept_name": proposed.concept_name,
                    "weighted_score": weighted_concept_score(proposed),
                }
            )
        else:
            proposed = max(remaining, key=weighted_concept_score)
            decision = SelectionDecision(
                selected_concept_name=proposed.concept_name,
                rationale="Highest weighted score among candidates not rejected by prior IP screens.",
                weighted_score=weighted_concept_score(proposed),
                rejected_concepts=[
                    RejectedConcept(concept_name=item.concept_name, reason="Lower weighted score")
                    for item in remaining
                    if item.concept_name != proposed.concept_name
                ],
            )
        enhanced_ip = await ai.ip_screen(proposed)
        deterministic = screen_concept(proposed, settings.ip_risk_threshold)
        if deterministic.status == "block":
            enhanced_ip = ModelResult(deterministic, enhanced_ip.metadata)
        screening_attempts.append(
            {
                "concept_name": proposed.concept_name,
                "report": enhanced_ip.value.model_dump(mode="json"),
            }
        )
        with session_scope() as session:
            repo = RunRepository(session)
            packet = enhanced_ip.value.model_dump(mode="json")
            packet["candidate_reports"] = screening_attempts
            repo.get(run_id).ip_report = packet
            _record_call(repo, run_id, "ip_screen", enhanced_ip)
        if ip_report_eligible(enhanced_ip.value, settings.ip_risk_threshold):
            selected = proposed
            break
        eligibility[proposed.concept_name] = (
            False,
            f"Enhanced IP risk {enhanced_ip.value.risk_score}/100, blocking match, or blocked status",
            weighted_concept_score(proposed),
        )
        remaining = [item for item in remaining if item.concept_name != proposed.concept_name]
    if selected is None or decision is None or enhanced_ip is None:
        set_status(
            run_id,
            RunStatus.NO_SAFE_CANDIDATE,
            "All eligible candidates failed enhanced IP screening",
        )
        return False
    ip_packet = enhanced_ip.value.model_dump(mode="json")
    ip_packet["candidate_reports"] = screening_attempts
    with session_scope() as session:
        repo = RunRepository(session)
        repo.store_selection(
            run_id,
            decision.model_dump(mode="json"),
            selected,
            eligibility,
            ip_packet,
        )
    return True


def _merge_qa(deterministic: QAReport, visual: QAReport) -> QAReport:
    by_key: dict[tuple[str, str], QAIssue] = {}
    for issue in [*deterministic.issues, *visual.issues]:
        by_key[(issue.code, issue.message)] = issue
    issues = list(by_key.values())
    return QAReport(
        passed=deterministic.passed
        and visual.passed
        and not any(item.severity == "error" for item in issues),
        revision=deterministic.revision,
        issues=issues,
        width=deterministic.width,
        height=deterministic.height,
        has_alpha=deterministic.has_alpha,
        color_profile=deterministic.color_profile,
    )


@dataclass
class ColorQAResult:
    report: QAReport
    publication: ProductTemplate | None
    excluded_base_colors: list[str]
    rejected_replacements: list[str]
    visual_calls: list[ModelResult[QAReport]]
    adjusted_visual: QAReport | None
    shortfall: bool = False


async def _qa_with_color_replacements(
    image: bytes,
    brief: CreativeBrief,
    raw_deterministic: QAReport,
    template: ProductTemplate,
    settings: Settings,
    ai: OpenAIService,
    *,
    source_scale: float,
    used_realesrgan: bool,
    rendered_text: str | None,
    effects: dict[str, Any] | None = None,
) -> ColorQAResult:
    base_colors = {item.color for item in template.variants if item.enabled}
    replacement_target = replacement_color_target(template)
    preserve_palette = replacement_target is not None
    deterministic, excluded = exclude_low_contrast_colors(
        raw_deterministic, template, allow_all=preserve_palette
    )
    if not deterministic.passed:
        return ColorQAResult(deterministic, None, excluded, [], [], None)

    candidate_groups = None
    rejected: set[str] = set()
    visual_calls: list[ModelResult[QAReport]] = []
    while True:
        if excluded and preserve_palette and candidate_groups is None:
            if settings.provider_mode == "live":
                printify = PrintifyClient(settings)
                try:
                    catalog = await printify.variants(
                        template.blueprint_id, template.print_provider_id
                    )
                finally:
                    await printify.close()
                candidate_groups = catalog_replacement_groups(template, catalog)
                if candidate_groups:
                    candidate_report = deterministic_qa(
                        image,
                        expected_width=template.print_width,
                        expected_height=template.print_height,
                        shirt_colors=[group[0].color_hex or group[0].color for group in candidate_groups],
                        revision=raw_deterministic.revision,
                        max_bytes=settings.max_artifact_bytes,
                        source_scale=source_scale,
                        used_realesrgan=used_realesrgan,
                        expected_text=brief.slogan,
                        rendered_text=rendered_text,
                        enforce_composition_scale=True,
                    )
                    bad_swatches = {
                        color.casefold()
                        for issue in candidate_report.issues
                        if issue.severity == "error" and "contrast" in issue.code.casefold()
                        for color in issue.affected_shirt_colors
                    }
                    rejected.update(
                        group[0].color for group in candidate_groups
                        if (group[0].color_hex or group[0].color).casefold() in bad_swatches
                    )
            else:
                candidate_groups = []

        if preserve_palette and excluded:
            publication = full_color_publication_template(
                template, set(excluded), candidate_groups or [], rejected
            )
            if publication is None:
                shortfall = QAIssue(
                    code="insufficient_contrast_colors",
                    severity="error",
                    message=(
                        f"Fewer than {replacement_target} catalog colors pass contrast QA "
                        "for this artwork"
                    ),
                    recommended_fix="Revise the artwork or creative brief before publication",
                )
                report = deterministic.model_copy(
                    update={"passed": False, "issues": [*deterministic.issues, shortfall]}
                )
                return ColorQAResult(
                    report, None, sorted(set(excluded) & base_colors),
                    sorted(rejected), visual_calls, None, True
                )
        else:
            publication = publication_template(template, excluded)

        qa_brief = brief.model_copy(
            update={"shirt_colors": sorted({v.color for v in publication.variants if v.enabled})}
        )
        visual = (
            await ai.visual_qa(image, qa_brief, deterministic, effects=effects)
            if effects else await ai.visual_qa(image, qa_brief, deterministic)
        )
        visual_calls.append(visual)
        adjusted, visual_excluded = exclude_low_contrast_colors(
            visual.value, publication, allow_all=preserve_palette
        )
        if visual_excluded and preserve_palette:
            excluded = sorted(set(excluded) | (set(visual_excluded) & base_colors))
            rejected.update(set(visual_excluded) - base_colors)
            if adjusted.passed:
                continue
        else:
            excluded = sorted(set(excluded) | set(visual_excluded))
            if visual_excluded:
                publication = publication_template(template, excluded)
        return ColorQAResult(
            _merge_qa(deterministic, adjusted), publication,
            sorted(set(excluded) & base_colors), sorted(rejected),
            visual_calls, adjusted
        )


def _prompt_product_context(template: ProductTemplate) -> dict[str, Any]:
    variants = [item for item in template.variants if item.enabled]
    colors = {item.color: item.color_hex for item in variants}
    return {
        "name": template.name,
        "decoration_method": template.decoration_method,
        "print_width": template.print_width,
        "print_height": template.print_height,
        "colors": colors,
        "sizes": sorted({item.size for item in variants}),
        "channels": [item.channel.value for item in template.channels if item.enabled],
        "etsy_production_partner_confirmed": template.etsy_production_partner_confirmed,
        "garment_facts": (
            template.garment_facts.model_dump(mode="json") if template.garment_facts else None
        ),
    }


async def generate_package_run(
    run_id: str,
    regenerate: bool = False,
    settings: Settings | None = None,
    preserve_brief: bool = False,
) -> bool:
    settings = settings or get_settings()
    storage = ArtifactStorage(settings)
    storage.ensure_bucket()
    with session_scope() as session:
        repo = RunRepository(session)
        previous_snapshot = repo.get(run_id).template_snapshot
        record = repo.begin_revision(run_id, regenerate, preserve_brief)
        if regenerate and previous_snapshot:
            previous_template = ProductTemplate.model_validate(previous_snapshot)
            if not _same_garment(previous_template, ConfigurationRepository(session).get_template()):
                record.template_snapshot = previous_snapshot
        repo.status(run_id, RunStatus.GENERATING)
        if not record.selected_concept:
            raise RuntimeError("selected concept is missing")
        concept = CandidateConcept.model_validate(record.selected_concept)
        template = (
            ProductTemplate.model_validate(record.template_snapshot)
            if record.template_snapshot else ConfigurationRepository(session).get_template()
        )
        version = record.version
        research_summary = (record.research_report or {}).get("market_summary", "")
        saved_brief = record.creative_brief
        saved_typography = record.typography_spec
        saved_listings = record.listings
        artifacts = {
            (item.kind, item.revision): item for item in repo.get(run_id, full=True).artifacts
        }
    ai = OpenAIService(settings)
    prompt_context = _prompt_product_context(template)
    enabled_variants = [item for item in template.variants if item.enabled]
    allowed_colors = {item.color for item in enabled_variants}
    qa_colors = template.qa_shirt_colors()
    if saved_brief:
        brief = CreativeBrief.model_validate(saved_brief)
        if concept.strategy is not None and brief.strategy != concept.strategy:
            brief = brief.model_copy(update={"strategy": concept.strategy})
            with session_scope() as session:
                RunRepository(session).get(run_id).creative_brief = brief.model_dump(mode="json")
    else:
        creative = await ai.creative(concept, prompt_context)
        brief_data = creative.value.model_dump()
        brief_data["shirt_colors"] = sorted(allowed_colors)
        brief_data["strategy"] = concept.strategy
        brief_data["slogan"] = concept.slogan_if_any
        brief = CreativeBrief.model_validate(brief_data)
        with session_scope() as session:
            repo = RunRepository(session)
            repo.get(run_id).creative_brief = brief.model_dump(mode="json")
            _record_call(repo, run_id, "creative", creative)
    typography: TypographySpec | None = None
    if saved_typography:
        if brief.slogan:
            typography = canonicalize_typography(
                brief.slogan,
                brief,
                TypographyProposal.model_validate(saved_typography),
            )
        else:
            typography = TypographySpec.model_validate(saved_typography)
    if brief.slogan and typography is None:
        typography_result = await ai.typography(brief.slogan, brief)
        typography = typography_result.value
        with session_scope() as session:
            repo = RunRepository(session)
            repo.get(run_id).typography_spec = typography.model_dump(mode="json")
            _record_call(repo, run_id, "typography", typography_result)
    generation_width, generation_height = largest_generation_size(
        template.print_width, template.print_height
    )
    final_qa: QAReport | None = None
    excluded_shirt_colors: list[str] = []
    publication: ProductTemplate | None = None
    color_shortfall = False
    repeated_visual_defects: set[str] = set()
    for revision in range(1, settings.max_revision_attempts + 1):
        source_kind = f"source-v{version}"
        production_kind = f"production-v{version}"
        production_artifact = artifacts.get((production_kind, revision))
        if production_artifact is not None:
            final_qa = QAReport.model_validate(production_artifact.metadata_json["qa"])
            excluded_shirt_colors = list(
                production_artifact.metadata_json.get("excluded_shirt_colors") or []
            )
            if production_artifact.metadata_json.get("publication_template"):
                publication = ProductTemplate.model_validate(
                    production_artifact.metadata_json["publication_template"]
                )
            color_shortfall = bool(production_artifact.metadata_json.get("color_shortfall"))
            repeated_visual_defects = set(
                production_artifact.metadata_json.get("repeated_visual_defects") or []
            )
        else:
            source_artifact = artifacts.get((source_kind, revision))
            if source_artifact is not None:
                source = storage.get(source_artifact.object_key)
            elif revision == 1:
                if brief.design_mode == DesignMode.TYPOGRAPHY:
                    import io

                    from PIL import Image

                    blank = Image.new("RGBA", (generation_width, generation_height), (0, 0, 0, 0))
                    buffer = io.BytesIO()
                    blank.save(buffer, "PNG")
                    source = buffer.getvalue()
                    art_metadata = {
                        "model": "deterministic",
                        "prompt": "typography-only transparent layer",
                        "estimated_cost_usd": 0.0,
                    }
                else:
                    source, art_metadata = await ai.artwork(
                        brief, generation_width, generation_height
                    )
                object_key, digest = storage.put(
                    source, suffix=f"{run_id}-source-v{version}-r{revision}.png"
                )
                with session_scope() as session:
                    repo = RunRepository(session)
                    source_artifact = repo.add_artifact(
                        run_id,
                        kind=source_kind,
                        revision=revision,
                        object_key=object_key,
                        sha256=digest,
                        width=generation_width,
                        height=generation_height,
                        metadata={},
                    )
                    repo.provider_call(run_id, "artwork", art_metadata)
                artifacts[(source_kind, revision)] = source_artifact
            else:
                previous_source = artifacts[(source_kind, revision - 1)]
                previous_production = artifacts[(production_kind, revision - 1)]
                previous_qa = QAReport.model_validate(previous_production.metadata_json["qa"])
                source, art_metadata = await ai.revise_artwork(
                    storage.get(previous_source.object_key), brief, previous_qa.issues
                )
                object_key, digest = storage.put(
                    source, suffix=f"{run_id}-source-v{version}-r{revision}.png"
                )
                with session_scope() as session:
                    repo = RunRepository(session)
                    source_artifact = repo.add_artifact(
                        run_id,
                        kind=source_kind,
                        revision=revision,
                        object_key=object_key,
                        sha256=digest,
                        width=generation_width,
                        height=generation_height,
                        metadata={},
                    )
                    repo.provider_call(run_id, "artwork", art_metadata)
                artifacts[(source_kind, revision)] = source_artifact
            set_status(run_id, RunStatus.PREPRESS)
            prepared = prepare_artwork(
                source,
                template.print_width,
                template.print_height,
                typography=typography,
                font_family=settings.font_family,
                font_file=settings.font_file,
                realesrgan_binary=settings.realesrgan_binary,
                realesrgan_endpoint=settings.realesrgan_endpoint,
                flat_palette=brief.palette if settings.flat_artwork_cleanup_enabled else None,
                artwork_distress_level=brief.artwork_distress_level,
            )
            raw_deterministic = deterministic_qa(
                prepared.data,
                expected_width=template.print_width,
                expected_height=template.print_height,
                shirt_colors=qa_colors,
                revision=revision,
                max_bytes=settings.max_artifact_bytes,
                source_scale=prepared.source_scale,
                used_realesrgan=prepared.used_realesrgan,
                expected_text=brief.slogan,
                rendered_text=typography.exact_text if typography else None,
                enforce_composition_scale=True,
            )
            if prepared.issues:
                raw_deterministic = raw_deterministic.model_copy(update={
                    "passed": raw_deterministic.passed
                    and not any(issue.severity == "error" for issue in prepared.issues),
                    "issues": [*raw_deterministic.issues, *prepared.issues],
                })
            set_status(run_id, RunStatus.QA)
            color_result = await _qa_with_color_replacements(
                prepared.data, brief, raw_deterministic, template, settings, ai,
                source_scale=prepared.source_scale,
                used_realesrgan=prepared.used_realesrgan,
                rendered_text=typography.exact_text if typography else None,
                effects=prepared.effects,
            )
            final_qa = color_result.report
            excluded_shirt_colors = color_result.excluded_base_colors
            publication = color_result.publication
            color_shortfall = color_result.shortfall
            adjusted_visual = color_result.adjusted_visual
            if adjusted_visual is not None and not final_qa.passed and revision > 1:
                previous = artifacts.get((production_kind, revision - 1))
                if previous is not None:
                    previous_visual = previous.metadata_json.get("visual_qa_adjusted")
                    if previous_visual is None:
                        previous_visual = previous.metadata_json.get("qa")
                    previous_codes = {
                        item["code"]
                        for item in (previous_visual or {}).get("issues", [])
                        if item.get("severity") == "error"
                    }
                    current_codes = {
                        issue.code
                        for issue in adjusted_visual.issues
                        if issue.severity == "error"
                    }
                    previous_families = {normalize_issue_family(code) for code in previous_codes}
                    repeated_visual_defects = {
                        code for code in current_codes
                        if normalize_issue_family(code) in previous_families
                    }
            object_key, digest = storage.put(
                prepared.data, suffix=f"{run_id}-production-v{version}-r{revision}.png"
            )
            with session_scope() as session:
                repo = RunRepository(session)
                production_artifact = repo.add_artifact(
                    run_id,
                    kind=production_kind,
                    revision=revision,
                    object_key=object_key,
                    sha256=digest,
                    width=prepared.width,
                    height=prepared.height,
                    metadata={
                        "generation_width": generation_width,
                        "generation_height": generation_height,
                        "source_scale": prepared.source_scale,
                        "used_realesrgan": prepared.used_realesrgan,
                        "artwork_effects": prepared.effects,
                        "typography_spec": typography.model_dump(mode="json") if typography else None,
                        "qa": final_qa.model_dump(mode="json"),
                        "raw_deterministic_qa": raw_deterministic.model_dump(mode="json"),
                        "visual_qa": (
                            color_result.visual_calls[-1].value.model_dump(mode="json")
                            if color_result.visual_calls else None
                        ),
                        "visual_qa_adjusted": (
                            adjusted_visual.model_dump(mode="json") if adjusted_visual else None
                        ),
                        "excluded_shirt_colors": excluded_shirt_colors,
                        "rejected_replacement_colors": color_result.rejected_replacements,
                        "publication_template": (
                            publication.model_dump(mode="json") if publication else None
                        ),
                        "color_shortfall": color_shortfall,
                        "repeated_visual_defects": sorted(repeated_visual_defects),
                    },
                )
                for visual_call in color_result.visual_calls:
                    _record_call(repo, run_id, "visual_qa", visual_call)
            artifacts[(production_kind, revision)] = production_artifact
        if final_qa.passed:
            break
        if _effect_failures(final_qa):
            break
        if unsupported_brief_requirement_codes(final_qa.issues):
            break
        if color_shortfall:
            break
        elif repeated_visual_defects:
            break
        if brief.design_mode == DesignMode.TYPOGRAPHY:
            break
    if final_qa is None:
        raise RuntimeError("artwork generation produced no result")
    if not final_qa.passed:
        with session_scope() as session:
            RunRepository(session).get(run_id).qa_report = None
        if effect_failures := _effect_failures(final_qa):
            set_status(
                run_id,
                RunStatus.AWAITING_BRIEF_REVISION,
                "Artwork effects failed QA (" + ", ".join(sorted(effect_failures))
                + "); simplify typography or distress in the creative brief",
            )
        elif contract_failures := unsupported_brief_requirement_codes(final_qa.issues):
            set_status(
                run_id,
                RunStatus.AWAITING_BRIEF_REVISION,
                "Unsupported creative brief requirements (" + ", ".join(sorted(contract_failures))
                + "); rewrite the brief for the raster artwork and configured catalog",
            )
        elif color_shortfall:
            target_colors = replacement_color_target(template) or len(
                {item.color for item in template.variants if item.enabled}
            )
            set_status(
                run_id,
                RunStatus.AWAITING_BRIEF_REVISION,
                f"Fewer than {target_colors} catalog colors pass contrast QA; "
                "revise the artwork brief",
            )
        elif repeated_visual_defects:
            codes = ", ".join(sorted(repeated_visual_defects))
            set_status(
                run_id,
                RunStatus.AWAITING_BRIEF_REVISION,
                f"Repeated visual defect ({codes}); revise the creative brief before retrying artwork",
            )
        else:
            set_status(
                run_id, RunStatus.AWAITING_BRIEF_REVISION,
                "Artwork failed QA after maximum revisions; revise the creative brief",
            )
        return False
    effective_template = publication or publication_template(template, excluded_shirt_colors)
    final_artifact = artifacts[(f"production-v{version}", final_qa.revision)]
    effective_template = await _select_featured_color_run(
        run_id, version, final_artifact, effective_template, template, storage, ai
    )
    with session_scope() as session:
        RunRepository(session).get(run_id).qa_report = final_qa.model_dump(mode="json")
    if saved_listings:
        listings = MarketplaceListingSet.model_validate(saved_listings)
    else:
        set_status(run_id, RunStatus.LISTING)
        listing_brief = brief.model_copy(
            update={
                "shirt_colors": sorted({
                    item.color for item in effective_template.variants if item.enabled
                })
            }
        )
        with session_scope() as session:
            state = dict(RunRepository(session).get(run_id).listing_generation_state or {})
        if state.get("version") != version:
            state = {"version": version}
        context = _prompt_product_context(effective_template)
        context["artwork_effects"] = final_artifact.metadata_json.get("artwork_effects")
        if state.get("draft"):
            draft = MarketplaceListingSet.model_validate(state["draft"])
        else:
            listing_result = await ai.listings(context, listing_brief, research_summary)
            draft = normalize_listing_copy(listing_result.value)
            state["draft"] = draft.model_dump(mode="json")
            with session_scope() as session:
                repo = RunRepository(session)
                repo.get(run_id).listing_generation_state = dict(state)
                _record_call(repo, run_id, "listings", listing_result)
        if state.get("polished"):
            listings = MarketplaceListingSet.model_validate(state["polished"])
        else:
            try:
                polished_result = await ai.polish_listings(context, listing_brief, draft)
                listings = normalize_listing_copy(polished_result.value)
                state["polished"] = listings.model_dump(mode="json")
                with session_scope() as session:
                    repo = RunRepository(session)
                    repo.get(run_id).listing_generation_state = dict(state)
                    _record_call(repo, run_id, "listing_polish", polished_result)
            except Exception as exc:
                state["warning"] = f"Listing polish failed: {type(exc).__name__}: {exc}"
                listings = draft
        try:
            validate_listing_copy(listings)
        except ValueError as exc:
            validate_listing_copy(draft)
            state["warning"] = f"Polished copy failed validation: {exc}"
            listings = draft
        state["final"] = listings.model_dump(mode="json")
        with session_scope() as session:
            repo = RunRepository(session)
            repo.get(run_id).listings = listings.model_dump(mode="json")
            repo.get(run_id).listing_generation_state = dict(state)
    quotes = []
    for channel in effective_template.channels:
        if not channel.enabled:
            continue
        for variant in effective_template.variants:
            if variant.enabled:
                quotes.append(
                    quote_price(
                        channel=channel.channel,
                        variant_id=variant.variant_id,
                        production_cost_cents=variant.production_cost_cents,
                        percent_fee=channel.percent_fee,
                        fixed_fee_cents=channel.fixed_fee_cents,
                        target_margin=settings.target_margin,
                    )
                )
    with session_scope() as session:
        repo = RunRepository(session)
        repo.store_package(
            run_id,
            brief=brief.model_dump(mode="json"),
            typography=typography.model_dump(mode="json") if typography else None,
            qa=final_qa.model_dump(mode="json"),
            listings=listings.model_dump(mode="json"),
            quotes=[quote.model_dump(mode="json") for quote in quotes],
            template=template.model_dump(mode="json"),
            excluded_shirt_colors=excluded_shirt_colors,
            publication_template=effective_template.model_dump(mode="json"),
        )
        if excluded_shirt_colors:
            repo.audit(
                run_id,
                "worker",
                "product.colors_excluded",
                {
                    "colors": excluded_shirt_colors,
                    "replacements": sorted(
                        {item.color for item in effective_template.variants if item.enabled}
                        - allowed_colors
                    ),
                },
            )
        repo.status(run_id, RunStatus.AWAITING_APPROVAL)
    return True


async def rewrite_failed_brief_run(run_id: str, settings: Settings | None = None) -> str:
    """Rewrite within a durable budget; rejected proposals never buy new artwork."""
    settings = settings or get_settings()
    while True:
        with session_scope() as session:
            session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
            repo = RunRepository(session)
            run = repo.get(run_id, full=True)
            if run.status == RunStatus.PENDING.value and run.error == "Automatic brief rewrite ready":
                return RunStatus.PENDING.value
            if run.status not in {RunStatus.FAILED.value, RunStatus.AWAITING_BRIEF_REVISION.value}:
                return run.status
            if not run.selected_concept or not run.creative_brief:
                return run.status
            if run.qa_report is not None or run.approvals or run.publishes:
                return run.status
            artifacts = [item for item in run.artifacts if item.kind == f"production-v{run.version}"]
            if not artifacts:
                return run.status
            latest = max(artifacts, key=lambda item: item.revision)
            qa = QAReport.model_validate(latest.metadata_json["qa"])
            if qa.passed or not any(issue.severity == "error" for issue in qa.issues):
                return run.status
            events = list(session.scalars(select(AuditEvent).where(
                AuditEvent.run_id == run_id,
                AuditEvent.action.in_(["artwork.brief_rewrite_attempt", "artwork.brief_rewritten"]),
            ).order_by(AuditEvent.created_at, AuditEvent.id)))
            attempts = [event for event in events if event.action == "artwork.brief_rewrite_attempt"]
            legacy_count = sum(
                event.action == "artwork.brief_rewritten" and not event.detail.get("attempt_id")
                for event in events
            )
            # Subtract new accepted attempts from the version floor before adding all
            # reserved calls, so rejected calls on legacy runs also consume the budget.
            accepted_attempts = sum(event.detail.get("outcome") == "accepted" for event in attempts)
            legacy_floor = max(run.version - 1 - accepted_attempts, legacy_count)
            used_attempts = legacy_floor + len(attempts)
            for prior_attempt in attempts:
                if prior_attempt.detail.get("outcome") == "started":
                    prior_attempt.detail = {**prior_attempt.detail, "outcome": "interrupted"}
            previous_strategy: RecoveryStrategy | None = (
                "structural_simplification" if any(
                    event.detail.get("recovery_context", {}).get("strategy") == "structural_simplification"
                    for event in events
                ) else None
            )
            failures = [
                ArtworkFailure(
                    version=int(item.kind.removeprefix("production-v")),
                    revision=item.revision,
                    issues=tuple(QAReport.model_validate(item.metadata_json["qa"]).issues),
                )
                for item in run.artifacts
                if item.kind.startswith("production-v") and item.metadata_json.get("qa")
            ]
            context = build_recovery_context(
                failures, attempt=used_attempts + 1, previous_strategy=previous_strategy,
            )
            concept = CandidateConcept.model_validate(run.selected_concept)
            brief_snapshot = dict(run.creative_brief)
            brief = CreativeBrief.model_validate(brief_snapshot)
            template = (
                ProductTemplate.model_validate(run.template_snapshot)
                if run.template_snapshot else ConfigurationRepository(session).get_template()
            )
            allowed_colors = sorted({
                item.color for item in template.variants if item.enabled
            })
            version = run.version
            effects = latest.metadata_json.get("artwork_effects")
            typography_spec = latest.metadata_json.get("typography_spec") or run.typography_spec
            fallback_applied = session.scalar(select(AuditEvent.id).where(
                AuditEvent.run_id == run_id,
                AuditEvent.action.in_([
                    "artwork.safe_layout_fallback",
                    "artwork.typography_fallback",
                ]),
            )) is not None
            if fallback_applied:
                repo.status(
                    run_id,
                    RunStatus.AWAITING_BRIEF_REVISION,
                    "Automatic safe-layout fallback failed QA; manual review is required",
                )
                return RunStatus.AWAITING_BRIEF_REVISION.value
            budget_exhausted = used_attempts >= settings.max_brief_rewrites
            if typography_fallback_required(
                failures,
                brief=brief,
                budget_exhausted=budget_exhausted,
            ):
                fallback_brief, fallback_typography = build_safe_layout_fallback(
                    brief, allowed_colors
                )
                run.version += 1
                run.creative_brief = fallback_brief.model_dump(mode="json")
                run.typography_spec = fallback_typography.model_dump(mode="json")
                run.qa_report = None
                run.listings = None
                run.listing_generation_state = None
                run.price_quotes = None
                run.excluded_shirt_colors = None
                run.publication_template_snapshot = None
                trigger = (
                    "rewrite_budget_exhausted"
                    if budget_exhausted
                    else "repeated_typography_layout_failure"
                )
                repo.status(run_id, RunStatus.PENDING, "Automatic safe-layout fallback ready")
                repo.audit(run_id, "worker", "artwork.safe_layout_fallback", {
                    "from_version": version,
                    "to_version": run.version,
                    "trigger": trigger,
                    "rewrite_attempts_used": used_attempts,
                    "previous_brief": brief.model_dump(mode="json"),
                    "fallback_brief": fallback_brief.model_dump(mode="json"),
                    "typography_spec": fallback_typography.model_dump(mode="json"),
                    "failure_history": [item.to_dict() for item in context.history],
                })
                return RunStatus.PENDING.value
            if budget_exhausted:
                repo.status(
                    run_id, RunStatus.AWAITING_BRIEF_REVISION,
                    f"Automatic brief rewrite budget exhausted ({settings.max_brief_rewrites} attempts); "
                    "artwork still requires a passing brief and QA",
                )
                return RunStatus.AWAITING_BRIEF_REVISION.value
            rewrite_issues = list(qa.issues)
            if attempts and attempts[-1].detail.get("rejection_reason"):
                rewrite_issues.append(QAIssue(
                    code="BRIEF_REWRITE_UNCHANGED", severity="error",
                    message=str(attempts[-1].detail["rejection_reason"]),
                    recommended_fix="Replace the composition and generation instructions; do not append the old brief.",
                ))
            attempt_id = str(uuid4())
            session.add(AuditEvent(
                id=attempt_id, run_id=run_id, actor="worker", action="artwork.brief_rewrite_attempt",
                detail={
                    "from_version": version, "outcome": "started",
                    "recovery_context": context.to_dict(),
                },
            ))
        if effects or typography_spec:
            effect_context = json.dumps({
                "artwork_effects": effects, "typography_spec": typography_spec,
            }, sort_keys=True)
            rewrite_issues = [
                issue.model_copy(update={
                    "recommended_fix": (
                        (issue.recommended_fix or "Revise the brief to resolve this finding.")
                        + " Saved rendering settings: " + effect_context
                        + (
                            " Simplify the requested arch or distress; illustration edits cannot fix "
                            "effects applied during prepress."
                            if issue.code.upper() in EFFECT_RECOVERY_CODES else ""
                        )
                    ),
                }) for issue in rewrite_issues
            ]
        try:
            result = await OpenAIService(settings).revise_brief(
                concept, brief, rewrite_issues, allowed_colors, recovery_context=context,
            )
        except Exception as exc:
            with session_scope() as session:
                event = session.get(AuditEvent, attempt_id)
                if event is not None and event.detail.get("outcome") == "started":
                    event.detail = {
                        **event.detail, "outcome": "provider_error", "error_type": type(exc).__name__,
                    }
            raise
        fixed = {
            "concept_name": brief.concept_name,
            "target_customer": brief.target_customer,
            "customer_motivation": brief.customer_motivation,
            "slogan": brief.slogan,
            "design_mode": brief.design_mode,
            "shirt_colors": allowed_colors,
            "strategy": brief.strategy,
        }
        revised = CreativeBrief.model_validate({**result.value.model_dump(mode="json"), **fixed})
        rejection = None
        if revised.model_dump(mode="json") == brief.model_dump(mode="json"):
            rejection = "Automatic brief rewrite made no change"
        elif context.strategy == "structural_simplification" and not structural_rewrite_is_material(brief, revised):
            rejection = "Structural recovery must replace both composition and generation instructions"
        with session_scope() as session:
            session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
            repo = RunRepository(session)
            run = repo.get(run_id, full=True)
            event = session.get(AuditEvent, attempt_id)
            assert event is not None
            # A timed-out activity may finish after its retry or an operator has moved on.
            superseded = (
                event.detail.get("outcome") != "started" or run.version != version
                or run.status not in {RunStatus.FAILED.value, RunStatus.AWAITING_BRIEF_REVISION.value}
                # Compare the saved representation so schema defaults added by
                # validation do not make an unchanged legacy brief look newer.
                or run.creative_brief != brief_snapshot
                or run.qa_report is not None or run.approvals or run.publishes
            )
            repo.provider_call(run_id, "brief_rewrite", {
                **result.metadata, "attempt_id": attempt_id, "recovery_context": context.to_dict(),
                "outcome": "superseded" if superseded else "rejected" if rejection else "accepted",
            })
            if superseded:
                event.detail = {**event.detail, "outcome": "superseded"}
                return run.status
            event.detail = {
                **event.detail, "outcome": "rejected" if rejection else "accepted",
                "rejection_reason": rejection, "candidate_brief": revised.model_dump(mode="json"),
            }
            if rejection:
                # Persist the rejected call before requesting another proposal in this budget.
                continue
            run.version += 1
            run.creative_brief = revised.model_dump(mode="json")
            run.typography_spec = None
            run.qa_report = None
            run.listings = None
            run.listing_generation_state = None
            run.price_quotes = None
            run.excluded_shirt_colors = None
            run.publication_template_snapshot = None
            repo.status(run_id, RunStatus.PENDING, "Automatic brief rewrite ready")
            repo.audit(run_id, "worker", "artwork.brief_rewritten", {
                "from_version": version, "to_version": run.version, "attempt_id": attempt_id,
                "recovery_context": context.to_dict(),
                "previous_brief": brief.model_dump(mode="json"),
                "issues": [issue.model_dump(mode="json") for issue in qa.issues],
                "artwork_effects": effects, "typography_spec": typography_spec,
            })
        return RunStatus.PENDING.value


def record_approval(run_id: str, signal: ApprovalSignal, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    with session_scope() as session:
        repo = RunRepository(session)
        record = repo.get(run_id)
        if record.status != RunStatus.AWAITING_APPROVAL.value:
            raise ApprovalInvalid("run is not awaiting approval")
        if record.version != signal.expected_version:
            raise ApprovalInvalid("approval version no longer matches the review package")
        if signal.actor == "system" and repo.has_current_audit_action(
            run_id, "artwork.typography_fallback", record.version
        ):
            raise ApprovalInvalid(
                "emergency typography fallback requires human design review"
            )
        if signal.actor == "system" and (
            settings.manual_approval_enabled
            or settings.ip_check_enabled
            or settings.etsy_production_partner_check_enabled
        ):
            raise ApprovalInvalid("manual review is enabled for this run")
        if not signal.channels:
            raise ApprovalInvalid("at least one publication channel is required")
        if not (record.qa_report or {}).get("passed"):
            raise ApprovalInvalid("passing QA is required")
        if settings.ip_check_enabled and not signal.ip_attested:
            raise ApprovalInvalid("IP attestation is required")
        if not record.listings:
            raise ApprovalInvalid("complete listing copy is required")
        try:
            validate_listing_copy(MarketplaceListingSet.model_validate(record.listings))
        except ValueError as exc:
            raise ApprovalInvalid(f"listing copy failed validation: {exc}") from exc
        template = ConfigurationRepository(session).get_template()
        if record.template_snapshot:
            saved_template = ProductTemplate.model_validate(record.template_snapshot)
            if _same_garment(template, saved_template) and template != saved_template:
                raise ApprovalInvalid("product template changed; regenerate the review package")
            template = saved_template
        effective_template = publication_template(
            template, record.excluded_shirt_colors or [], record.publication_template_snapshot
        )
        expected_prices = {
            (channel.channel.value, variant.variant_id)
            for channel in effective_template.channels
            if channel.enabled
            for variant in effective_template.variants
            if variant.enabled
        }
        approved_prices = {
            (item["channel"], item["variant_id"]) for item in record.price_quotes or []
        }
        if approved_prices != expected_prices:
            raise ApprovalInvalid("approved prices do not match the QA-approved shirt options")
        if (
            settings.etsy_production_partner_check_enabled
            and Channel.ETSY in signal.channels
            and not template.etsy_production_partner_confirmed
        ):
            raise ApprovalInvalid("Etsy production partner confirmation is required")
        enabled = {item.channel for item in template.channels if item.enabled}
        if not set(signal.channels).issubset(enabled):
            raise ApprovalInvalid("one or more requested channels are not configured")
        repo.approve(run_id, signal)
        repo.status(run_id, RunStatus.PUBLISHING)


def automatic_approval_signal(
    run_id: str, settings: Settings | None = None
) -> ApprovalSignal | None:
    """Prepare automatic release only when no run-level manual attestation is enabled."""
    settings = settings or get_settings()
    if (
        settings.manual_approval_enabled
        or settings.ip_check_enabled
        or settings.etsy_production_partner_check_enabled
    ):
        return None
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.get(run_id)
        if run.status != RunStatus.AWAITING_APPROVAL.value:
            raise ApprovalInvalid("run is not ready for automatic release")
        if repo.has_current_audit_action(
            run_id, "artwork.typography_fallback", run.version
        ):
            return None
        if not (run.qa_report or {}).get("passed") or not run.listings or not run.price_quotes:
            raise ApprovalInvalid("automatic release requires a passing completed package")
        template = (
            ProductTemplate.model_validate(run.template_snapshot)
            if run.template_snapshot
            else ConfigurationRepository(session).get_template()
        )
        channels = [item.channel for item in template.channels if item.enabled]
        if not channels:
            raise ApprovalInvalid("no enabled publication channels are configured")
        return ApprovalSignal(
            channels=channels,
            expected_version=run.version,
            ip_attested=False,
            actor="system",
        )


def record_rejection(run_id: str, actor: str = "admin") -> None:
    with session_scope() as session:
        repo = RunRepository(session)
        record = repo.get(run_id)
        signal = ApprovalSignal(
            channels=[], expected_version=record.version, ip_attested=False, actor=actor
        )
        repo.approve(run_id, signal, decision="rejected")
        repo.status(run_id, RunStatus.REJECTED)


async def revalidate_approval_run(run_id: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.get(run_id)
        active_template = ConfigurationRepository(session).get_template()
        approved_template = (
            ProductTemplate.model_validate(run.template_snapshot)
            if run.template_snapshot
            else active_template
        )
        uses_active_template = _same_garment(active_template, approved_template)
        template = active_template if uses_active_template else approved_template
        excluded_shirt_colors = list(run.excluded_shirt_colors or [])
        publication_snapshot = run.publication_template_snapshot
        if run.status != RunStatus.PUBLISHING.value:
            raise ApprovalInvalid("run is not in the approved publishing state")
    printify = PrintifyClient(settings)
    try:
        current = await printify.validate_template(template)
        if (
            current.print_width != approved_template.print_width
            or current.print_height != approved_template.print_height
            or {
                (item.variant_id, item.color, item.color_hex, item.size)
                for item in current.variants if item.enabled
            } != {
                (item.variant_id, item.color, item.color_hex, item.size)
                for item in approved_template.variants if item.enabled
            }
        ):
            with session_scope() as session:
                configuration = ConfigurationRepository(session)
                if (
                    uses_active_template and configuration.get_template() == active_template
                    and current != active_template
                ):
                    configuration.save_template(current)
                repository = RunRepository(session)
                run = repository.get(run_id)
                run.template_snapshot = current.model_dump(mode="json")
                run.qa_report = None
                run.excluded_shirt_colors = None
                run.publication_template_snapshot = None
                repository.reprice_package(
                    run_id,
                    quotes=[],
                    reason="Shirt options or print area changed; regenerate artwork and QA",
                )
            return False
        try:
            publication = publication_template(current, excluded_shirt_colors, publication_snapshot)
            current_costs = {item.variant_id: item.production_cost_cents for item in current.variants}
            # Keep the run's QA-approved colors and featured image while applying
            # the matching garment's current fees and reviewed/catalog costs.
            publication = current.model_copy(update={
                "variants": [
                    item.model_copy(update={
                        "production_cost_cents": current_costs.get(item.variant_id, item.production_cost_cents),
                    }) for item in publication.variants
                ],
                "featured_variant_id": publication.featured_variant_id,
            })
            effective_current = await printify.validate_template(publication)
        except ProviderConfigurationError:
            with session_scope() as session:
                repository = RunRepository(session)
                record = repository.get(run_id)
                record.qa_report = None
                record.publication_template_snapshot = None
                repository.reprice_package(
                    run_id,
                    quotes=[],
                    reason="A QA-approved shirt variant is unavailable; regenerate the package",
                )
            return False
    finally:
        await printify.close()
    approved_effective = publication_template(
        approved_template, excluded_shirt_colors, publication_snapshot
    )
    if (
        current.model_dump() == approved_template.model_dump()
        and effective_current.model_dump() == approved_effective.model_dump()
    ):
        return True
    quotes = [
        quote_price(
            channel=channel.channel,
            variant_id=variant.variant_id,
            production_cost_cents=variant.production_cost_cents,
            percent_fee=channel.percent_fee,
            fixed_fee_cents=channel.fixed_fee_cents,
            target_margin=settings.target_margin,
        )
        for channel in effective_current.channels
        if channel.enabled
        for variant in effective_current.variants
        if variant.enabled
    ]
    with session_scope() as session:
        configuration = ConfigurationRepository(session)
        if (
            uses_active_template and configuration.get_template() == active_template
            and current != active_template
        ):
            configuration.save_template(current)
        RunRepository(session).get(run_id).template_snapshot = current.model_dump(mode="json")
        RunRepository(session).get(run_id).publication_template_snapshot = (
            effective_current.model_dump(mode="json")
        )
        RunRepository(session).reprice_package(
            run_id,
            quotes=[item.model_dump(mode="json") for item in quotes],
            reason="Product template or Printify catalog changed after approval",
        )
    return False


def record_publish_failure(run_id: str, channel: Channel, error: str) -> None:
    with session_scope() as session:
        session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
        record = RunRepository(session).publish_record(run_id, channel.value, "pending")
        if record.status in {PublishStatus.SUCCEEDED.value, PublishStatus.DRY_RUN.value}:
            return
        record.status = PublishStatus.FAILED.value
        record.error = error[:4000]
        RunRepository(session).audit(
            run_id,
            "worker",
            "channel.publish_failed",
            {"channel": channel.value, "error": error[:500]},
        )


def _checkpoint_etsy_publish(
    run_id: str, *, allow_completed: bool = False, **updates: Any,
) -> None:
    with session_scope() as session:
        session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
        publish = RunRepository(session).publish_record(run_id, Channel.ETSY.value, "pending")
        if not allow_completed and publish.status in {
            PublishStatus.SUCCEEDED.value, PublishStatus.DRY_RUN.value,
        }:
            # Stop stale workers before a checkpoint can authorize another remote upload.
            raise StorefrontVerificationError("Etsy publication already completed; stale attempt stopped")
        data = dict(publish.response_data or {})
        previous = data.get("mockup_verification") or {}
        incoming = updates.get("mockup_verification")
        if previous.get("status") == "failed" and incoming and incoming != previous:
            history = list(data.get("mockup_verification_history") or [])
            history.append({
                "manifest": data.get("mockup_manifest") or [], "verification": previous,
            })
            data["mockup_verification_history"] = history[-10:]
        if updates.get("draft_create_started") and data.get("draft_create_started"):
            raise StorefrontVerificationError("Etsy draft creation is already reserved; reconcile its outcome")
        color = updates.get("image_upload_started_color")
        if color and (
            data.get("image_upload_started_color")
            or color in data.get("etsy_color_image_ids", {})
        ):
            raise StorefrontVerificationError("Etsy image upload is already reserved; reconcile its outcome")
        pending_color = data.get("image_upload_started_color")
        if (
            "image_upload_started_color" in updates and color is None and pending_color
            and pending_color not in updates.get("etsy_color_image_ids", {})
        ):
            # An overlapping retry completing another color cannot release this reservation.
            updates.pop("image_upload_started_color")
        if "etsy_color_image_ids" in updates:
            updates["etsy_color_image_ids"] = {
                **data.get("etsy_color_image_ids", {}), **updates["etsy_color_image_ids"],
            }
        data.update(updates)
        publish.response_data = data


def _clear_rejected_publish_intent(run_id: str, channel: Channel, *keys: str) -> None:
    """A definitive rejection permits a later corrected request, unlike a lost response."""
    with session_scope() as session:
        session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
        publish = RunRepository(session).publish_record(run_id, channel.value, "pending")
        data = dict(publish.response_data or {})
        for key in keys:
            data.pop(key, None)
        publish.response_data = data


async def _wait_for_native_etsy_link(
    run_id: str, printify: PrintifyClient, shop_id: str, product_id: str, settings: Settings,
    response_data: dict[str, Any],
) -> dict[str, Any]:
    started = response_data.get("native_poll_started")
    if started is None:
        started = datetime.now(UTC).isoformat()
        _checkpoint_etsy_publish(run_id, native_poll_started=started, stage="waiting_for_printify")
    started_at = datetime.fromisoformat(str(started))
    deadline = started_at.timestamp() + settings.etsy_native_publish_grace_seconds
    while True:
        product = await printify.product(shop_id, product_id)
        if (product.get("external") or {}).get("id"):
            return product
        remaining = deadline - time.time()
        if remaining <= 0:
            return product
        await asyncio.sleep(min(10, remaining))


def _mockup_evidence_writer(run_id: str, settings: Settings) -> Callable[[bytes, str], str]:
    storage = ArtifactStorage(settings)

    def write(data: bytes, content_type: str) -> str:
        extension = "png" if content_type == "image/png" else "jpg"
        key, _ = storage.put(
            data, suffix=f"{run_id}-mockup.{extension}", content_type=content_type,
        )
        return key

    return write


async def _direct_etsy_fallback(
    run_id: str, printify: PrintifyClient, settings: Settings, shop_id: str,
    product_id: str, product: dict[str, Any], template: ProductTemplate,
    listing: MarketplaceListing, quotes: list[PriceQuote],
    *, prepared_mockups: list[PreparedMockup] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if product.get("is_locked", True):
        raise StorefrontVerificationError("Printify is still publishing; Etsy fallback is unsafe")
    etsy_channel = next(item for item in template.channels if item.channel == Channel.ETSY)
    defaults = etsy_channel.etsy_listing_defaults
    if defaults is None:
        raise StorefrontVerificationError("Etsy direct-publication defaults are not configured")
    try:
        token = await etsy_access_token(settings)
    except Exception as exc:
        raise StorefrontVerificationError(f"Etsy token refresh failed: {exc}") from exc
    etsy = EtsyStorefrontClient(settings, access_token=token)
    try:
        if not etsy.configured:
            raise StorefrontVerificationError("Etsy API credentials are incomplete")
        with session_scope() as session:
            publish = RunRepository(session).publish_record(run_id, Channel.ETSY.value, "pending")
            progress = dict(publish.response_data or {})
        def checkpoint(**updates: Any) -> None:
            _checkpoint_etsy_publish(run_id, **updates)
            progress.update(updates)
        linked, featured_id, image_ids = await publish_direct_etsy(
            etsy, printify, shop_id, product_id, product, template, listing, quotes,
            defaults, progress, checkpoint,
            prepared_mockups=prepared_mockups,
            evidence_writer=_mockup_evidence_writer(run_id, settings),
        )
        verified_product, verification = await _verify_etsy_publish(
            run_id, printify, settings, shop_id, product_id, template, listing,
            quotes, featured_id or None,
            prepared_mockups=prepared_mockups,
        )
        listing_id = int((linked.get("external") or {})["id"])
        if int(verification["listing_id"]) != listing_id:
            raise StorefrontVerificationError("Etsy verification changed the Printify listing link")
        verification.update({
            "publication_mode": (
                "direct_etsy_fallback" if progress.get("etsy_listing_owned")
                else "adopted_etsy_listing"
            ),
            "photo_count": len(image_ids),
            "color_photo_links": len(image_ids),
        })
        checkpoint(stage="verified", verification=verification)
        return verified_product, verification
    finally:
        await etsy.close()


async def _verify_etsy_publish(
    run_id: str,
    printify: PrintifyClient,
    settings: Settings,
    shop_id: str,
    product_id: str,
    template: ProductTemplate,
    listing: MarketplaceListing,
    quotes: list[PriceQuote],
    prior_image_id: int | None,
    *, prepared_mockups: list[PreparedMockup] | None = None,
    allow_completed_checkpoint: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        token = await etsy_access_token(settings)
    except Exception as exc:
        raise StorefrontVerificationError(f"Etsy token refresh failed: {exc}") from exc
    etsy = EtsyStorefrontClient(settings, access_token=token)
    try:
        if not etsy.configured:
            raise StorefrontVerificationError(
                "Etsy API key, shared secret, access token, and shop ID are required to verify the live listing"
            )
        last_error: Exception | None = None
        product: dict[str, Any] = {}
        listing_id = 0
        for attempt in range(6):
            try:
                product = await printify.product(shop_id, product_id)
                verify_printify_product(product, template, quotes)
                listing_id = printify_listing_id(product)
                live_listing = await etsy.listing(listing_id)
                verify_etsy_listing(
                    live_listing, listing_id, int(settings.etsy_shop_id or 0), listing.title
                )
                inventory = await etsy.inventory(listing_id)
                verify_etsy_inventory(inventory, product, template, quotes)
                break
            except (StorefrontVerificationError, httpx.HTTPError) as exc:
                last_error = exc
                if attempt < 5:
                    await asyncio.sleep(2)
        else:
            raise StorefrontVerificationError(
                f"Etsy listing did not match the approved product: {last_error}"
            )

        with session_scope() as session:
            publish = RunRepository(session).publish_record(run_id, Channel.ETSY.value, "pending")
            image_plan = list((publish.response_data or {}).get("selector_image_plan") or [])
        if not selector_labels_are_exact(inventory):
            payload, old_to_new = build_etsy_selector_inventory(inventory)
            current_images = await etsy.variation_images(listing_id)
            if not image_plan:
                image_plan = plan_etsy_variation_images(current_images, inventory, old_to_new)
            with session_scope() as session:
                publish = RunRepository(session).publish_record(run_id, Channel.ETSY.value, "pending")
                response_data = dict(publish.response_data or {})
                response_data["selector_image_plan"] = image_plan
                publish.response_data = response_data
            await etsy.update_inventory(listing_id, payload)
            for attempt in range(6):
                inventory = await etsy.inventory(listing_id)
                try:
                    verify_etsy_selector_labels(inventory)
                    verify_etsy_inventory(inventory, product, template, quotes)
                    break
                except StorefrontVerificationError:
                    if attempt == 5:
                        raise
                    await asyncio.sleep(2)
        else:
            verify_etsy_selector_labels(inventory)

        if image_plan:
            expected_images = remap_etsy_variation_images(image_plan, inventory)
            await etsy.update_variation_images(listing_id, expected_images)
            for attempt in range(6):
                current_images = await etsy.variation_images(listing_id)
                try:
                    verify_etsy_variation_images(current_images, expected_images)
                    break
                except StorefrontVerificationError:
                    if attempt == 5:
                        raise
                    await asyncio.sleep(2)

        def checkpoint(**updates: Any) -> None:
            _checkpoint_etsy_publish(
                run_id, allow_completed=allow_completed_checkpoint, **updates,
            )

        evidence_writer = _mockup_evidence_writer(run_id, settings)
        if prepared_mockups is None:
            prepared_mockups = await prepare_mockups(
                product, template, evidence_writer=evidence_writer, checkpoint=checkpoint,
            )
        if mockup_plan(product, template) != [
            (item.color, item.source) for item in prepared_mockups
        ]:
            checkpoint(mockup_verification={
                "status": "failed", "error": "Printify mockup plan changed after validation",
            })
            raise StorefrontVerificationError("Printify mockup plan changed after validation")
        # Saved IDs and alt text are hints, never proof that Etsy serves the right pixels.
        gallery = await verify_etsy_mockups(
            etsy, listing_id, template, prepared_mockups, inventory,
            evidence_writer=evidence_writer, checkpoint=checkpoint,
        )
        image_id = int(gallery["featured_image_id"])
        checkpoint(
            stage="mockups_verified", featured_image_id=image_id,
            etsy_color_image_ids=gallery["image_ids"],
        )
        return product, {
            "listing_id": listing_id,
            "featured_variant_id": template.featured_variant().variant_id,
            "featured_image_id": image_id,
            "variant_count": len(quotes),
            "selector_labels": ["Size", "Color"],
            "photo_count": gallery["photo_count"],
            "color_photo_links": gallery["color_photo_links"],
            "mockup_verification": gallery,
            "verified_at": datetime.now(UTC).isoformat(),
        }
    finally:
        await etsy.close()


async def repair_published_etsy_listing(run_id: str, settings: Settings | None = None) -> int:
    """Recheck and repair a previously completed Etsy listing without republishing it."""
    settings = settings or get_settings()
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        publish = next((item for item in run.publishes if item.channel == Channel.ETSY.value), None)
        if not publish or publish.status != PublishStatus.SUCCEEDED.value:
            raise ApprovalInvalid("Run has no completed Etsy publication to repair")
        if not publish.printify_product_id or not run.template_snapshot or not run.listings:
            raise ApprovalInvalid("Completed Etsy publication is missing its approved package")
        template = publication_template(
            ProductTemplate.model_validate(run.template_snapshot),
            run.excluded_shirt_colors or [],
            run.publication_template_snapshot,
        )
        listing_data = next(
            (item for item in run.listings["listings"] if item["channel"] == Channel.ETSY.value),
            None,
        )
        if listing_data is None:
            raise ApprovalInvalid("Completed Etsy publication has no approved listing")
        listing = effective_approved_listing(
            run_id, Channel.ETSY, MarketplaceListing.model_validate(listing_data)
        )
        quotes = [
            PriceQuote.model_validate(item)
            for item in run.price_quotes or []
            if item["channel"] == Channel.ETSY.value
        ]
        if not quotes:
            raise ApprovalInvalid("Completed Etsy publication has no approved prices")
        product_id = publish.printify_product_id
        prior_image_id = (publish.response_data or {}).get("featured_image_id")
    printify = PrintifyClient(settings)
    try:
        _, verification = await _verify_etsy_publish(
            run_id,
            printify,
            settings,
            channel_shop(template, Channel.ETSY),
            product_id,
            template,
            listing,
            quotes,
            int(prior_image_id) if prior_image_id else None,
            allow_completed_checkpoint=True,
        )
    finally:
        await printify.close()
    with session_scope() as session:
        publish = RunRepository(session).publish_record(run_id, Channel.ETSY.value, "pending")
        response_data = dict(publish.response_data or {})
        response_data.pop("selector_image_plan", None)
        response_data["featured_image_id"] = verification["featured_image_id"]
        response_data["verification"] = verification
        publish.response_data = response_data
        publish.external_product_id = str(verification["listing_id"])
        publish.error = None
    return int(verification["listing_id"])


async def import_etsy_listing_defaults(
    listing_id: int, settings: Settings | None = None
) -> int:
    """Copy shop-specific publication metadata from one of this app's verified listings."""
    settings = settings or get_settings()
    with session_scope() as session:
        source = session.scalar(select(PublishRecord).where(
            PublishRecord.channel == Channel.ETSY.value,
            PublishRecord.external_product_id == str(listing_id),
            PublishRecord.status == PublishStatus.SUCCEEDED.value,
        ))
        if source is None:
            raise ValueError("Source listing must belong to a verified Etsy run")
        package = RunRepository(session).get(source.run_id).listings or {}
        approved_title = next(
            item["title"] for item in package.get("listings", [])
            if item["channel"] == Channel.ETSY.value
        )
    token = await etsy_access_token(settings)
    etsy = EtsyStorefrontClient(settings, access_token=token)
    try:
        listing = await etsy.listing(listing_id)
        verify_etsy_listing(listing, listing_id, int(settings.etsy_shop_id or 0), approved_title)
        inventory = await etsy.inventory(listing_id)
    finally:
        await etsy.close()
    partners = [
        int(item["production_partner_id"])
        for item in listing.get("production_partners") or []
        if item.get("production_partner_id")
    ]
    readiness = listing.get("readiness_state_id") or next((
        offer.get("readiness_state_id")
        for product in inventory.get("products", [])
        for offer in product.get("offerings", [])
        if offer.get("readiness_state_id")
    ), None)
    if readiness is None:
        raise ValueError("Source Etsy listing has no processing profile")
    defaults = EtsyListingDefaults(
        taxonomy_id=int(listing["taxonomy_id"]),
        shipping_profile_id=int(listing["shipping_profile_id"]),
        return_policy_id=int(listing["return_policy_id"]),
        readiness_state_id=int(readiness),
        production_partner_ids=partners,
    )
    with session_scope() as session:
        repository = ConfigurationRepository(session)
        template = repository.get_template()
        channels = [
            item.model_copy(update={"etsy_listing_defaults": defaults})
            if item.channel == Channel.ETSY else item
            for item in template.channels
        ]
        if not any(item.channel == Channel.ETSY for item in channels):
            raise ValueError("Active product template has no Etsy channel")
        updated = template.model_copy(update={"channels": channels})
        if updated == template:
            return session.scalar(select(ProductTemplateRecord.version).where(
                ProductTemplateRecord.active.is_(True)
            )) or 0
        version = repository.save_template(updated).version
        RunRepository(session).audit(None, "operator", "etsy.defaults_imported", {
            "source_listing_id": listing_id, "template_version": version,
        })
        return version


def _require_publish_verification(
    run_id: str, channel: Channel, fingerprint: str, product_id: str,
    response_data: dict[str, Any], mapping_data: dict[str, Any], error: Exception,
) -> PublishStatus:
    with session_scope() as session:
        session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
        repository = RunRepository(session)
        publish = repository.publish_record(run_id, channel.value, fingerprint)
        if publish.status in {PublishStatus.SUCCEEDED.value, PublishStatus.DRY_RUN.value}:
            return PublishStatus(publish.status)
        publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
        publish.error = str(error)[:4000]
        publish.response_data = {**response_data, **dict(publish.response_data or {})}
        repository.save_product_mapping(run_id, channel.value, product_id, mapping_data)
        repository.audit(
            run_id, "worker", "channel.verification_required",
            {"channel": channel.value, "reason": publish.error},
        )
    return PublishStatus.RECONCILIATION_REQUIRED


async def publish_channel_run(
    run_id: str, channel: Channel, settings: Settings | None = None
) -> PublishStatus:
    settings = settings or get_settings()
    storage = ArtifactStorage(settings)
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.get(run_id, full=True)
        template = (
            ProductTemplate.model_validate(run.template_snapshot)
            if run.template_snapshot
            else ConfigurationRepository(session).get_template()
        )
        template = publication_template(
            template, run.excluded_shirt_colors or [], run.publication_template_snapshot
        )
        if not run.listings or not run.price_quotes:
            raise RuntimeError("approved listing package is incomplete")
        listing_data = next(
            item for item in run.listings["listings"] if item["channel"] == channel.value
        )
        listing = effective_approved_listing(
            run_id, channel, MarketplaceListing.model_validate(listing_data)
        )
        quotes = [
            PriceQuote.model_validate(item)
            for item in run.price_quotes
            if item["channel"] == channel.value
        ]
        version_artifacts = [
            item
            for item in run.artifacts
            if item.kind.startswith("production-v")
            and int(item.kind.removeprefix("production-v")) <= run.version
        ]
        if not version_artifacts:
            raise RuntimeError("approved artwork version is missing")
        latest_artifact = max(
            version_artifacts,
            key=lambda item: (int(item.kind.removeprefix("production-v")), item.revision),
        )
        art = storage.get(latest_artifact.object_key)
        existing = next((item for item in run.publishes if item.channel == channel.value), None)
        if (
            channel == Channel.ETSY
            and existing is not None
            and has_unresolved_artwork_replacement(existing.response_data)
        ):
            raise ApprovalInvalid(
                "Published artwork replacement requires the dedicated "
                "reconcile-published-artwork command"
            )
        if (
            existing
            and existing.printify_product_id
            and existing.status
            in {
                PublishStatus.SUCCEEDED.value,
                PublishStatus.DRY_RUN.value,
            }
        ):
            return PublishStatus(existing.status)
        existing_upload_id = existing.artwork_upload_id if existing else None
        existing_product_id = existing.printify_product_id if existing else None
        existing_status = existing.status if existing else None
        existing_response = dict(existing.response_data or {}) if existing else {}
    if (
        settings.publish_mode == "dry_run"
        and existing_product_id
        and not existing_product_id.startswith("dry-product-")
    ):
        with session_scope() as session:
            publish = RunRepository(session).publish_record(run_id, channel.value, "pending")
            publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
            publish.error = "Live product verification requires MERCH_PUBLISH_MODE=live"
        return PublishStatus.RECONCILIATION_REQUIRED
    if settings.publish_mode == "live" and channel == Channel.ETSY:
        if template.featured_variant_id is None:
            raise ApprovalInvalid("Set featured_variant_id before publishing to Etsy")
    printify = PrintifyClient(settings)
    try:
        current_template = await printify.validate_template(template)
        if current_template.model_dump() != template.model_dump():
            set_status(
                run_id, RunStatus.AWAITING_APPROVAL, "Printify costs changed; approval invalidated"
            )
            raise ApprovalInvalid(
                "Printify catalog costs changed; regenerate prices and approve again"
            )
        shop_id = channel_shop(template, channel)
        upload_id = existing_upload_id
        if not upload_id:
            upload = await printify.upload_image(f"merch-{run_id}-v{run.version}.png", art)
            upload_id = str(upload["id"])
        fingerprint = printify.product_fingerprint(template, listing, quotes, upload_id)
        with session_scope() as session:
            session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
            publish = RunRepository(session).publish_record(run_id, channel.value, fingerprint)
            if (
                channel == Channel.ETSY
                and has_unresolved_artwork_replacement(publish.response_data)
            ):
                raise ApprovalInvalid(
                    "Published artwork replacement requires the dedicated "
                    "reconcile-published-artwork command"
                )
            if publish.status in {PublishStatus.SUCCEEDED.value, PublishStatus.DRY_RUN.value}:
                return PublishStatus(publish.status)
            # A retry may have progressed while validation or upload was in flight.
            upload_id = publish.artwork_upload_id or upload_id
            existing_product_id = publish.printify_product_id
            existing_status = publish.status
            existing_response = dict(publish.response_data or {})
            create_reserved = not (
                existing_product_id or existing_response.get("product_create_started")
                or existing_status == PublishStatus.RECONCILIATION_REQUIRED.value
            )
            if create_reserved:
                publish.response_data = {**existing_response, "product_create_started": True}
            publish.artwork_upload_id = upload_id
            publish.status = PublishStatus.CREATING.value
            publish.error = None

        product: dict[str, Any]
        if existing_product_id:
            product = {"id": existing_product_id}
        elif (
            existing_status == PublishStatus.RECONCILIATION_REQUIRED.value
            or existing_response.get("product_create_started")
        ):
            matches = await printify.reconcile_product(shop_id, upload_id, listing.title)
            if len(matches) != 1:
                with session_scope() as session:
                    session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
                    publish = RunRepository(session).publish_record(
                        run_id, channel.value, fingerprint
                    )
                    if publish.status in {PublishStatus.SUCCEEDED.value, PublishStatus.DRY_RUN.value}:
                        return PublishStatus(publish.status)
                    publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
                    publish.error = "Ambiguous product creation still requires reconciliation"
                return PublishStatus.RECONCILIATION_REQUIRED
            product = matches[0]
        else:
            assert create_reserved
            payload = printify.product_payload(template, listing, quotes, upload_id)
            try:
                product = await printify.create_product(shop_id, payload)
            except (ProviderConfigurationError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, ProviderConfigurationError) or 400 <= exc.response.status_code < 500:
                    _clear_rejected_publish_intent(run_id, channel, "product_create_started")
                raise
            except AmbiguousCreateError as exc:
                matches = await printify.reconcile_product(shop_id, upload_id, listing.title)
                with session_scope() as session:
                    session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
                    publish = RunRepository(session).publish_record(
                        run_id, channel.value, fingerprint
                    )
                    if publish.status in {PublishStatus.SUCCEEDED.value, PublishStatus.DRY_RUN.value}:
                        return PublishStatus(publish.status)
                    if len(matches) == 1:
                        publish.printify_product_id = matches[0]["id"]
                        product = matches[0]
                    else:
                        publish.status = PublishStatus.RECONCILIATION_REQUIRED.value
                        publish.error = str(exc)
                        return PublishStatus.RECONCILIATION_REQUIRED
        with session_scope() as session:
            session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
            publish = RunRepository(session).publish_record(run_id, channel.value, fingerprint)
            if publish.status in {PublishStatus.SUCCEEDED.value, PublishStatus.DRY_RUN.value}:
                return PublishStatus(publish.status)
            publish.printify_product_id = product["id"]
            publish.status = PublishStatus.PUBLISHING.value
            # Keep draft IDs and uploaded-image checkpoints even if this activity stops here.
            publish.response_data = {**product, **dict(publish.response_data or {})}
            existing_response = dict(publish.response_data)
        prepared_mockups: list[PreparedMockup] | None = None
        if settings.publish_mode == "live" and channel == Channel.ETSY:
            # Validate actual source photos before reserving or submitting a publish request.
            # Run again on retries: neither saved image IDs nor cached URLs prove content.
            mockup_product = product
            try:
                mockup_product = await printify.product(shop_id, product["id"])
                verify_printify_product(mockup_product, template, quotes)
                prepared_mockups = await prepare_mockups(
                    mockup_product, template,
                    evidence_writer=_mockup_evidence_writer(run_id, settings),
                    checkpoint=lambda **updates: _checkpoint_etsy_publish(run_id, **updates),
                )
            except (StorefrontVerificationError, httpx.HTTPError, KeyError, ValueError) as exc:
                return _require_publish_verification(
                    run_id, channel, fingerprint, str(product["id"]),
                    existing_response, mockup_product, exc,
                )
        response = existing_response.get("publish_response")
        if response is None and existing_response.get("publish_started"):
            # A missing response after a recorded request is ambiguous. Reconcile by readback.
            response = {"status": "reconciling_previous_publish"}
        if existing_status == PublishStatus.RECONCILIATION_REQUIRED.value and response is None:
            remote_product = await printify.product(shop_id, product["id"])
            external = remote_product.get("external") or {}
            if isinstance(external, dict) and external.get("id"):
                response = {"status": "already_published", "listing_id": external["id"]}
        with session_scope() as session:
            session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
            publish = RunRepository(session).publish_record(run_id, channel.value, fingerprint)
            if publish.status in {PublishStatus.SUCCEEDED.value, PublishStatus.DRY_RUN.value}:
                return PublishStatus(publish.status)
            existing_response = dict(publish.response_data or {})
            response = existing_response.get("publish_response") or (
                {"status": "reconciling_previous_publish"}
                if existing_response.get("publish_started") else response
            )
            publish_reserved = response is None
            if publish_reserved:
                existing_response["publish_started"] = True
                if channel == Channel.ETSY:
                    existing_response.setdefault("native_poll_started", datetime.now(UTC).isoformat())
                publish.response_data = existing_response
        if publish_reserved:
            try:
                response = await printify.publish(shop_id, product["id"])
            except (ProviderConfigurationError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, ProviderConfigurationError) or 400 <= exc.response.status_code < 500:
                    _clear_rejected_publish_intent(run_id, channel, "publish_started", "native_poll_started")
                raise
        response_data: dict[str, Any] = {**existing_response, "publish_response": response}
        with session_scope() as session:
            session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
            publish = RunRepository(session).publish_record(run_id, channel.value, fingerprint)
            response_data = {**response_data, **dict(publish.response_data or {})}
            if publish_reserved or "publish_response" not in response_data:
                response_data["publish_response"] = response
            publish.response_data = response_data
            response = response_data["publish_response"]
        mapping_data = product
        verification: dict[str, Any] | None = None
        if settings.publish_mode == "live":
            try:
                if channel == Channel.ETSY:
                    remote_product = await _wait_for_native_etsy_link(
                        run_id, printify, shop_id, product["id"], settings, response_data,
                    )
                    if (remote_product.get("external") or {}).get("id"):
                        prior_image_id = response_data.get("featured_image_id")
                        mapping_data, verification = await _verify_etsy_publish(
                            run_id, printify, settings, shop_id, product["id"],
                            template, listing, quotes,
                            int(prior_image_id) if prior_image_id else None,
                            prepared_mockups=prepared_mockups,
                        )
                    else:
                        mapping_data = remote_product
                        mapping_data, verification = await _direct_etsy_fallback(
                            run_id, printify, settings, shop_id, product["id"],
                            remote_product, template, listing, quotes,
                            prepared_mockups=prepared_mockups,
                        )
                else:
                    mapping_data = await printify.product(shop_id, product["id"])
                    if response.get("status") == "reconciling_previous_publish" and not (
                        mapping_data.get("external") or {}
                    ).get("id"):
                        raise StorefrontVerificationError(
                            "Printify publish outcome is unknown; storefront publication requires reconciliation"
                        )
                    verify_printify_product(mapping_data, template, quotes)
            except (StorefrontVerificationError, httpx.HTTPError, KeyError, ValueError) as exc:
                return _require_publish_verification(
                    run_id, channel, fingerprint, str(product["id"]),
                    response_data, mapping_data, exc,
                )
        final = (
            PublishStatus.DRY_RUN if settings.publish_mode == "dry_run" else PublishStatus.SUCCEEDED
        )
        with session_scope() as session:
            session.scalar(select(RunRecord.id).where(RunRecord.id == run_id).with_for_update())
            publish = RunRepository(session).publish_record(run_id, channel.value, fingerprint)
            publish.status = final.value
            response_data = {**response_data, **dict(publish.response_data or {})}
            if verification:
                response_data["featured_image_id"] = verification["featured_image_id"]
            response_data["verification"] = verification or {
                "printify_product_id": product["id"],
                "variant_count": len(quotes),
                "verified_at": datetime.now(UTC).isoformat(),
            }
            publish.response_data = response_data
            if verification:
                publish.external_product_id = str(verification["listing_id"])
            publish.error = None
            repository = RunRepository(session)
            repository.save_product_mapping(run_id, channel.value, product["id"], mapping_data)
            repository.audit(
                run_id,
                "worker",
                "channel.published",
                {"channel": channel.value, "status": final.value},
            )
        return final
    finally:
        await printify.close()


def finish_publishing(run_id: str, results: list[PublishStatus]) -> None:
    success = {PublishStatus.SUCCEEDED, PublishStatus.DRY_RUN}
    if results and all(item in success for item in results):
        set_status(run_id, RunStatus.PUBLISHED)
    elif PublishStatus.RECONCILIATION_REQUIRED in results:
        set_status(
            run_id,
            RunStatus.VERIFICATION_REQUIRED,
            "At least one storefront publication needs verification or reconciliation",
        )
    elif any(item in success for item in results):
        set_status(run_id, RunStatus.PARTIALLY_PUBLISHED)
    else:
        set_status(run_id, RunStatus.FAILED, "No selected channel published successfully")


def finish_retry(run_id: str) -> None:
    with session_scope() as session:
        statuses = list(
            session.scalars(select(PublishRecord.status).where(PublishRecord.run_id == run_id))
        )
    finish_publishing(run_id, [PublishStatus(item) for item in statuses])


async def sync_printify_orders(settings: Settings) -> str:
    if not settings.printify_api_token.get_secret_value():
        return "not configured"
    with session_scope() as session:
        template = ConfigurationRepository(session).get_template()
    client = PrintifyClient(settings)
    imported = 0
    try:
        for channel in template.channels:
            if not channel.enabled:
                continue
            page = await client.orders(channel.printify_shop_id)
            for item in page.get("data", []):
                order_id = str(item.get("id", ""))
                if not order_id:
                    continue
                line_items = item.get("line_items", [])
                first = line_items[0] if line_items else {}
                with session_scope() as session:
                    record = session.scalar(
                        select(OrderRecord).where(
                            OrderRecord.channel == channel.channel.value,
                            OrderRecord.external_order_id == order_id,
                        )
                    )
                    if record is None:
                        record = OrderRecord(
                            channel=channel.channel.value,
                            external_order_id=order_id,
                            status=str(item.get("status", "unknown")),
                        )
                        session.add(record)
                    record.printify_product_id = (
                        str(first.get("product_id")) if first.get("product_id") else None
                    )
                    record.sku = str(first.get("sku")) if first.get("sku") else None
                    record.status = str(item.get("status", record.status))
                    record.quantity = sum(int(line.get("quantity", 0)) for line in line_items)
                    record.gross_cents = item.get("total_price")
                    record.fulfillment_cost_cents = item.get("total_cost")
                    record.source_data = {
                        "id": order_id,
                        "status": record.status,
                        "created_at": item.get("created_at"),
                        "sent_to_production_at": item.get("sent_to_production_at"),
                        "line_items": [
                            {
                                "product_id": line.get("product_id"),
                                "variant_id": line.get("variant_id"),
                                "sku": line.get("sku"),
                                "quantity": line.get("quantity"),
                            }
                            for line in line_items
                        ],
                    }
                imported += 1
    finally:
        await client.close()
    return f"synced {imported} orders"


async def sync_analytics(settings: Settings | None = None) -> dict[str, str]:
    settings = settings or get_settings()
    if settings.credential_encryption_key.get_secret_value():
        with session_scope() as session:
            credentials = CredentialStore(
                session, CredentialCipher(settings.credential_encryption_key.get_secret_value())
            )
            settings = settings.model_copy(
                update={
                    "amazon_refresh_token": SecretStr(
                        credentials.get("amazon_refresh_token")
                        or settings.amazon_refresh_token.get_secret_value()
                    ),
                }
            )
    etsy_error: str | None = None
    try:
        settings = settings.model_copy(
            update={"etsy_access_token": SecretStr(await etsy_access_token(settings))}
        )
    except Exception as exc:
        etsy_error = f"error: {exc}"
    since = date.today() - timedelta(days=90)
    clients: dict[str, ShopifyAnalyticsClient | EtsyAnalyticsClient | AmazonAnalyticsClient] = {
        "shopify": ShopifyAnalyticsClient(settings),
        "etsy": EtsyAnalyticsClient(settings),
        "amazon_us": AmazonAnalyticsClient(settings),
    }
    results: dict[str, str] = {}
    try:
        results["printify"] = await sync_printify_orders(settings)
    except Exception as exc:
        results["printify"] = f"error: {exc}"
    for name, client in clients.items():
        if name == "etsy" and etsy_error:
            results[name] = etsy_error
            continue
        if not client.configured:
            results[name] = "not configured"
            continue
        try:
            metrics = await client.sync(since)
            with session_scope() as session:
                repo = MetricsRepository(session)
                for metric in metrics:
                    repo.upsert(metric)
                repo.connector_result(name, True, f"synced {len(metrics)} records", synced=True)
            results[name] = f"synced {len(metrics)} records"
        except Exception as exc:
            with session_scope() as session:
                MetricsRepository(session).connector_result(name, False, str(exc))
            results[name] = f"error: {exc}"
    return results


async def health_connectors(settings: Settings | None = None) -> dict[str, str]:
    settings = settings or get_settings()
    if settings.credential_encryption_key.get_secret_value():
        with session_scope() as session:
            credentials = CredentialStore(
                session, CredentialCipher(settings.credential_encryption_key.get_secret_value())
            )
            settings = settings.model_copy(
                update={
                    "amazon_refresh_token": SecretStr(
                        credentials.get("amazon_refresh_token")
                        or settings.amazon_refresh_token.get_secret_value()
                    ),
                }
            )
    etsy_error: str | None = None
    try:
        settings = settings.model_copy(
            update={"etsy_access_token": SecretStr(await etsy_access_token(settings))}
        )
    except Exception as exc:
        etsy_error = f"error: {exc}"
    clients: dict[str, ShopifyAnalyticsClient | EtsyAnalyticsClient | AmazonAnalyticsClient] = {
        "shopify": ShopifyAnalyticsClient(settings),
        "etsy": EtsyAnalyticsClient(settings),
        "amazon_us": AmazonAnalyticsClient(settings),
    }
    result: dict[str, str] = {}
    for name, client in clients.items():
        if name == "etsy" and etsy_error:
            result[name] = etsy_error
            continue
        if not client.configured:
            result[name] = "not configured"
            continue
        try:
            result[name] = await client.health()
        except Exception as exc:
            result[name] = f"error: {exc}"
    return result


async def run_fixture_pipeline(value: RunInput, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    ensure_fixture_template(settings)
    create_run(value, f"merch-manual-{value.run_id}")
    await research_run(str(value.run_id), settings)
    if not await screen_and_select_run(str(value.run_id), settings):
        return
    await generate_package_run(str(value.run_id), settings=settings)
