from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from PIL import ImageColor
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    field_validator,
    model_validator,
)

Score = Annotated[int, Field(ge=0, le=100)]
MAX_SLOGAN_CHARS = 240

_CSS_HEX_COLOR = re.compile(
    r"#[0-9A-Fa-f]{8}\b|#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{4}\b|#[0-9A-Fa-f]{3}\b"
)


def normalize_opaque_color(value: object) -> str:
    """Normalize one Pillow color or the first embedded hex color to opaque RGB."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("color must be a nonempty string")
    candidate = value.strip()
    match = _CSS_HEX_COLOR.search(candidate)
    if match is not None:
        candidate = match.group()
    try:
        red, green, blue, alpha = cast(
            tuple[int, int, int, int], ImageColor.getcolor(candidate, "RGBA")
        )
    except ValueError as exc:
        raise ValueError("color must be a concrete CSS color or contain a hex color") from exc
    if alpha != 255:
        raise ValueError("color must be fully opaque")
    return f"#{red:02X}{green:02X}{blue:02X}"


def normalize_slogan(value: object) -> str | None:
    """Keep approved wording while removing unusable blank hard-line boundaries."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("slogan must be text or null")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    result = "\n".join(lines) or None
    if result is not None and len(result) > MAX_SLOGAN_CHARS:
        raise ValueError(f"slogan must be at most {MAX_SLOGAN_CHARS} characters")
    return result


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class Channel(StrEnum):
    SHOPIFY = "shopify"
    ETSY = "etsy"
    AMAZON_US = "amazon_us"


class MarketplaceSource(StrEnum):
    """Marketplaces observed during research, independent of publish channels."""

    ETSY = "etsy"
    AMAZON_US = "amazon_us"
    TIKTOK_SHOP = "tiktok_shop"
    WALMART = "walmart"
    EBAY = "ebay"


class DesignMode(StrEnum):
    ILLUSTRATION = "illustration"
    TYPOGRAPHY = "typography"
    HYBRID = "hybrid"


class RunStatus(StrEnum):
    PENDING = "pending"
    RESEARCHING = "researching"
    SCREENING = "screening"
    RANKING = "ranking"
    GENERATING = "generating"
    PREPRESS = "prepress"
    QA = "qa"
    AWAITING_BRIEF_REVISION = "awaiting_brief_revision"
    LISTING = "listing"
    AWAITING_APPROVAL = "awaiting_approval"
    PUBLISHING = "publishing"
    VERIFICATION_REQUIRED = "verification_required"
    PARTIALLY_PUBLISHED = "partially_published"
    PUBLISHED = "published"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"
    NO_SAFE_CANDIDATE = "no_safe_candidate"
    NO_QUALIFIED_OPPORTUNITY = "no_qualified_opportunity"


