"""Catalog matching, evidence gates, and canonical opportunity ranking."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import cycle

from merch.schemas import (
    CatalogProduct,
    CatalogVariant,
    CompetitorListingSnapshot,
    OpportunityScores,
    ProductOpportunity,
)

WEIGHTS = {
    "demand": 0.25,
    "evidence_confidence": 0.15,
    "purchase_intent": 0.15,
    "projected_margin": 0.15,
    "undercut_feasibility": 0.10,
    "competition_gap": 0.10,
    "trend_velocity": 0.05,
    "longevity": 0.05,
}
_STOP_WORDS = {
    "and",
    "for",
    "the",
    "with",
    "from",
    "this",
    "that",
    "gift",
    "custom",
    "personalized",
    "print",
    "printed",
    "design",
    "product",
    "fixture",
    "popular",
}


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) > 2 and token not in _STOP_WORDS
    }


def product_match_score(
    product: CatalogProduct, listings: list[CompetitorListingSnapshot]
) -> float:
    product_tokens = _tokens(" ".join([product.title, *product.tags]))
    evidence_tokens = _tokens(
        " ".join([item.product_type for item in listings] + [item.title for item in listings])
    )
    if not product_tokens or not evidence_tokens:
        return 0.0
    overlap = len(product_tokens & evidence_tokens)
    return overlap / len(product_tokens)


def evidence_is_fresh(
    listing: CompetitorListingSnapshot,
    *,
    now: datetime,
    direct_hours: int,
    fallback_hours: int,
) -> bool:
    if listing.collected_at.tzinfo is None:
        return False
    age = now - listing.collected_at.astimezone(UTC)
    limit = direct_hours if listing.source_method == "browser" else fallback_hours
    return timedelta(0) <= age <= timedelta(hours=limit)


def evidence_gate(listings: list[CompetitorListingSnapshot]) -> tuple[bool, str | None]:
    if len(listings) < 3:
        return False, "fewer than three specific comparable listings"
    if len({item.marketplace for item in listings}) < 2:
        return False, "evidence does not span two marketplaces"
    explicit = sum(signal.explicit for item in listings for signal in item.sales_signals)
    proxy_listings = {
        (item.marketplace, item.external_listing_id)
        for item in listings
        if any(not signal.explicit for signal in item.sales_signals)
    }
    if explicit < 1 and len(proxy_listings) < 2:
        return False, "insufficient explicit or corroborating proxy sales signals"
    return True, None


def _keyword_phrases(
    listings: list[CompetitorListingSnapshot], product: CatalogProduct
) -> list[str]:
    counter: Counter[str] = Counter()
    for listing in listings:
        counter.update(_tokens(listing.title))
    product_tokens = _tokens(product.title)
    ordered = [term for term, _ in counter.most_common() if term not in product_tokens]
    return ordered[:8] or sorted(product_tokens)[:8]


def _strongest_comparables(
    listings: list[CompetitorListingSnapshot], *, limit: int = 10
) -> list[CompetitorListingSnapshot]:
    """Put explicit demand evidence first so reference art uses the best-supported listings."""
    return sorted(
        listings,
        key=lambda item: (
            -sum(signal.explicit for signal in item.sales_signals),
            -item.confidence,
            -(item.review_count or 0),
            item.marketplace.value,
            item.external_listing_id,
        ),
    )[:limit]


def _scores(
    listings: list[CompetitorListingSnapshot], product: CatalogProduct
) -> OpportunityScores:
    explicit = sum(signal.explicit for item in listings for signal in item.sales_signals)
    proxy = sum(not signal.explicit for item in listings for signal in item.sales_signals)
    delivered = sum(item.delivered_price_cents is not None for item in listings)
    delivered_prices = sorted(
        item.delivered_price_cents for item in listings if item.delivered_price_cents is not None
    )
    benchmark = delivered_prices[len(delivered_prices) // 2] if len(delivered_prices) >= 3 else None
    costs = [
        (variant.production_cost_cents or 0) + (variant.shipping_cost_cents or 0)
        for variant in product.variants
        if variant.available
        and variant.production_cost_cents is not None
        and variant.shipping_cost_cents is not None
    ]
    if costs and benchmark:
        projected = (benchmark - min(costs) - round(benchmark * 0.095) - 45) / benchmark
        projected_margin = max(0, min(100, round(projected * 100)))
        undercut_feasibility = 90 if projected >= 0.40 else max(0, round(projected * 100))
    else:
        projected_margin = 0
        undercut_feasibility = 0
    demand = min(100, 55 + explicit * 15 + proxy * 4)
    confidence = round(sum(item.confidence for item in listings) / len(listings))
    return OpportunityScores(
        demand=demand,
        evidence_confidence=confidence,
        purchase_intent=min(100, 60 + len(listings) * 4),
        projected_margin=projected_margin,
        undercut_feasibility=min(100, undercut_feasibility + delivered),
        competition_gap=max(25, 75 - len(listings) * 3),
        trend_velocity=min(100, 50 + explicit * 10 + proxy * 3),
        longevity=70,
    )


def weighted_opportunity_score(scores: OpportunityScores) -> float:
    return round(float(sum(getattr(scores, name) * weight for name, weight in WEIGHTS.items())), 4)


def build_opportunities(
    catalog: list[CatalogProduct],
    evidence_by_product: dict[tuple[int, int], list[CompetitorListingSnapshot]],
    *,
    count: int = 25,
) -> list[ProductOpportunity]:
    seeds: list[tuple[CatalogProduct, list[CompetitorListingSnapshot]]] = []
    for product in catalog:
        listings = evidence_by_product.get((product.blueprint_id, product.print_provider_id), [])
        gated, _ = evidence_gate(listings)
        if gated and product_match_score(product, listings) >= 0.20:
            seeds.append((product, _strongest_comparables(listings)))
    if not seeds:
        return []
    opportunities: list[ProductOpportunity] = []
    for index, (product, listings) in enumerate(cycle(seeds), start=1):
        if len(opportunities) >= count:
            break
        phrases = _keyword_phrases(listings, product)
        scores = _scores(listings, product)
        identity = (
            f"{product.blueprint_id}:{product.print_provider_id}:{index}:"
            f"{','.join(item.external_listing_id for item in listings)}"
        )
        opportunity_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        angle = phrases[(index - 1) % len(phrases)] if phrases else product.title
        opportunities.append(
            ProductOpportunity(
                opportunity_id=opportunity_id,
                concept_name=f"{angle.title()} {product.title} direction {index}",
                target_customer=f"US shoppers searching for {angle} {product.title}",
                product_type=product.title,
                visual_direction=(
                    f"Use the reference set's high-level hierarchy and product placement for a "
                    f"{product.title}, while replacing all wording, motifs, and distinctive execution "
                    f"with an original trend-led {angle} concept."
                ),
                keyword_phrases=phrases,
                comparable_listings=listings,
                matched_blueprint_id=product.blueprint_id,
                matched_print_provider_id=product.print_provider_id,
                match_rationale=(
                    "Catalog title/function tokens match the researched product type and the "
                    "selected variants expose supported printable surfaces."
                ),
                scores=scores,
                weighted_score=weighted_opportunity_score(scores),
            )
        )
    return sorted(
        opportunities,
        key=lambda item: (
            -item.weighted_score,
            -item.scores.evidence_confidence,
            -item.scores.projected_margin,
            item.opportunity_id,
        ),
    )


def reduce_variation_axes(
    variants: list[CatalogVariant],
    *,
    max_axes: int,
    maximum_products: int,
) -> list[CatalogVariant]:
    """Reduce least-variable axes, then choose a deterministic profitable subset."""
    available = [item for item in variants if item.available]
    axes = sorted({key for item in available for key in item.options})
    if len(axes) > max_axes:
        value_counts = {
            axis: Counter(item.options.get(axis) for item in available if axis in item.options)
            for axis in axes
        }
        keep_axes = set(
            sorted(
                axes,
                key=lambda axis: (-len(value_counts[axis]), axis),
            )[:max_axes]
        )
        collapsed_axes = [axis for axis in axes if axis not in keep_axes]
        preferred = {
            axis: value_counts[axis].most_common(1)[0][0]
            for axis in collapsed_axes
            if value_counts[axis]
        }
        available = [
            item
            for item in available
            if all(item.options.get(axis) == value for axis, value in preferred.items())
        ]
    ranked = sorted(
        available,
        key=lambda item: (
            item.production_cost_cents is None,
            item.production_cost_cents or 0,
            item.variant_id,
        ),
    )
    return ranked[:maximum_products]
