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
from merch.domain.concept_ranking import (
    concept_score_breakdown,
    selection_candidates,
    weighted_concept_score,
)
from merch.domain.prepress import make_fixture_art
from merch.prompts import (
    ARTWORK_PROMPT,
    BRIEF_REWRITE_PROMPT,
    CATALOG_ARTWORK_PROMPT,
    CREATIVE_PROMPT,
    ETSY_CATALOG_LISTING_PROMPT,
    IP_PROMPT,
    LISTING_POLISH_PROMPT,
    LISTING_PROMPT,
    MARKETPLACE_FALLBACK_PROMPT,
    ORIGINALITY_ASSESSMENT_PROMPT,
    PROMPT_VERSION,
    QA_PROMPT,
    REFERENCE_ANALYSIS_PROMPT,
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
    ConceptStrategy,
    CreativeBrief,
    DesignMode,
    Evidence,
    IPScreeningReport,
    MarketplaceListing,
    MarketplaceListingSet,
    MarketplaceSearchFallback,
    MarketplaceSource,
    NewResearchReport,
    OriginalityVisionAssessment,
    PrintSurface,
    ProductOpportunity,
    ProductPlanV2,
    QAIssue,
    QAReport,
    ReferenceAnalysis,
    RejectedConcept,
    ResearchReport,
    SalesSignal,
    SearchFallbackListing,
    SelectionDecision,
    SEOEvidence,
    ShirtColorRanking,
    ShirtColorScore,
    TypographyProposal,
    TypographySpec,
    normalize_opaque_color,
    normalize_slogan,
)
from merch.services.openai_costs import estimate_image_cost, estimate_text_cost


@dataclass(frozen=True)
class ModelResult[T: BaseModel]:
    value: T
    metadata: dict[str, Any]


class OpenAINonRetryableError(RuntimeError):
    """An OpenAI billing, credential, or request error that a retry cannot fix."""


def _canonical_line_breaks(slogan: str, proposed: list[str]) -> list[str]:
    """Keep valid soft wraps without allowing one to cross an approved hard break."""
    if not proposed or any(not line.strip() for line in proposed):
        return slogan.split("\n")
    index = 0
    for hard_line in slogan.split("\n"):
        wrapped: list[str] = []
        while index < len(proposed):
            wrapped.append(proposed[index])
            index += 1
            joined = " ".join(wrapped)
            if joined == hard_line:
                break
            if not hard_line.startswith(joined + " "):
                return slogan.split("\n")
        else:
            return slogan.split("\n")
    return proposed if index == len(proposed) else slogan.split("\n")