class PublishStatus(StrEnum):
    PENDING = "pending"
    DRY_RUN = "dry_run"
    CREATING = "creating"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Evidence(StrictModel):
    claim: str
    title: str
    url: str
    published_at: date | None = None
    accessed_at: datetime | None = None
    excerpt: str | None = None
    kind: Literal["observed_metric", "marketplace_proxy", "editorial", "inference", "unknown"] = (
        "unknown"
    )
    supports: list[Literal["demand", "competition", "trend_velocity"]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def valid_http_url(cls, value: str) -> str:
        # OpenAI Structured Outputs rejects Pydantic's `format: uri` schema.
        # Keep URL validation locally while exposing a plain string to the API.
        return str(TypeAdapter(HttpUrl).validate_python(value))


class ConceptScores(StrictModel):
    demand: Score
    trend_velocity: Score
    novelty: Score
    purchase_intent: Score
    printability: Score
    competition: Score
    longevity: Score
    ip_risk: Score


class ConceptStrategy(StrictModel):
    micro_niche: str
    premise: str
    brand_connection: str
    brand_fit: Score
    shareability: Score
    etsy_angle: str
    amazon_angle: str
    shopify_angle: str


class CandidateConcept(StrictModel):
    concept_name: str
    target_customer: str
    customer_motivation: str
    trend_evidence: list[str]
    why_now: str
    slogan_if_any: str | None
    visual_concept: str
    design_mode: DesignMode
    graphic_style: str
    palette: list[str]
    recommended_shirt_colors: list[str]
    seasonality: str
    estimated_trend_window: str
    competitive_advantage: str
    risks: list[str]
    scores: ConceptScores
    evidence: list[Evidence]
    strategy: ConceptStrategy | None = None

    @field_validator("slogan_if_any", mode="before")
    @classmethod
    def slogan_has_printable_lines(cls, value: object) -> str | None:
        return normalize_slogan(value)


class ResearchReport(StrictModel):
    current_date: date
    market_summary: str
    candidates: Annotated[list[CandidateConcept], Field(min_length=10, max_length=30)]


class NewResearchReport(ResearchReport):
    candidates: Annotated[list[CandidateConcept], Field(min_length=25, max_length=25)]

    @model_validator(mode="after")
    def distinct_strategy_concepts(self) -> NewResearchReport:
        names = [" ".join(item.concept_name.split()).casefold() for item in self.candidates]
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("new research requires 25 distinct nonempty concept names")
        if any(item.strategy is None for item in self.candidates):
            raise ValueError("every new research candidate requires a strategy")
        return self


class RejectedConcept(StrictModel):
    concept_name: str
    reason: str


class SelectionDecision(StrictModel):
    selected_concept_name: str
    rationale: str
    weighted_score: float = Field(ge=0, le=100)
    rejected_concepts: list[RejectedConcept]


class CreativeBrief(StrictModel):
    concept_name: str
    target_customer: str
    customer_motivation: str
    slogan: str | None
    design_mode: DesignMode
    visual_concept: str
    composition: str
    graphic_style: str
    palette: list[str]
    shirt_colors: list[str]
    print_method: Literal["dtg", "dtf"] = "dtg"
    typography_style: str | None
    generation_brief: str
    artwork_distress_level: int = Field(default=0, ge=0, le=5)
    strategy: ConceptStrategy | None = None

    @field_validator("slogan", mode="before")
    @classmethod
    def slogan_has_printable_lines(cls, value: object) -> str | None:
        return normalize_slogan(value)


class TypographyProposal(StrictModel):
    exact_text: str
    font_category: Literal["sans", "serif", "slab", "display", "mono"] = "sans"
    font_weight: Annotated[int, Field(ge=100, le=900)] = 700
    capitalization: Literal["exact"] = "exact"
    line_breaks: list[str]
    letter_spacing: float = Field(ge=-0.1, le=0.5)
    line_spacing: float = Field(ge=0.7, le=2.0)
    text_alignment: Literal["left", "center", "right"]
    text_arc_or_shape: Literal["none", "up", "down"]
    outline: str | None
    shadow: str | None
    distress_level: int = Field(ge=0, le=5)
    primary_color: str
    secondary_color: str | None
    vertical_placement: Literal["top", "center", "bottom"] = "center"
    interaction_with_illustration: str
    relative_width: float = Field(gt=0, le=1)
    relative_height: float = Field(gt=0, le=1)


class TypographySpec(TypographyProposal):
    line_breaks: Annotated[list[str], Field(min_length=1)]

    @field_validator("primary_color", mode="before")
    @classmethod
    def primary_color_is_opaque_hex(cls, value: object) -> str:
        return normalize_opaque_color(value)

    @field_validator("outline", "shadow", "secondary_color", mode="before")
    @classmethod
    def optional_colors_are_opaque_hex(cls, value: object) -> str | None:
        if value is None:
            return None
        try:
            return normalize_opaque_color(value)
        except ValueError:
            return None

    @model_validator(mode="after")
    def text_is_exact(self) -> TypographySpec:
        if any(not line.strip() for line in self.line_breaks):
            raise ValueError("every slogan line must contain printable text")
        if " ".join(self.line_breaks) != self.exact_text.replace("\n", " "):
            raise ValueError("line breaks must preserve the exact slogan")
        return self


class IPMatch(StrictModel):
    source: str
    term: str
    url: str | None = None
    explanation: str
    blocking: bool

    @field_validator("url")
    @classmethod
    def valid_http_url(cls, value: str | None) -> str | None:
        return str(TypeAdapter(HttpUrl).validate_python(value)) if value is not None else None


class IPScreeningReport(StrictModel):
    status: Literal["pass", "review", "block"]
    risk_score: Score
    searched_terms: list[str]
    matches: list[IPMatch]
    uspto_search_url: str
    notes: list[str]
    legal_clearance: Literal[False] = False

    @field_validator("uspto_search_url")
    @classmethod
    def valid_http_url(cls, value: str) -> str:
        return str(TypeAdapter(HttpUrl).validate_python(value))


class QAIssue(StrictModel):
    code: str
    severity: Literal["warning", "error"]
    message: str
    recommended_fix: str | None = None
    affected_shirt_colors: list[str] = Field(default_factory=list)


class QAReport(StrictModel):
    passed: bool
    revision: int = Field(ge=1)
    issues: list[QAIssue]
    width: int
    height: int
    has_alpha: bool
    color_profile: str


class ShirtColorScore(StrictModel):
    candidate_id: int = Field(ge=1)
    score: Score
    reason: str


class ShirtColorRanking(StrictModel):
    scores: list[ShirtColorScore]


class MarketplaceListing(StrictModel):
    channel: Channel
    title: str
    short_description: str
    long_description: str
    tags: list[str]
    bullet_points: list[str]
    alt_text: str
    target_customer: str
    gift_occasions: list[str]
    seo_meta_title: str
    seo_meta_description: str


class MarketplaceListingSet(StrictModel):
    listings: Annotated[list[MarketplaceListing], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def one_listing_per_channel(self) -> MarketplaceListingSet:
        channels = [item.channel for item in self.listings]
        if len(channels) != len(set(channels)):
            raise ValueError("listing set must contain at most one listing per channel")
        return self


class PrintSurface(StrictModel):
    position: str = Field(min_length=1)
    decoration_method: str = Field(min_length=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    required: bool = True
    placement: Literal["placed", "full_bleed", "repeat", "restricted_palette", "unsupported"]
    rules: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @property
    def signature(self) -> str:
        rules = ",".join(f"{key}={self.rules[key]}" for key in sorted(self.rules))
        suffix = f":{rules}" if rules else ""
        return (
            f"{self.position}:{self.decoration_method}:{self.width}x{self.height}:"
            f"{self.placement}{suffix}"
        )


class CatalogVariant(StrictModel):
    variant_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    options: dict[str, str]
    surfaces: Annotated[list[PrintSurface], Field(min_length=1)]
    available: bool = True
    production_cost_cents: int | None = Field(default=None, ge=0)
    shipping_cost_cents: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def supported_option_count(self) -> CatalogVariant:
        if len(self.options) > 3:
            raise ValueError("Printify variants may expose at most three option axes")
        return self


class CatalogProduct(StrictModel):
    blueprint_id: int = Field(gt=0)
    print_provider_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    description: str = ""
    brand: str | None = None
    model: str | None = None
    tags: list[str] = Field(default_factory=list)
    variants: Annotated[list[CatalogVariant], Field(min_length=1)]
    synced_at: datetime
    source_fingerprint: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def unique_variants(self) -> CatalogProduct:
        ids = [item.variant_id for item in self.variants]
        if len(ids) != len(set(ids)):
            raise ValueError("catalog variant IDs must be unique within a product")
        return self


class SalesSignal(StrictModel):
    kind: Literal[
        "sales_rank",
        "bestseller_badge",
        "sold_count",
        "review_velocity",
        "review_count",
        "search_placement",
        "search_snippet",
    ]
    value: float = Field(ge=0)
    label: str = Field(min_length=1)
    explicit: bool = False
    observed_at: datetime


class CompetitorListingSnapshot(StrictModel):
    marketplace: MarketplaceSource
    external_listing_id: str = Field(min_length=1)
    url: str
    title: str = Field(min_length=1)
    seller: str | None = None
    product_type: str = Field(min_length=1)
    attributes: dict[str, str] = Field(default_factory=dict)
    displayed_price_cents: int | None = Field(default=None, ge=0)
    shipping_price_cents: int | None = Field(default=None, ge=0)
    delivered_price_cents: int | None = Field(default=None, ge=0)
    currency: Literal["USD"] = "USD"
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    sales_signals: list[SalesSignal] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    collected_at: datetime
    source_method: Literal["browser", "search_fallback"]
    confidence: Score
    limitations: list[str] = Field(default_factory=list)
    page_artifact_key: str | None = None
    screenshot_artifact_key: str | None = None
    extraction_version: str = "1"

    @field_validator("url")
    @classmethod
    def listing_url_is_http(cls, value: str) -> str:
        return str(TypeAdapter(HttpUrl).validate_python(value))

    @model_validator(mode="after")
    def consistent_delivered_price(self) -> CompetitorListingSnapshot:
        if (
            self.displayed_price_cents is not None
            and self.shipping_price_cents is not None
            and self.delivered_price_cents is not None
            and self.delivered_price_cents != self.displayed_price_cents + self.shipping_price_cents
        ):
            raise ValueError("delivered price must equal displayed price plus shipping")
        return self


class SearchFallbackListing(StrictModel):
    external_listing_id: str = Field(min_length=1)
    url: str
    title: str = Field(min_length=1)
    seller: str | None = None
    displayed_price_cents: int | None = Field(default=None, ge=0)
    shipping_price_cents: int | None = Field(default=None, ge=0)
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    sales_signals: list[SalesSignal] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def fallback_url_is_http(cls, value: str) -> str:
        return str(TypeAdapter(HttpUrl).validate_python(value))


class MarketplaceSearchFallback(StrictModel):
    marketplace: MarketplaceSource
    query: str
    listings: Annotated[list[SearchFallbackListing], Field(min_length=1, max_length=10)]


class OpportunityScores(StrictModel):
    demand: Score
    evidence_confidence: Score
    purchase_intent: Score
    projected_margin: Score
    undercut_feasibility: Score
    competition_gap: Score
    trend_velocity: Score
    longevity: Score


class ProductOpportunity(StrictModel):
    opportunity_id: str = Field(min_length=1)
    concept_name: str = Field(min_length=1)
    target_customer: str = Field(min_length=1)
    product_type: str = Field(min_length=1)
    visual_direction: str = Field(min_length=1)
    keyword_phrases: list[str] = Field(default_factory=list)
    comparable_listings: Annotated[
        list[CompetitorListingSnapshot], Field(min_length=3, max_length=10)
    ]
    matched_blueprint_id: int = Field(gt=0)
    matched_print_provider_id: int = Field(gt=0)
    match_rationale: str = Field(min_length=1)
    scores: OpportunityScores
    weighted_score: float = Field(ge=0, le=100)
    eligible: bool = True
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def evidence_spans_marketplaces(self) -> ProductOpportunity:
        identities = {
            (item.marketplace, item.external_listing_id) for item in self.comparable_listings
        }
        if len(identities) != len(self.comparable_listings):
            raise ValueError("an opportunity cannot repeat a comparable listing")
        if len({item.marketplace for item in self.comparable_listings}) < 2:
            raise ValueError("an opportunity requires evidence from at least two marketplaces")
        explicit = sum(
            signal.explicit
            for listing in self.comparable_listings
            for signal in listing.sales_signals
        )
        proxy_listings = {
            (listing.marketplace, listing.external_listing_id)
            for listing in self.comparable_listings
            if any(not signal.explicit for signal in listing.sales_signals)
        }
        if explicit < 1 and len(proxy_listings) < 2:
            raise ValueError("an opportunity requires one explicit or two proxy sales signals")
        return self


class ReferenceAnalysis(StrictModel):
    reusable_patterns: list[str]
    forbidden_elements: list[str]
    transformation_brief: str = Field(min_length=1)
    reference_listing_ids: Annotated[list[str], Field(min_length=1, max_length=3)]


class SimilarityFinding(StrictModel):
    reference_listing_id: str
    perceptual_hash_distance: int | None = Field(default=None, ge=0, le=64)
    wording_similarity: float = Field(ge=0, le=1)
    vision_similarity_risk: Score
    blocking_reasons: list[str] = Field(default_factory=list)


class OriginalityReport(StrictModel):
    passed: bool
    originality_score: Score
    copying_risk: Score
    findings: list[SimilarityFinding]
    checked_at: datetime


class OriginalityVisionAssessment(StrictModel):
    originality_score: Score
    copying_risk: Score
    reasons: list[str] = Field(default_factory=list)


class SEOKeywordEvidence(StrictModel):
    phrase: str = Field(min_length=1)
    marketplaces: Annotated[list[MarketplaceSource], Field(min_length=1)]
    listing_ids: Annotated[list[str], Field(min_length=1)]
    included: bool
    exclusion_reason: str | None = None


class SEOEvidence(StrictModel):
    keywords: list[SEOKeywordEvidence]
    prohibited_terms: list[str] = Field(default_factory=list)
    generated_at: datetime


class PriceDecision(StrictModel):
    variant_id: int = Field(gt=0)
    benchmark_median_delivered_cents: int | None = Field(default=None, ge=0)
    item_price_cents: int = Field(ge=0)
    customer_shipping_cents: int = Field(ge=0)
    production_cost_cents: int = Field(ge=0)
    fulfillment_shipping_cents: int = Field(ge=0)
    estimated_fee_cents: int = Field(ge=0)
    contribution_margin: float = Field(ge=0, le=1)
    undercut_status: Literal["true", "false", "unknown"]
    reason: str


class SurfaceArtwork(StrictModel):
    surface_signature: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    placement: Literal["placed", "full_bleed", "repeat", "restricted_palette"]


class EtsyProductProfile(StrictModel):
    taxonomy_id: int = Field(gt=0)
    shipping_profile_id: int = Field(gt=0)
    return_policy_id: int = Field(gt=0)
    readiness_state_id: int = Field(gt=0)
    production_partner_ids: Annotated[list[int], Field(min_length=1)]
    max_variations_supported: Literal[2, 3] = 2
    variation_property_ids: dict[str, int] = Field(default_factory=dict)
    quantity: int = Field(default=999, ge=1)
    customer_shipping_cents: int = Field(default=0, ge=0)


class ProductPlanV2(StrictModel):
    schema_version: Literal[2] = 2
    blueprint_id: int = Field(gt=0)
    print_provider_id: int = Field(gt=0)
    product_title: str = Field(min_length=1)
    variants: Annotated[list[CatalogVariant], Field(min_length=1)]
    surface_artworks: Annotated[list[SurfaceArtwork], Field(min_length=1)]
    featured_variant_id: int = Field(gt=0)
    gallery_variant_ids: Annotated[list[int], Field(min_length=1, max_length=20)]
    etsy_profile: EtsyProductProfile
    generated_at: datetime

    @model_validator(mode="after")
    def coherent_plan(self) -> ProductPlanV2:
        variant_ids = {item.variant_id for item in self.variants}
        if len(variant_ids) != len(self.variants):
            raise ValueError("product plan variants must be unique")
        if self.featured_variant_id not in variant_ids:
            raise ValueError("featured variant must be present in the product plan")
        if not set(self.gallery_variant_ids).issubset(variant_ids):
            raise ValueError("gallery variants must be present in the product plan")
        signatures = {
            surface.signature
            for variant in self.variants
            for surface in variant.surfaces
            if surface.required
        }
        planned = [item.surface_signature for item in self.surface_artworks]
        if len(planned) != len(set(planned)) or set(planned) != signatures:
            raise ValueError("surface artwork assignments must exactly cover required surfaces")
        axes = {axis for variant in self.variants for axis in variant.options}
        if set(self.etsy_profile.variation_property_ids) != axes:
            raise ValueError("Etsy variation properties must exactly cover product option axes")
        return self


class CopyRefreshEdit(StrictModel):
    expected_version: int = Field(ge=1)
    title: str
    long_description: str
    tags: list[str]
    alt_text: str | None = Field(default=None, min_length=1, max_length=500)


class CopyRefreshApproval(StrictModel):
    expected_version: int = Field(ge=1)
    digest: str = Field(min_length=64, max_length=64)


class VariantConfig(StrictModel):
    variant_id: int
    title: str
    color: str
    color_hex: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    size: str
    production_cost_cents: int = Field(ge=0)
    enabled: bool = True


class EtsyListingDefaults(StrictModel):
    taxonomy_id: int = Field(gt=0)
    shipping_profile_id: int = Field(gt=0)
    return_policy_id: int = Field(gt=0)
    readiness_state_id: int = Field(gt=0)
    production_partner_ids: Annotated[list[int], Field(min_length=1)]
    quantity: int = Field(default=999, ge=1)


class ChannelConfig(StrictModel):
    channel: Channel
    printify_shop_id: str
    percent_fee: float = Field(ge=0, lt=0.5)
    fixed_fee_cents: int = Field(ge=0)
    enabled: bool = True
    etsy_listing_defaults: EtsyListingDefaults | None = None

    @model_validator(mode="after")
    def etsy_defaults_match_channel(self) -> ChannelConfig:
        if self.etsy_listing_defaults is not None and self.channel != Channel.ETSY:
            raise ValueError("Etsy listing defaults may only be set on the Etsy channel")
        return self


class GarmentFacts(StrictModel):
    brand: str | None = None
    model: str | None = None
    finish: str | None = None
    fit: str | None = None
    source_url: str | None = None
    verified_at: date | None = None

    @field_validator("source_url")
    @classmethod
    def valid_http_url(cls, value: str | None) -> str | None:
        return str(TypeAdapter(HttpUrl).validate_python(value)) if value is not None else None


class ProductTemplate(StrictModel):
    name: str
    blueprint_id: int
    print_provider_id: int
    position: Literal["front"] = "front"
    decoration_method: Literal["dtg"] = "dtg"
    print_width: int = Field(gt=0)
    print_height: int = Field(gt=0)
    variants: Annotated[list[VariantConfig], Field(min_length=1, max_length=100)]
    featured_variant_id: int | None = None
    channels: Annotated[list[ChannelConfig], Field(min_length=1)]
    etsy_production_partner_confirmed: bool = False
    garment_facts: GarmentFacts | None = None
    production_costs_reviewed_at: date | None = None

    @model_validator(mode="after")
    def unique_catalog_configuration(self) -> ProductTemplate:
        variant_ids = [item.variant_id for item in self.variants]
        channels = [item.channel for item in self.channels]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("variant IDs must be unique")
        if len(channels) != len(set(channels)):
            raise ValueError("channel configurations must be unique")
        if self.featured_variant_id is not None and self.featured_variant_id not in {
            item.variant_id for item in self.variants if item.enabled
        }:
            raise ValueError("featured variant must be enabled in the template")
        return self

    def featured_variant(self) -> VariantConfig:
        enabled = [item for item in self.variants if item.enabled]
        if not enabled:
            raise ValueError("at least one enabled variant is required")
        return next(
            (item for item in enabled if item.variant_id == self.featured_variant_id),
            enabled[0],
        )

    def qa_shirt_colors(self) -> list[str]:
        """Return every enabled garment swatch, once per color, for prepress QA."""
        return sorted({item.color_hex or item.color for item in self.variants if item.enabled})


class PriceQuote(StrictModel):
    channel: Channel
    variant_id: int
    production_cost_cents: int
    retail_price_cents: int
    estimated_fee_cents: int
    estimated_margin: float


class ApprovalRequest(StrictModel):
    channels: Annotated[list[Channel], Field(min_length=1)]
    expected_version: int = Field(ge=1)
    ip_attested: bool = False
    confirmation: Literal["PUBLISH"]


class ListingPackageUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    listings: MarketplaceListingSet
    prices: Annotated[list[PriceQuote], Field(min_length=1)]


class RunInput(StrictModel):
    run_id: UUID
    scheduled_for: datetime
    manual: bool = False
    reuse_research: bool = False
    reuse_selection: bool = False
    reuse_brief: bool = False
    pipeline_version: Literal[1, 2] = 1
    max_opportunity_attempts: int = Field(default=3, ge=1, le=10)


class ApprovalSignal(StrictModel):
    channels: list[Channel]
    expected_version: int
    ip_attested: bool
    actor: str = "admin"


class PublishInput(StrictModel):
    run_id: UUID
    channel: Channel


class DailyPerformance(StrictModel):
    metric_date: date
    period_start: date | None = None
    period_end: date | None = None
    channel: Channel
    concept_id: UUID | None = None
    external_product_id: str | None = None
    impressions: int | None = None
    visits: int | None = None
    favorites: int | None = None
    cart_adds: int | None = None
    orders: int | None = None
    units: int | None = None
    gross_revenue_cents: int | None = None
    refunds_cents: int | None = None
    fees_cents: int | None = None
    fulfillment_cost_cents: int | None = None
    profit_cents: int | None = None
    profit_quality: Literal["actual", "estimated", "unknown"] = "unknown"
    source: str
    completeness: dict[str, bool] = Field(default_factory=dict)


class EtsyCsvImportResult(StrictModel):
    filename: str
    sha256: str
    imported_rows: int
    rejected_rows: int


class ConnectorTokenUpdate(StrictModel):
    credential: Literal["etsy_access_token", "etsy_refresh_token", "amazon_refresh_token"]
    value: str = Field(min_length=1)


class RunView(StrictModel):
    id: UUID
    workflow_id: str
    status: RunStatus
    version: int
    scheduled_for: datetime
    selected_concept: dict[str, Any] | None = None
    creative_brief: dict[str, Any] | None = None
    ip_report: dict[str, Any] | None = None
    qa_report: dict[str, Any] | None = None
    listings: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
