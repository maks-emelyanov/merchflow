from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from merch.config import Settings
from merch.domain.concept_ranking import (
    concept_score_breakdown,
    selection_candidates,
    weighted_concept_score,
)
from merch.schemas import (
    CandidateConcept,
    Evidence,
    NewResearchReport,
    ResearchReport,
    SelectionDecision,
)
from merch.services.openai_service import OpenAIService


@pytest.fixture
async def concept() -> CandidateConcept:
    return (await OpenAIService(Settings()).research(date(2026, 9, 20), "none")).value.candidates[0]


def supporting_evidence(kind: str, supports: list[str], excerpt: str = "Source observation") -> Evidence:
    return Evidence.model_validate({
        "claim": "A scoped research signal", "title": "Source", "url": "https://example.com/signal",
        "kind": kind, "supports": supports, "excerpt": excerpt,
    })


def scored(concept: CandidateConcept, name: str, score: int) -> CandidateConcept:
    assert concept.strategy is not None
    return concept.model_copy(update={
        "concept_name": name,
        "scores": concept.scores.model_copy(update={
            "demand": score, "trend_velocity": score, "novelty": score,
            "purchase_intent": score, "printability": score, "competition": 100 - score,
            "longevity": score, "ip_risk": 0,
        }),
        "strategy": concept.strategy.model_copy(update={"brand_fit": score, "shareability": score}),
        "evidence": [supporting_evidence("observed_metric", ["demand", "competition", "trend_velocity"])],
    })


def live_service(value: BaseModel, captured: dict[str, Any]) -> OpenAIService:
    class Responses:
        async def parse(self, **kwargs: Any) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(
                output_parsed=value, usage=None, id="fixture-live", model=kwargs["model"], output=[],
            )

    service = OpenAIService(Settings())
    service.client = SimpleNamespace(responses=Responses())  # type: ignore[assignment]
    return service


@pytest.mark.parametrize(
    ("kind", "confidence", "effective"),
    [("observed_metric", 1.0, 90), ("marketplace_proxy", 0.5, 70),
     ("editorial", 0.25, 60), ("inference", 0.0, 50), ("unknown", 0.0, 50)],
)
def test_market_evidence_adjusts_only_supported_dimension(
    concept: CandidateConcept, kind: str, confidence: float, effective: float,
) -> None:
    candidate = scored(concept, "Signal", 90).model_copy(update={
        "evidence": [supporting_evidence(kind, ["demand"])],
    })
    components = concept_score_breakdown(candidate)["components"]
    assert components["demand"]["confidence"] == confidence
    assert components["demand"]["effective"] == effective
    assert components["low_competition"]["effective"] == 50
    assert components["trend_velocity"]["effective"] == 50
    assert components["novelty"]["effective"] == 90


def test_excerpt_is_required_and_strongest_source_is_used(concept: CandidateConcept) -> None:
    candidate = scored(concept, "Signal", 90).model_copy(update={
        "evidence": [
            supporting_evidence("observed_metric", ["competition"], " "),
            supporting_evidence("editorial", ["competition"]),
            supporting_evidence("marketplace_proxy", ["competition"]),
        ],
    })
    competition = concept_score_breakdown(candidate)["components"]["low_competition"]
    assert competition["confidence"] == 0.5
    assert competition["effective"] == 70


def test_weights_brand_preference_and_ip_penalty(concept: CandidateConcept) -> None:
    candidate = scored(concept, "Strong outside-lane idea", 90)
    assert candidate.strategy is not None
    candidate = candidate.model_copy(update={
        "strategy": candidate.strategy.model_copy(update={"brand_fit": 0}),
    })
    assert weighted_concept_score(candidate) == 81
    assert weighted_concept_score(candidate) > weighted_concept_score(scored(concept, "Weaker on-brand", 75))
    risky = candidate.model_copy(update={"scores": candidate.scores.model_copy(update={"ip_risk": 80})})
    assert weighted_concept_score(risky) == 31
    assert weighted_concept_score(risky, False) == 81


