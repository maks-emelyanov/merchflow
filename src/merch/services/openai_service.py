from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from openai import APIStatusError, AsyncOpenAI
from PIL import Image
from pydantic import BaseModel

from merch.config import Settings
from merch.domain.artwork_recovery import RecoveryContext
from merch.domain.ip_screening import weighted_concept_score
from merch.domain.prepress import make_fixture_art
from merch.prompts import (
    ARTWORK_PROMPT,
    BRIEF_REWRITE_PROMPT,
    CREATIVE_PROMPT,
    IP_PROMPT,
    LISTING_POLISH_PROMPT,
    LISTING_PROMPT,
    PROMPT_VERSION,
    QA_PROMPT,
    RESEARCH_PROMPT,
    REVISION_PROMPT,
    SELECTION_PROMPT,
    SHIRT_COLOR_PROMPT,
    TYPOGRAPHY_PROMPT,
)
from merch.schemas import (
    CandidateConcept,
    Channel,
    ConceptScores,
    CreativeBrief,
    DesignMode,
    Evidence,
    IPScreeningReport,
    MarketplaceListing,
    MarketplaceListingSet,
    QAIssue,
    QAReport,
    RejectedConcept,
    ResearchReport,
    SelectionDecision,
    ShirtColorRanking,
    ShirtColorScore,
    TypographySpec,
)
from merch.services.openai_costs import estimate_image_cost, estimate_text_cost


@dataclass(frozen=True)
class ModelResult[T: BaseModel]:
    value: T
    metadata: dict[str, Any]


class OpenAINonRetryableError(RuntimeError):
    """An OpenAI billing, credential, or request error that a retry cannot fix."""


