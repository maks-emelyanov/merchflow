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
    kind: Literal[
        "observed_metric", "marketplace_proxy", "editorial", "inference", "unknown"
    ] = "unknown"
    supports: list[Literal["demand", "competition", "trend_velocity"]] = Field(
        default_factory=list
    )
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
    listings: Annotated[list[MarketplaceListing], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def one_listing_per_channel(self) -> MarketplaceListingSet:
        if {item.channel for item in self.listings} != set(Channel):
            raise ValueError("listing set must contain Shopify, Etsy, and Amazon US exactly once")
        return self


class CopyRefreshEdit(StrictModel):
    expected_version: int = Field(ge=1)
    title: str
    long_description: str
    tags: list[str]


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
