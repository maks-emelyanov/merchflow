"""Evidence-backed Etsy keyword selection without competitor identity leakage."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from difflib import SequenceMatcher

from merch.schemas import (
    MarketplaceListing,
    MarketplaceSource,
    ProductOpportunity,
    SEOEvidence,
    SEOKeywordEvidence,
)

_STOP = {
    "and",
    "the",
    "for",
    "with",
    "from",
    "your",
    "this",
    "that",
    "best",
    "seller",
    "bestseller",
    "amazon",
    "etsy",
    "tiktok",
    "walmart",
    "ebay",
}
_UNSUPPORTED_CLAIMS = {
    "organic",
    "eco friendly",
    "sustainable",
    "non toxic",
    "dishwasher safe",
    "microwave safe",
    "waterproof",
    "medical grade",
    "guaranteed delivery",
}


def _words(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) > 2 and token not in _STOP
    ]


def build_seo_evidence(opportunity: ProductOpportunity) -> SEOEvidence:
    sellers = {
        " ".join(_words(item.seller or ""))
        for item in opportunity.comparable_listings
        if _words(item.seller or "")
    }
    prohibited = sorted(sellers)
    occurrences: Counter[str] = Counter()
    markets: dict[str, set[MarketplaceSource]] = defaultdict(set)
    listings: dict[str, set[str]] = defaultdict(set)
    for snapshot in opportunity.comparable_listings:
        words = _words(snapshot.title)
        phrases = set(words)
        phrases.update(" ".join(words[index : index + 2]) for index in range(len(words) - 1))
        source_weight = 3 if snapshot.marketplace == MarketplaceSource.ETSY else 1
        for phrase in phrases:
            occurrences[phrase] += source_weight
            markets[phrase].add(snapshot.marketplace)
            listings[phrase].add(snapshot.external_listing_id)
    for phrase in opportunity.keyword_phrases:
        normalized = " ".join(_words(phrase))
        if normalized:
            occurrences[normalized] += 1
    evidence: list[SEOKeywordEvidence] = []
    for phrase, count in sorted(
        occurrences.items(), key=lambda item: (-item[1], len(item[0]), item[0])
    ):
        excluded = next((term for term in prohibited if term and term in phrase), None)
        included = (
            excluded is None
            and len(phrase) <= 20
            and (count >= 2 or phrase in opportunity.keyword_phrases)
        )
        evidence.append(
            SEOKeywordEvidence(
                phrase=phrase,
                marketplaces=sorted(
                    markets.get(phrase)
                    or {item.marketplace for item in opportunity.comparable_listings},
                    key=lambda item: item.value,
                ),
                listing_ids=sorted(
                    listings.get(phrase)
                    or {item.external_listing_id for item in opportunity.comparable_listings}
                ),
                included=included,
                exclusion_reason=(
                    f"contains competitor identity: {excluded}"
                    if excluded
                    else "phrase exceeds Etsy's tag limit"
                    if len(phrase) > 20
                    else "not corroborated across the evidence set"
                    if not included
                    else None
                ),
            )
        )
    return SEOEvidence(
        keywords=evidence[:100],
        prohibited_terms=prohibited,
        generated_at=datetime.now(UTC),
    )


def validate_seo_listing(listing: MarketplaceListing, evidence: SEOEvidence) -> None:
    if listing.channel.value != "etsy":
        raise ValueError("catalog listing must target Etsy")
    if not 1 <= len(listing.title) <= 140:
        raise ValueError("Etsy title must contain 1-140 characters")
    if len(listing.tags) > 13 or len(set(tag.casefold() for tag in listing.tags)) != len(
        listing.tags
    ):
        raise ValueError("Etsy listing must contain at most 13 unique tags")
    if any(not tag.strip() or len(tag) > 20 for tag in listing.tags):
        raise ValueError("Etsy tags must contain 1-20 characters")
    searchable = " ".join(
        [
            listing.title,
            listing.short_description,
            listing.long_description,
            *listing.tags,
        ]
    ).casefold()
    for term in evidence.prohibited_terms:
        if term and re.search(rf"\b{re.escape(term)}\b", searchable):
            raise ValueError(f"listing contains prohibited competitor term: {term}")
    normalized = " ".join(_words(searchable))
    unsupported = next((claim for claim in _UNSUPPORTED_CLAIMS if claim in normalized), None)
    if unsupported:
        raise ValueError(f"listing contains unsupported product claim: {unsupported}")
    description = listing.long_description.casefold()
    if "ai" not in description or "printify" not in description:
        raise ValueError("Etsy description must disclose AI assistance and Printify production")
    allowed = {item.phrase.casefold() for item in evidence.keywords if item.included}
    if allowed and any(tag.casefold() not in allowed for tag in listing.tags):
        raise ValueError("listing tags must be backed by included SEO evidence")


def copied_listing_wording(
    listing: MarketplaceListing, opportunity: ProductOpportunity
) -> str | None:
    """Return a reference ID if generated listing copy closely reproduces its title."""
    proposed = " ".join(_words(listing.title))
    for reference in opportunity.comparable_listings:
        wording = " ".join(_words(reference.title))
        if len(wording.split()) >= 4 and SequenceMatcher(None, proposed, wording).ratio() >= 0.80:
            return reference.external_listing_id
    return None