class OpenAIService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = (
            AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(), max_retries=0)
            if settings.provider_mode == "live"
            else None
        )

    @staticmethod
    def _handle_api_error(exc: APIStatusError) -> None:
        body = exc.body if isinstance(exc.body, dict) else {}
        code = str(body.get("code") or "")
        kind = str(body.get("type") or "")
        if exc.status_code == 429 and (
            code
            in {
                "credit_balance_exhausted",
                "insufficient_quota",
                "organization_spend_limit_exceeded",
                "project_spend_limit_exceeded",
                "organization_usage_limit_exceeded",
            }
            or kind == "insufficient_quota"
        ):
            raise OpenAINonRetryableError(
                "OpenAI API credits, spend limit, or quota are exhausted; review billing before resuming"
            ) from exc
        if exc.status_code in {400, 401, 403, 404, 422}:
            raise OpenAINonRetryableError(
                f"OpenAI rejected the request with non-retryable HTTP {exc.status_code}"
            ) from exc

    async def _parse[T: BaseModel](
        self,
        prompt: str,
        schema: type[T],
        *,
        web_search: bool = False,
        image: bytes | None = None,
        model: str | None = None,
        reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None,
        image_detail: Literal["low", "high", "original", "auto"] | None = None,
    ) -> ModelResult[T]:
        if self.client is None:
            raise RuntimeError("live OpenAI client is not configured")
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        if image is not None:
            encoded = base64.b64encode(image).decode()
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{encoded}",
                    "detail": image_detail or self.settings.openai_visual_qa_detail,
                }
            )
        selected_model = model or self.settings.openai_text_model
        selected_effort = reasoning_effort or self.settings.openai_reasoning_effort
        selected_image_detail = image_detail or self.settings.openai_visual_qa_detail
        kwargs: dict[str, Any] = {
            "model": selected_model,
            "input": [{"role": "user", "content": content}],
            "text_format": schema,
            "reasoning": {"effort": selected_effort},
            "metadata": {"prompt_version": PROMPT_VERSION},
        }
        if web_search:
            kwargs["tools"] = [{"type": "web_search"}]
        try:
            response = await self.client.responses.parse(**kwargs)
        except APIStatusError as exc:
            self._handle_api_error(exc)
            raise
        if response.output_parsed is None:
            raise RuntimeError("OpenAI returned no structured output")
        usage = response.usage.model_dump() if response.usage else None
        web_search_calls = sum(
            getattr(item, "type", None) == "web_search_call"
            for item in (getattr(response, "output", None) or [])
        )
        return ModelResult(
            value=response.output_parsed,
            metadata={
                "response_id": response.id,
                "model": response.model,
                "reasoning_effort": selected_effort,
                "image_detail": selected_image_detail if image is not None else None,
                "usage": usage,
                "web_search_calls": web_search_calls,
                "estimated_cost_usd": estimate_text_cost(response.model, usage, web_search_calls),
                "prompt": prompt,
                "prompt_version": PROMPT_VERSION,
                "schema_name": schema.__name__,
                "schema_version": "1",
            },
        )

    async def research(
        self, current_date: date, performance_summary: str
    ) -> ModelResult[ResearchReport]:
        prompt = RESEARCH_PROMPT.format(
            current_date=current_date.isoformat(),
            performance_summary=performance_summary,
            ip_risk_instruction=(
                "Score IP risk from 0-100, where 0 is safest and 100 is riskiest."
                if self.settings.ip_check_enabled
                else "Set ip_risk to 0 for every candidate."
            ),
        )
        if self.client:
            return await self._parse(
                prompt, ResearchReport, web_search=True, model=self.settings.openai_research_model
            )
        concepts = []
        for index in range(10):
            concepts.append(
                CandidateConcept(
                    concept_name=f"Trail Ritual {index + 1}",
                    target_customer="Weekend hikers who enjoy quiet morning routines",
                    customer_motivation="Wearable identity and an easy outdoors gift",
                    trend_evidence=["Fixture demand signal for deterministic local development"],
                    why_now="Outdoor micro-adventures remain giftable across seasons",
                    slogan_if_any="TAKE THE SCENIC ROUTE" if index == 0 else None,
                    visual_concept="A sunrise nested inside a simple switchback trail silhouette",
                    design_mode=DesignMode.HYBRID if index == 0 else DesignMode.ILLUSTRATION,
                    graphic_style="clean retro screen-print geometry",
                    palette=["#EF5E50", "#FFD166", "#F7F3E8"],
                    recommended_shirt_colors=["#111827", "#1F3A32"],
                    seasonality="year-round with spring and fall peaks",
                    estimated_trend_window="12 weeks",
                    competitive_advantage="Readable one-second silhouette with a specific ritual cue",
                    risks=["Generic outdoor concepts can be saturated"],
                    scores=ConceptScores(
                        demand=82 - index,
                        trend_velocity=79 - index,
                        novelty=80 - index,
                        purchase_intent=84 - index,
                        printability=92,
                        competition=35 + index,
                        longevity=86,
                        ip_risk=0,
                    ),
                    evidence=[
                        Evidence(
                            claim="Fixture evidence only; live mode performs current web research",
                            title="Merch fixture source",
                            url="https://example.com/fixture",
                        )
                    ],
                )
            )
        report = ResearchReport(
            current_date=current_date,
            market_summary="Deterministic fixture research for local and CI execution.",
            candidates=concepts,
        )
        return ModelResult(report, self._fake_metadata("research", prompt))

    async def select(self, concepts: list[CandidateConcept]) -> ModelResult[SelectionDecision]:
        concept_data = [item.model_dump(mode="json") for item in concepts]
        if not self.settings.ip_check_enabled:
            for item in concept_data:
                item["scores"]["ip_risk"] = 0
        prompt = SELECTION_PROMPT.format(
            concepts=json.dumps(concept_data),
            selection_penalties=(
                "IP uncertainty" if self.settings.ip_check_enabled else "weak originality"
            ),
        )
        if self.client:
            return await self._parse(
                prompt,
                SelectionDecision,
                reasoning_effort=self.settings.openai_creative_reasoning_effort,
            )

        def score(item: CandidateConcept) -> float:
            return weighted_concept_score(item, self.settings.ip_check_enabled)

        ranked = sorted(concepts, key=score, reverse=True)
        decision = SelectionDecision(
            selected_concept_name=ranked[0].concept_name,
            rationale="Highest deterministic weighted commercial score among eligible concepts.",
            weighted_score=score(ranked[0]),
            rejected_concepts=[
                RejectedConcept(concept_name=item.concept_name, reason="Lower weighted score")
                for item in ranked[1:]
            ],
        )
        return ModelResult(decision, self._fake_metadata("selection", prompt))

    async def ip_screen(self, concept: CandidateConcept) -> ModelResult[IPScreeningReport]:
        prompt = IP_PROMPT.format(concept=concept.model_dump_json(indent=2))
        if self.client:
            return await self._parse(
                prompt,
                IPScreeningReport,
                web_search=True,
                reasoning_effort=self.settings.openai_creative_reasoning_effort,
            )
        from merch.domain.ip_screening import screen_concept

        return ModelResult(
            screen_concept(concept, self.settings.ip_risk_threshold),
            self._fake_metadata("ip_screen", prompt),
        )

    async def creative(
        self, concept: CandidateConcept, product_template: dict[str, Any]
    ) -> ModelResult[CreativeBrief]:
        prompt = CREATIVE_PROMPT.format(
            concept=concept.model_dump_json(indent=2),
            product_template=json.dumps(product_template),
        )
        if self.client:
            return await self._parse(
                prompt,
                CreativeBrief,
                reasoning_effort=self.settings.openai_creative_reasoning_effort,
            )
        brief = CreativeBrief(
            concept_name=concept.concept_name,
            target_customer=concept.target_customer,
            customer_motivation=concept.customer_motivation,
            slogan=concept.slogan_if_any,
            design_mode=concept.design_mode,
            visual_concept=concept.visual_concept,
            composition="Centered sunrise and trail mark with slogan beneath",
            graphic_style=concept.graphic_style,
            palette=concept.palette,
            shirt_colors=concept.recommended_shirt_colors,
            typography_style="bold geometric sans",
            generation_brief="Original sunrise nested in a switchback trail, isolated, no text",
        )
        return ModelResult(brief, self._fake_metadata("creative", prompt))

    async def revise_brief(
        self, concept: CandidateConcept, brief: CreativeBrief, issues: list[QAIssue],
        shirt_colors: list[str],
        *, recovery_context: RecoveryContext | None = None,
    ) -> ModelResult[CreativeBrief]:
        context = recovery_context or RecoveryContext(attempt=1, strategy="targeted")
        prompt = BRIEF_REWRITE_PROMPT.format(
            concept=concept.model_dump_json(indent=2),
            brief=brief.model_dump_json(indent=2),
            issues=json.dumps([issue.model_dump(mode="json") for issue in issues]),
            shirt_colors=json.dumps(shirt_colors),
            recovery_context=json.dumps(context.to_dict()),
        )
        if self.client:
            result = await self._parse(
                prompt, CreativeBrief,
                reasoning_effort=self.settings.openai_creative_reasoning_effort,
            )
            return ModelResult(
                result.value, {**result.metadata, "recovery_context": context.to_dict()}
            )
        if context.strategy == "structural_simplification":
            composition = (
                "Open centered arrangement with a clear primary motif and separate supporting "
                "motifs. Remove enclosing frames and rings, optional repeated elements, and "
                "decorative marks. Keep wide printable gaps and clear anatomy."
            )
            visual_concept = (
                f"An unframed illustration of the {concept.concept_name} theme for "
                f"{concept.target_customer}, with distinct primary motifs and restrained detail."
            )
            generation_brief = (
                f"Create an original {concept.graphic_style} illustration of the "
                f"{concept.concept_name} theme. Use one clear primary motif with separate "
                "supporting shapes, open spacing, and plausible anatomy where relevant. "
                "Omit enclosing frames, optional repeats, and extra decoration. "
                "Use clean opaque colors on a transparent background, without text or texture."
            )
        else:
            composition = (
                "Balanced centered arrangement of the selected theme's primary motifs. "
                "Separate subjects and limbs with open printable gaps; correct overlaps "
                "and unclear anatomy while retaining the recognizable visual idea."
            )
            visual_concept = concept.visual_concept
            generation_brief = (
                f"Create an original illustration of: {concept.visual_concept}. "
                "Resolve overlaps and unclear anatomy through clean, distinct shapes with "
                "printable strokes and gaps. Use opaque colors on a transparent background, "
                "without text, texture, or extra outlines."
            )
        updates: dict[str, Any] = {
            "composition": composition,
            "visual_concept": visual_concept,
            "generation_brief": generation_brief,
            "shirt_colors": list(shirt_colors),
        }
        effect_codes = {issue.code.upper() for issue in issues}
        if effect_codes & {"TYPOGRAPHY_LAYOUT", "TYPOGRAPHY_READABILITY"}:
            updates["typography_style"] = "bold readable straight lettering with generous spacing"
        if "DISTRESS_PRINTABILITY" in effect_codes:
            updates["artwork_distress_level"] = 0
        revised = brief.model_copy(update=updates)
        return ModelResult(
            revised,
            {
                **self._fake_metadata("brief_rewrite", prompt),
                "recovery_context": context.to_dict(),
            },
        )

    async def typography(self, slogan: str, brief: CreativeBrief) -> ModelResult[TypographySpec]:
        prompt = TYPOGRAPHY_PROMPT.format(slogan=slogan, brief=brief.model_dump_json(indent=2))
        if self.client:
            return await self._parse(
                prompt, TypographySpec, model=self.settings.openai_typography_model
            )
        spec = TypographySpec(
            exact_text=slogan,
            line_breaks=[slogan],
            letter_spacing=0.03,
            line_spacing=1.0,
            text_alignment="center",
            text_arc_or_shape="none",
            outline="#111827",
            shadow=None,
            distress_level=0,
            primary_color="#F7F3E8",
            secondary_color=None,
            interaction_with_illustration="Placed below illustration with clear separation",
            relative_width=0.78,
            relative_height=0.12,
        )
        return ModelResult(spec, self._fake_metadata("typography", prompt))

    async def artwork(
        self, brief: CreativeBrief, width: int, height: int
    ) -> tuple[bytes, dict[str, Any]]:
        prompt = ARTWORK_PROMPT.format(brief=brief.model_dump_json(indent=2))
        if self.client:
            try:
                result = await self.client.images.generate(
                    model=self.settings.openai_image_model,
                    prompt=prompt,
                    size=f"{width}x{height}",
                    quality=self.settings.openai_image_quality,
                    background="transparent",
                    output_format="png",
                )
            except APIStatusError as exc:
                self._handle_api_error(exc)
                raise
            if not result.data or not result.data[0].b64_json:
                raise RuntimeError("OpenAI returned no artwork")
            usage = result.usage.model_dump() if result.usage else None
            return base64.b64decode(result.data[0].b64_json), {
                "model": self.settings.openai_image_model,
                "quality": result.quality or self.settings.openai_image_quality,
                "size": result.size or f"{width}x{height}",
                "usage": usage,
                "estimated_cost_usd": estimate_image_cost(self.settings.openai_image_model, usage),
                "prompt": prompt,
                "prompt_version": PROMPT_VERSION,
                "schema_name": "RasterArtwork",
                "schema_version": "1",
            }
        return make_fixture_art(width, height), self._fake_metadata("artwork", prompt)

    async def revise_artwork(
        self, image: bytes, brief: CreativeBrief, issues: list[QAIssue]
    ) -> tuple[bytes, dict[str, Any]]:
        prompt = REVISION_PROMPT.format(
            brief=brief.model_dump_json(indent=2),
            issues=json.dumps([issue.model_dump(mode="json") for issue in issues]),
        )
        if self.client:
            upload = io.BytesIO(image)
            upload.name = "artwork.png"
            with Image.open(io.BytesIO(image)) as original:
                width, height = original.size
            size = f"{width}x{height}" if width % 16 == 0 and height % 16 == 0 else "auto"
            try:
                result = await self.client.images.edit(
                    model=self.settings.openai_image_model,
                    image=upload,
                    prompt=prompt,
                    size=size,
                    quality=self.settings.openai_image_revision_quality,
                    background="transparent",
                    output_format="png",
                )
            except APIStatusError as exc:
                self._handle_api_error(exc)
                raise
            if not result.data or not result.data[0].b64_json:
                raise RuntimeError("OpenAI returned no revised artwork")
            usage = result.usage.model_dump() if result.usage else None
            return base64.b64decode(result.data[0].b64_json), {
                "model": self.settings.openai_image_model,
                "quality": result.quality or self.settings.openai_image_revision_quality,
                "size": result.size,
                "usage": usage,
                "estimated_cost_usd": estimate_image_cost(self.settings.openai_image_model, usage),
                "prompt": prompt,
                "prompt_version": PROMPT_VERSION,
                "schema_name": "RasterArtworkRevision",
                "schema_version": "1",
            }
        return image, self._fake_metadata("revision", prompt)

    async def visual_qa(
        self, image: bytes, brief: CreativeBrief, deterministic: QAReport,
        *, effects: dict[str, Any] | None = None,
    ) -> ModelResult[QAReport]:
        prompt = QA_PROMPT.format(
            brief=brief.model_dump_json(indent=2),
            slogan=brief.slogan or "",
            effects=json.dumps(effects or {}),
            shirt_colors=brief.shirt_colors,
            deterministic=deterministic.model_dump_json(indent=2),
        )
        if self.client:
            return await self._parse(prompt, QAReport, image=image)
        return ModelResult(deterministic, self._fake_metadata("visual_qa", prompt))

    async def rank_shirt_colors(
        self, preview: bytes, candidates: list[dict[str, Any]]
    ) -> ModelResult[ShirtColorRanking]:
        prompt = SHIRT_COLOR_PROMPT.format(candidates=json.dumps(candidates))
        if self.client:
            return await self._parse(prompt, ShirtColorRanking, image=preview)
        ranking = ShirtColorRanking(
            scores=[
                ShirtColorScore(candidate_id=int(item["candidate_id"]), score=50, reason="Fixture score")
                for item in candidates
            ]
        )
        return ModelResult(ranking, self._fake_metadata("featured_color", prompt))

    async def listings(
        self, product_template: dict[str, Any], brief: CreativeBrief, research_summary: str
    ) -> ModelResult[MarketplaceListingSet]:
        prompt = LISTING_PROMPT.format(
            product_template=json.dumps(product_template),
            brief=brief.model_dump_json(indent=2),
            research_summary=research_summary,
        )
        if self.client:
            return await self._parse(
                prompt, MarketplaceListingSet, model=self.settings.openai_listing_model
            )
        items = []
        for channel in Channel:
            disclosure = (
                " Seller-prompted AI assisted the original artwork; Printify is the production partner."
                if channel is Channel.ETSY
                else ""
            )
            title = "Trail Graphic T-Shirt for Outdoor Days"
            items.append(
                MarketplaceListing(
                    channel=channel,
                    title=title,
                    short_description="Bring a little trail spirit to an ordinary day.",
                    long_description=(
                        "Trail Graphic T-Shirt for outdoor days and gifts for hikers. "
                        "The sunrise and winding trail artwork adds a quiet reminder of the next adventure. "
                        "Choose from the listed sizes and colors; printed to order with DTG."
                        f"{disclosure}"
                    ),
                    tags=["outdoor shirt", "hiking gift", "original graphic"],
                    bullet_points=[
                        "Original centered artwork",
                        "Printed to order",
                        "See listing for size details",
                    ],
                    alt_text="Retro sunrise and switchback trail graphic",
                    target_customer=brief.target_customer,
                    gift_occasions=["birthday", "holiday", "outdoor trip"],
                    seo_meta_title=title,
                    seo_meta_description="Original trail-inspired graphic T-shirt printed to order.",
                )
            )
        return ModelResult(
            MarketplaceListingSet(listings=items), self._fake_metadata("listings", prompt)
        )

    async def polish_listings(
        self,
        product_template: dict[str, Any],
        brief: CreativeBrief,
        drafts: MarketplaceListingSet,
    ) -> ModelResult[MarketplaceListingSet]:
        prompt = LISTING_POLISH_PROMPT.format(
            product_template=json.dumps(product_template),
            brief=brief.model_dump_json(indent=2),
            drafts=drafts.model_dump_json(indent=2),
        )
        if self.client:
            return await self._parse(
                prompt, MarketplaceListingSet, model=self.settings.openai_listing_model
            )
        return ModelResult(drafts, self._fake_metadata("listing_polish", prompt))

    @staticmethod
    def _fake_metadata(kind: str, prompt: str) -> dict[str, Any]:
        return {
            "response_id": f"fixture-{kind}",
            "model": "fixture",
            "usage": None,
            "estimated_cost_usd": 0.0,
            "prompt": prompt,
            "prompt_version": PROMPT_VERSION,
            "schema_name": kind,
            "schema_version": "1",
        }