def test_legacy_score_and_selection_remain_unchanged(concept: CandidateConcept) -> None:
    legacy = concept.model_copy(update={"strategy": None, "evidence": []})
    scores = legacy.scores
    expected = round(
        .25 * scores.demand + .20 * scores.trend_velocity + .15 * scores.purchase_intent
        + .15 * scores.novelty + .10 * (100 - scores.competition)
        + .10 * scores.printability + .05 * scores.longevity, 2,
    )
    assert weighted_concept_score(legacy) == expected
    assert concept_score_breakdown(legacy)["version"] == "legacy"
    legacy_pool = [legacy.model_copy(update={"concept_name": str(index)}) for index in range(8)]
    assert selection_candidates(legacy_pool) == legacy_pool


def test_shortlist_is_three_within_five_points_with_stable_ties(concept: CandidateConcept) -> None:
    pool = [
        scored(concept, "Far", 74), scored(concept, "Third", 75),
        scored(concept, "Leader", 80), scored(concept, "Second", 78), scored(concept, "Z fourth", 75),
    ]
    assert [item.concept_name for item in selection_candidates(pool)] == ["Leader", "Second", "Third"]
    assert [item.concept_name for item in selection_candidates([pool[0], pool[2]])] == ["Leader"]


@pytest.mark.asyncio
@pytest.mark.parametrize("chosen", ["Second", "Far", "Invented"])
async def test_live_selection_uses_shortlist_and_canonical_score(
    concept: CandidateConcept, chosen: str,
) -> None:
    pool = [scored(concept, "Leader", 80), scored(concept, "Second", 78), scored(concept, "Far", 50)]
    captured: dict[str, Any] = {}
    service = live_service(SelectionDecision(
        selected_concept_name=chosen, rationale="Memorable premise", weighted_score=99,
        rejected_concepts=[],
    ), captured)
    result = await service.select(pool)
    expected = pool[1] if chosen == "Second" else pool[0]
    assert result.value.selected_concept_name == expected.concept_name
    assert result.value.weighted_score == weighted_concept_score(expected, False)
    assert len(result.value.rejected_concepts) == 2
    assert result.metadata["selection_fallback"] is (chosen != "Second")
    prompt = captured["input"][0]["content"][0]["text"]
    assert '"concept_name": "Far"' not in prompt
    assert '"canonical_ranking"' in prompt


@pytest.mark.asyncio
async def test_new_research_has_varied_strategies_and_receives_context() -> None:
    result = await OpenAIService(Settings()).research(
        date(2026, 9, 20), "No performance observations",
        product_context={"name": "Approved garment", "colors": ["Pepper"]},
        recent_concepts=[{"concept_name": "Last night", "premise": "Do not repeat me"}],
    )
    report = NewResearchReport.model_validate(result.value.model_dump())
    assert len(report.candidates) == 25
    assert len({item.strategy.micro_niche for item in report.candidates if item.strategy}) == 25
    assert all(item.evidence[0].kind == "inference" for item in report.candidates)
    assert "Approved garment" in result.metadata["prompt"]
    assert "Do not repeat me" in result.metadata["prompt"]
    assert "No performance observations" in result.metadata["prompt"]
    captured: dict[str, Any] = {}
    live = live_service(report, captured)
    await live.research(date(2026, 9, 20), "none")
    assert captured["text_format"] is NewResearchReport
    assert captured["tools"] == [{"type": "web_search"}]


@pytest.mark.asyncio
async def test_live_new_research_does_not_accept_legacy_provider_output() -> None:
    report = (await OpenAIService(Settings()).research(date(2026, 9, 20), "none")).value
    legacy = ResearchReport.model_validate({**report.model_dump(), "candidates": report.candidates[:10]})
    with pytest.raises(ValidationError):
        await live_service(legacy, {}).research(date(2026, 9, 20), "none")


@pytest.mark.asyncio
async def test_service_preserves_strategy_and_exact_slogan_through_creative_and_recovery(
    concept: CandidateConcept,
) -> None:
    original = (await OpenAIService(Settings()).creative(concept, {})).value
    changed = original.model_copy(update={"strategy": None, "slogan": "Invented wording"})
    service = live_service(changed, {})
    creative = await service.creative(concept, {})
    revised = await service.revise_brief(concept, original, [], [])
    for result in (creative, revised):
        assert result.value.strategy == concept.strategy
        assert result.value.slogan == concept.slogan_if_any
    listing = await OpenAIService(Settings()).listings({}, original, "none")
    assert concept.strategy is not None
    for angle in (concept.strategy.etsy_angle, concept.strategy.amazon_angle, concept.strategy.shopify_angle):
        assert angle in listing.metadata["prompt"]