def canonicalize_typography(
    slogan: str, brief: CreativeBrief, spec: TypographyProposal
) -> TypographySpec:
    """Bind model-chosen styling to validated deterministic text and placement contracts."""
    approved = normalize_slogan(slogan)
    if approved is None:
        raise ValueError("Typography requires a nonblank approved slogan")
    try:
        primary_color = normalize_opaque_color(spec.primary_color)
    except ValueError:
        primary_color = "#F7F3E8"
        for candidate in brief.palette:
            try:
                primary_color = normalize_opaque_color(candidate)
                break
            except ValueError:
                continue
    return TypographySpec.model_validate(
        {
            **spec.model_dump(mode="python"),
            "exact_text": approved,
            "line_breaks": _canonical_line_breaks(approved, spec.line_breaks),
            "primary_color": primary_color,
            "vertical_placement": (
                "center" if brief.design_mode == DesignMode.TYPOGRAPHY else "bottom"
            ),
        }
    )


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
        self, current_date: date, performance_summary: str,
        *, product_context: dict[str, Any] | None = None,
        recent_concepts: list[dict[str, Any]] | None = None,
    ) -> ModelResult[ResearchReport]:
        prompt = RESEARCH_PROMPT.format(
            current_date=current_date.isoformat(),
            performance_summary=performance_summary,
            product_context=json.dumps(product_context or {}),
            recent_concepts=json.dumps(recent_concepts or []),
            ip_risk_instruction=(
                "Score IP risk from 0-100, where 0 is safest and 100 is riskiest."
                if self.settings.ip_check_enabled
                else "Set ip_risk to 0 for every candidate."
            ),
        )
        if self.client:
            result = await self._parse(
                prompt, NewResearchReport, web_search=True, model=self.settings.openai_research_model
            )
            # Validate the fresh contract even when a mocked/custom provider bypasses parsing.
            report = NewResearchReport.model_validate(result.value.model_dump())
            return ModelResult(report, result.metadata)
        # These are fictional development fixtures, not claimed market findings.
        ideas = [
            ("Trail Ritual 1", "quiet morning hikers", "The early trail is a quiet commuter route", "A sunrise nested inside a simple switchback trail silhouette", "TAKE THE SCENIC ROUTE"),
            ("Trail Ritual 2", "hikers who pack more snacks than gear", "Trail maintenance is mostly snack breaks", "A tiny backpack overflowing with trail snacks beside a mountain", None),
            ("Pigeon Lunch Bureau", "city walkers who share snacks with birds", "Pigeons conduct solemn lunch inspections", "A dignified pigeon guarding a single pretzel", "PIGEON LUNCH BUREAU"),
            ("After Hours Reading", "readers with overdue library books", "A skeleton librarian works the never-ending late shift", "A skeleton reading behind a stack of library returns", "AFTER HOURS READING DEPT."),
            ("Raccoon Night Inventory", "night owls who love convenience-store snacks", "A raccoon takes snack inventory far too seriously", "A raccoon examining a paper snack bag with a small clipboard", None),
            ("Moss Inspection Club", "gardeners who admire things growing slowly", "A snail is the chief inspector of moss", "A snail studying a patch of moss through a magnifying glass", "MOSS INSPECTION CLUB"),
            ("Lunar Coffee Watch", "amateur astronomers fueled by coffee", "A coffee cup serves as a miniature lunar observatory", "A telescope beside a coffee mug beneath a crescent moon", None),
            ("Desktop Cat Archives", "programmers whose cats interrupt work", "A cat supervises an obsolete computer archive", "A cat asleep on a chunky retro computer monitor", "DESKTOP CAT ARCHIVES"),
            ("Cardio Exemption Office", "lifters who dislike cardio", "A tortoise issues official cardio exemptions", "A serious tortoise holding a small dumbbell", "CARDIO EXEMPTION OFFICE"),
            ("Solo Cast Society", "fly fishers who prefer quiet company", "The club has room for exactly one chair", "A fishing rod beside one folding chair at a calm river", "SOLO CAST SOCIETY"),
            ("Botanical Night Shift", "plant lovers drawn to gothic illustration", "Moths tend an imaginary moonlit greenhouse", "Two broad-winged moths hovering over a night-blooming flower", None),
            ("Desert Book Courier", "readers with a Western sense of humor", "A pack mule delivers an unreasonable number of books", "A mule carrying two neatly stacked book panniers", "DESERT BOOK COURIER"),
            ("Failed Parking Club", "drivers who love tiny old hatchbacks", "An insignificant hatchback receives grand racing treatment", "A small boxy hatchback next to an oversized traffic cone", "FAILED PARKING CLUB"),
            ("Midnight Stitch Union", "crocheters who promise one last row", "An owl runs the night shift at a yarn workshop", "An owl holding a crochet hook beside a ball of yarn", "MIDNIGHT STITCH UNION"),
            ("Low Stakes Bowling", "casual bowlers who enjoy the social ritual", "A bowling trophy celebrates simply showing up", "A humble bowling pin resting on a tiny trophy pedestal", None),
            ("Sourdough Field Station", "home bakers scheduling life around starter", "A jar of starter receives expedition-level attention", "A starter jar beside a kitchen timer and small field notebook", "SOURDOUGH FIELD STATION"),
            ("Urban Pond Committee", "birdwatchers delighted by ordinary ducks", "Ducks hold a very important puddle meeting", "Three ducks gathered around a small puddle", "URBAN POND COMMITTEE"),
            ("Weekend Repair Society", "motorcycle tinkerers with unfinished projects", "A patient possum manages an endless repair queue", "A possum examining one loose motorcycle wheel", None),
            ("Small Hill Expedition", "runners who dramatically dislike hills", "A tiny incline is treated as an alpine expedition", "One running shoe atop a modest rounded hill", "SMALL HILL EXPEDITION"),
            ("Mushroom Records Office", "foragers who keep meticulous nature notes", "Mushrooms form a tiny botanical archive", "A broad mushroom cap sheltering a field notebook", "MUSHROOM RECORDS OFFICE"),
            ("Unhurried Pickleball", "recreational pickleball players between snack breaks", "A sloth is the club's most composed player", "A sloth resting a paddle on one shoulder", None),
            ("Lost Dice Department", "tabletop gamers who lose dice under furniture", "A mouse operates the lost-and-found for runaway dice", "A mouse pushing an oversized plain six-sided die", "LOST DICE DEPARTMENT"),
            ("Cowboy Compost Crew", "gardeners who like Western imagery", "A worm works a very small ranch", "A worm in a plain cowboy hat beside a compost leaf", "COWBOY COMPOST CREW"),
            ("Early Exit Social", "introverts who leave parties for their dog", "A dog proudly manages its human's departure schedule", "A dog holding a leash beside a small clock", None),
            ("Sunday Cloud Survey", "campers who prefer resting to conquering peaks", "A hammock is an official cloud-observation station", "A hammock below one generous cloud", "SUNDAY CLOUD SURVEY"),
        ]
        concepts = []
        for index, (name, audience, premise, visual, slogan) in enumerate(ideas):
            concepts.append(
                CandidateConcept(
                    concept_name=name,
                    target_customer=audience,
                    customer_motivation=f"Recognize a familiar ritual and find a specific gift for {audience}",
                    trend_evidence=["Fixture demand signal for deterministic local development"],
                    why_now="Fixture evergreen identity; live research must establish current relevance",
                    slogan_if_any=slogan,
                    visual_concept=visual,
                    design_mode=DesignMode.HYBRID if slogan else DesignMode.ILLUSTRATION,
                    graphic_style="original vintage hand-drawn club illustration",
                    palette=["#EF5E50", "#FFD166", "#F7F3E8"],
                    recommended_shirt_colors=["#111827", "#1F3A32"],
                    seasonality="year-round with spring and fall peaks",
                    estimated_trend_window="12 weeks",
                    competitive_advantage="Readable one-second silhouette with a specific ritual cue",
                    risks=["Fixture scores are synthetic; real demand and competition remain unverified"],
                    strategy=ConceptStrategy(
                        micro_niche=audience,
                        premise=premise,
                        brand_connection="A specific everyday identity treated as a serious vintage institution",
                        brand_fit=90 - index,
                        shareability=88 - index,
                        etsy_angle=f"An identity gift for {audience}",
                        amazon_angle=f"A clear, searchable shirt for {audience}",
                        shopify_angle=f"Odd Hour Press presents {premise.lower()}",
                    ),
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
                            kind="inference",
                            supports=["demand"],
                            excerpt="Synthetic development fixture; no measured demand or competition.",
                            limitations=["Fixture only: not evidence of real marketplace performance"],
                        )
                    ],
                )
            )
        report = NewResearchReport(
            current_date=current_date,
            market_summary="Deterministic fixture research for local and CI execution.",
            candidates=concepts,
        )
        return ModelResult(report, self._fake_metadata("research", prompt))

    async def marketplace_fallback(
        self,
        marketplace: MarketplaceSource,
        query: str,
        *,
        current_time: str,
    ) -> ModelResult[MarketplaceSearchFallback]:
        prompt = MARKETPLACE_FALLBACK_PROMPT.format(
            marketplace=marketplace.value,
            query=query,
            current_time=current_time,
        )
        if self.client:
            return await self._parse(
                prompt,
                MarketplaceSearchFallback,
                web_search=True,
                model=self.settings.openai_research_model,
            )
        observed = __import__("datetime").datetime.fromisoformat(current_time)
        listings = [
            SearchFallbackListing(
                external_listing_id=f"fixture-{marketplace.value}-{index}",
                url=f"https://example.com/{marketplace.value}/{index}",
                title=f"Fixture popular {query} {index}",
                seller=f"Fixture seller {index}",
                displayed_price_cents=1999 + index * 100,
                shipping_price_cents=0,
                rating=4.7,
                review_count=100 + index,
                sales_signals=[
                    SalesSignal(
                        kind=("bestseller_badge" if index == 1 else "review_count"),
                        value=float(1 if index == 1 else 100 + index),
                        label=("Visible bestseller badge" if index == 1 else "Visible reviews"),
                        explicit=index == 1,
                        observed_at=observed,
                    )
                ],
                limitations=["Synthetic fixture evidence"],
            )
            for index in range(1, 4)
        ]
        return ModelResult(
            MarketplaceSearchFallback(
                marketplace=marketplace, query=query, listings=listings
            ),
            self._fake_metadata("marketplace_fallback", prompt),
        )

    async def select(self, concepts: list[CandidateConcept]) -> ModelResult[SelectionDecision]:
        if not concepts:
            raise ValueError("at least one eligible concept is required")
        include_ip_risk = self.settings.ip_check_enabled
        shortlist = selection_candidates(concepts, include_ip_risk)
        strategy_ranking = any(item.strategy is not None for item in concepts)
        concept_data = [
            {
                **item.model_dump(mode="json"),
                "canonical_ranking": concept_score_breakdown(item, include_ip_risk),
            }
            for item in shortlist
        ]
        if not self.settings.ip_check_enabled:
            for item in concept_data:
                item["scores"]["ip_risk"] = 0
        prompt = SELECTION_PROMPT.format(
            concepts=json.dumps(concept_data),
            ranking_policy=(
                "This is the top three eligible concepts within five points of the leader. "
                "Canonical weights: 20% demand, 20% purchase intent, 10% novelty, "
                "10% low competition, 10% printability, 10% brand fit, 10% shareability, "
                "5% trend velocity and 5% longevity. Evidence discounts demand, "
                "competition and velocity toward neutral 50. Choose the strongest premise "
                "among these close alternatives and explain your choice."
                if strategy_ranking else
                "Legacy weights: 25% demand, 20% trend acceleration, 15% purchase intent, "
                "15% originality, 10% low saturation, 10% print quality potential, and 5% longevity."
            ),
            selection_penalties=(
                "IP uncertainty" if self.settings.ip_check_enabled else "weak originality"
            ),
        )
        if self.client:
            result = await self._parse(
                prompt,
                SelectionDecision,
                reasoning_effort=self.settings.openai_creative_reasoning_effort,
            )
            if not strategy_ranking:
                return result
            selected = next(
                (item for item in shortlist if item.concept_name == result.value.selected_concept_name),
                shortlist[0],
            )
            valid_choice = selected.concept_name == result.value.selected_concept_name
            decision = result.value.model_copy(update={
                "selected_concept_name": selected.concept_name,
                "weighted_score": weighted_concept_score(selected, include_ip_risk),
                "rationale": (
                    result.value.rationale if valid_choice else
                    "Model selected outside the eligible shortlist; used the canonical leader."
                ),
                "rejected_concepts": [
                    RejectedConcept(
                        concept_name=item.concept_name,
                        reason=(
                            "Not chosen after comparing the leading premises"
                            if item in shortlist else "Below the commercial shortlist"
                        ),
                    )
                    for item in concepts if item.concept_name != selected.concept_name
                ],
            })
            return ModelResult(decision, {
                **result.metadata,
                "selection_shortlist": [item.concept_name for item in shortlist],
                "selection_fallback": not valid_choice,
            })

        def score(item: CandidateConcept) -> float:
            return weighted_concept_score(item, self.settings.ip_check_enabled)

        ranked = (
            sorted(concepts, key=lambda item: (-score(item), item.concept_name.casefold()))
            if strategy_ranking else sorted(concepts, key=score, reverse=True)
        )
        decision = SelectionDecision(
            selected_concept_name=ranked[0].concept_name,
            rationale="Highest deterministic weighted commercial score among eligible concepts.",
            weighted_score=score(ranked[0]),
            rejected_concepts=[
                RejectedConcept(concept_name=item.concept_name, reason="Lower weighted score")
                for item in ranked[1:]
            ],
        )
        metadata = self._fake_metadata("selection", prompt)
        if strategy_ranking:
            metadata["selection_shortlist"] = [item.concept_name for item in shortlist]
            metadata["selection_fallback"] = False
        return ModelResult(decision, metadata)

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

    async def analyze_references(
        self, opportunity: ProductOpportunity, reference_sheet: bytes
    ) -> ModelResult[ReferenceAnalysis]:
        reference_ids = [
            item.external_listing_id for item in opportunity.comparable_listings[:3]
        ]
        prompt = REFERENCE_ANALYSIS_PROMPT.format(
            opportunity=opportunity.model_dump_json(indent=2),
            reference_ids=json.dumps(reference_ids),
        )
        if self.client:
            return await self._parse(
                prompt,
                ReferenceAnalysis,
                image=reference_sheet,
                reasoning_effort=self.settings.openai_creative_reasoning_effort,
            )
        analysis = ReferenceAnalysis(
            reusable_patterns=[
                "Clear primary focal hierarchy",
                "Product-appropriate centered placement",
                "Limited high-contrast palette",
            ],
            forbidden_elements=[
                *(item.title for item in opportunity.comparable_listings[:3]),
                *(item.seller for item in opportunity.comparable_listings[:3] if item.seller),
            ],
            transformation_brief=(
                f"Create new wording, motifs, composition, and line work for "
                f"{opportunity.visual_direction}; retain only broad demand patterns."
            ),
            reference_listing_ids=reference_ids,
        )
        return ModelResult(analysis, self._fake_metadata("reference_analysis", prompt))

    async def catalog_artwork(
        self,
        opportunity: ProductOpportunity,
        analysis: ReferenceAnalysis,
        surface: PrintSurface,
        reference_sheet: bytes,
    ) -> tuple[bytes, dict[str, Any]]:
        prompt = CATALOG_ARTWORK_PROMPT.format(
            opportunity=opportunity.model_dump_json(indent=2),
            reference_analysis=analysis.model_dump_json(indent=2),
            surface=surface.model_dump_json(indent=2),
        )
        if self.client:
            upload = io.BytesIO(reference_sheet)
            upload.name = "marketplace-references.png"
            background: Literal["transparent", "opaque"] = (
                "transparent"
                if surface.placement in {"placed", "restricted_palette"}
                else "opaque"
            )
            try:
                result = await self.client.images.edit(
                    model=self.settings.openai_image_model,
                    image=upload,
                    prompt=prompt,
                    size=f"{surface.width}x{surface.height}",
                    quality=self.settings.openai_image_quality,
                    background=background,
                    output_format="png",
                )
            except APIStatusError as exc:
                self._handle_api_error(exc)
                raise
            if not result.data or not result.data[0].b64_json:
                raise RuntimeError("OpenAI returned no catalog artwork")
            usage = result.usage.model_dump() if result.usage else None
            return base64.b64decode(result.data[0].b64_json), {
                "model": self.settings.openai_image_model,
                "quality": result.quality or self.settings.openai_image_quality,
                "size": result.size or f"{surface.width}x{surface.height}",
                "usage": usage,
                "estimated_cost_usd": estimate_image_cost(
                    self.settings.openai_image_model, usage
                ),
                "prompt": prompt,
                "prompt_version": PROMPT_VERSION,
                "schema_name": "CatalogRasterArtwork",
                "schema_version": "2",
            }
        fixture = make_fixture_art(surface.width, surface.height)
        if surface.placement in {"full_bleed", "repeat"}:
            with Image.open(io.BytesIO(fixture)) as source:
                flattened = Image.new("RGBA", source.size, "#274C77")
                flattened.alpha_composite(source.convert("RGBA"))
                output = io.BytesIO()
                flattened.convert("RGB").save(output, format="PNG")
                fixture = output.getvalue()
        return fixture, self._fake_metadata("catalog_artwork", prompt)

    async def originality_assessment(
        self, comparison_sheet: bytes, reference_ids: list[str]
    ) -> ModelResult[OriginalityVisionAssessment]:
        prompt = ORIGINALITY_ASSESSMENT_PROMPT.format(
            reference_ids=json.dumps(reference_ids)
        )
        if self.client:
            return await self._parse(
                prompt,
                OriginalityVisionAssessment,
                image=comparison_sheet,
                reasoning_effort=self.settings.openai_creative_reasoning_effort,
            )
        return ModelResult(
            OriginalityVisionAssessment(
                originality_score=95,
                copying_risk=5,
                reasons=["Fixture artwork is structurally distinct from fixture references"],
            ),
            self._fake_metadata("originality_assessment", prompt),
        )

    async def etsy_catalog_listing(
        self,
        opportunity: ProductOpportunity,
        plan: ProductPlanV2,
        seo: SEOEvidence,
        analysis: ReferenceAnalysis,
    ) -> ModelResult[MarketplaceListingSet]:
        prompt = ETSY_CATALOG_LISTING_PROMPT.format(
            opportunity=opportunity.model_dump_json(indent=2),
            product_plan=plan.model_dump_json(indent=2),
            seo_evidence=seo.model_dump_json(indent=2),
            reference_analysis=analysis.model_dump_json(indent=2),
        )
        if self.client:
            result = await self._parse(
                prompt, MarketplaceListing, model=self.settings.openai_listing_model
            )
            listing = result.value.model_copy(update={"channel": Channel.ETSY})
            return ModelResult(MarketplaceListingSet(listings=[listing]), result.metadata)
        included = [item.phrase for item in seo.keywords if item.included]
        tags = [item[:20] for item in included[:13]] or ["original gift"]
        title = f"{opportunity.concept_name} {plan.product_title}"[:140]
        disclosure = (
            "Seller-prompted AI assisted the original artwork; "
            "Printify is the production partner."
        )
        listing = MarketplaceListing(
            channel=Channel.ETSY,
            title=title,
            short_description=(
                f"An original {plan.product_title} for {opportunity.target_customer}."
            ),
            long_description=(
                f"A trend-led but original {plan.product_title} built around "
                f"{opportunity.visual_direction}\n\n{disclosure}"
            ),
            tags=tags,
            bullet_points=["Original transformed artwork", "Printed to order"],
            alt_text=f"Original artwork on {plan.product_title}",
            target_customer=opportunity.target_customer,
            gift_occasions=["birthday", "holiday"],
            seo_meta_title=title,
            seo_meta_description=f"Original {plan.product_title} printed to order.",
        )
        return ModelResult(
            MarketplaceListingSet(listings=[listing]),
            self._fake_metadata("etsy_catalog_listing", prompt),
        )

    async def creative(
        self, concept: CandidateConcept, product_template: dict[str, Any]
    ) -> ModelResult[CreativeBrief]:
        prompt = CREATIVE_PROMPT.format(
            concept=concept.model_dump_json(indent=2),
            product_template=json.dumps(product_template),
        )
        if self.client:
            result = await self._parse(
                prompt,
                CreativeBrief,
                reasoning_effort=self.settings.openai_creative_reasoning_effort,
            )
            return ModelResult(
                result.value.model_copy(update={
                    "strategy": concept.strategy,
                    "slogan": concept.slogan_if_any,
                }),
                result.metadata,
            )
        brief = CreativeBrief(
            concept_name=concept.concept_name,
            target_customer=concept.target_customer,
            customer_motivation=concept.customer_motivation,
            slogan=concept.slogan_if_any,
            design_mode=concept.design_mode,
            visual_concept=concept.visual_concept,
            composition="Centered primary illustration with generous spacing and any exact slogan beneath",
            graphic_style=concept.graphic_style,
            palette=concept.palette,
            shirt_colors=concept.recommended_shirt_colors,
            typography_style="bold geometric sans",
            generation_brief=f"Original illustration: {concept.visual_concept}. Isolated, no text or texture.",
            strategy=concept.strategy,
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
                result.value.model_copy(update={"strategy": brief.strategy, "slogan": brief.slogan}),
                {**result.metadata, "recovery_context": context.to_dict()},
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
            result = await self._parse(
                prompt, TypographyProposal, model=self.settings.openai_typography_model
            )
            return ModelResult(
                canonicalize_typography(slogan, brief, result.value), result.metadata
            )
        spec = TypographySpec(
            exact_text=slogan,
            line_breaks=slogan.split("\n"),
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
        return ModelResult(
            canonicalize_typography(slogan, brief, spec),
            self._fake_metadata("typography", prompt),
        )

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
