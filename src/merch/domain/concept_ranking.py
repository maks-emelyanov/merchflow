"""Auditable commercial scores and a bounded shortlist for concept selection."""

from __future__ import annotations

from typing import Any

from merch.schemas import CandidateConcept

LEGACY_WEIGHTS = {
    "demand": 0.25,
    "trend_velocity": 0.20,
    "purchase_intent": 0.15,
    "novelty": 0.15,
    "low_competition": 0.10,
    "printability": 0.10,
    "longevity": 0.05,
}
STRATEGY_WEIGHTS = {
    "demand": 0.20,
    "purchase_intent": 0.20,
    "novelty": 0.10,
    "low_competition": 0.10,
    "printability": 0.10,
    "brand_fit": 0.10,
    "shareability": 0.10,
    "trend_velocity": 0.05,
    "longevity": 0.05,
}
EVIDENCE_CONFIDENCE = {
    "observed_metric": 1.0,
    "marketplace_proxy": 0.5,
    "editorial": 0.25,
    "inference": 0.0,
    "unknown": 0.0,
}


def concept_score_breakdown(
    concept: CandidateConcept, include_ip_risk: bool = True,
) -> dict[str, Any]:
    """Return score inputs and adjustments without presenting estimates as facts.

    Evidence classification remains a research judgment. An excerpt is required
    before a classified source can influence the confidence of a market signal.
    Historical concepts without strategy metadata retain their original formula.
    """
    strategy = concept.strategy
    weights = STRATEGY_WEIGHTS if strategy is not None else LEGACY_WEIGHTS
    raw = concept.scores.model_dump()
    raw["low_competition"] = 100 - concept.scores.competition
    if strategy is not None:
        raw.update(brand_fit=strategy.brand_fit, shareability=strategy.shareability)
    components: dict[str, dict[str, float | int]] = {}
    for name, weight in weights.items():
        confidence = 1.0
        signal = "competition" if name == "low_competition" else name
        if strategy is not None and signal in {"demand", "competition", "trend_velocity"}:
            confidence = max(
                (
                    EVIDENCE_CONFIDENCE[evidence.kind]
                    for evidence in concept.evidence
                    if signal in evidence.supports and (evidence.excerpt or "").strip()
                ),
                default=0.0,
            )
        effective = 50 + confidence * (raw[name] - 50)
        components[name] = {
            "raw": raw[name],
            "confidence": confidence,
            "effective": effective,
            "weight": weight,
            "contribution": weight * effective,
        }
    penalty = min(50, concept.scores.ip_risk * 1.5) if include_ip_risk else 0.0
    score = round(max(0.0, sum(item["contribution"] for item in components.values()) - penalty), 2)
    return {
        "version": "strategy-v1" if strategy is not None else "legacy",
        "score": score,
        "components": components,
        "ip_penalty": penalty,
    }


def weighted_concept_score(concept: CandidateConcept, include_ip_risk: bool = True) -> float:
    return float(concept_score_breakdown(concept, include_ip_risk)["score"])


def selection_candidates(
    concepts: list[CandidateConcept], include_ip_risk: bool = True,
) -> list[CandidateConcept]:
    """Shortlist up to three concepts within five points; retain legacy selection."""
    if not concepts or not any(item.strategy is not None for item in concepts):
        return list(concepts)
    ranked = sorted(
        concepts,
        key=lambda item: (-weighted_concept_score(item, include_ip_risk), item.concept_name.casefold()),
    )
    cutoff = weighted_concept_score(ranked[0], include_ip_risk) - 5
    return [item for item in ranked[:3] if weighted_concept_score(item, include_ip_risk) >= cutoff]
