from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from merch.config import Settings
from merch.domain.artwork_recovery import ArtworkFailureSummary, RecoveryContext
from merch.prompts import (
    ARTWORK_PROMPT,
    BRIEF_REWRITE_PROMPT,
    CREATIVE_PROMPT,
    PIPELINE_CAPABILITIES,
    PROMPT_VERSION,
    QA_PROMPT,
    REVISION_PROMPT,
    TYPOGRAPHY_PROMPT,
)
from merch.schemas import CandidateConcept, CreativeBrief, QAIssue, QAReport
from merch.services.openai_service import OpenAIService


async def fixture_brief() -> tuple[OpenAIService, CandidateConcept, CreativeBrief]:
    service = OpenAIService(Settings())
    concept = (await service.research(date(2026, 9, 19), "none")).value.candidates[0]
    brief = (await service.creative(concept, {})).value
    return service, concept, brief


@pytest.mark.parametrize(
    "prompt",
    [CREATIVE_PROMPT, ARTWORK_PROMPT, REVISION_PROMPT, QA_PROMPT, BRIEF_REWRITE_PROMPT],
    ids=["creative", "artwork", "revision", "qa", "brief-rewrite"],
)
def test_artwork_stages_share_actual_pipeline_capabilities(prompt: str) -> None:
    assert PIPELINE_CAPABILITIES in prompt
    assert "transparent raster PNG" in prompt
    assert "Application prepress owns" in prompt
    assert "product catalog owns garment variants" in prompt
    assert "Original illustrated people are allowed" in prompt
    assert "Outlines are optional" in prompt
    assert "Do not show a shirt, person, model" not in prompt
    assert "Make one strong centered silhouette" not in prompt
    assert "Give dark colored elements both a light" not in prompt


def test_typography_prompt_declares_deterministic_text_color_and_placement_contract() -> None:
    assert PROMPT_VERSION == "2026-09-21.2"
    assert "literal LF newline" in TYPOGRAPHY_PROMPT
    assert "mandatory hard line boundary" in TYPOGRAPHY_PROMPT
    assert "fully opaque #RRGGBB" in TYPOGRAPHY_PROMPT
    assert "center vertical_placement" in TYPOGRAPHY_PROMPT
    assert "bottom vertical_placement" in TYPOGRAPHY_PROMPT


def test_visual_qa_requires_brief_correction_and_retains_real_failures() -> None:
    assert "BRIEF_CONTRACT" in QA_PROMPT
    assert "Do not turn such a conflict into an illustration defect or waive any real defect" in (
        QA_PROMPT.replace("\n", " ")
    )
    assert "actual anatomy, readability, contrast, and printability failures separately" in QA_PROMPT
    assert "retain\nits error findings" in QA_PROMPT


def test_creative_and_visual_qa_require_distinctive_hierarchy_and_formatting() -> None:
    assert "one unmistakable focal element" in CREATIVE_PROMPT
    assert "concept-specific visual detail" in ARTWORK_PROMPT
    assert "Technical correctness alone is not enough" in QA_PROMPT
    for code in (
        "COMPOSITION_HIERARCHY",
        "TYPOGRAPHY_FORMATTING",
        "BRAND_DISTINCTIVENESS",
        "CONCEPT_DILUTION",
    ):
        assert code in QA_PROMPT
    assert "material recomposition" in REVISION_PROMPT
    assert "plain word stack, generic badge, stock icon" in BRIEF_REWRITE_PROMPT


@pytest.mark.asyncio
async def test_visual_qa_receives_full_deterministic_report_with_color_profile() -> None:
    service, _, brief = await fixture_brief()
    deterministic = QAReport(
        passed=False,
        revision=2,
        width=3692,
        height=4800,
        has_alpha=True,
        color_profile="sRGB",
        issues=[
            QAIssue(
                code="PRINT_DETAIL",
                severity="error",
                message="The smallest stroke is too thin to print.",
                recommended_fix="Thicken the small strokes.",
            )
        ],
    )
    captured: dict[str, Any] = {}

    class Responses:
        async def parse(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                output_parsed=deterministic,
                usage=None,
                id="qa-contract-fixture",
                model=kwargs["model"],
                output=[],
            )

    service.client = SimpleNamespace(responses=Responses())  # type: ignore[assignment]
    effects = {"artwork_distress_level": 2, "text_arc_or_shape": "up"}
    result = await service.visual_qa(b"fixture-image", brief, deterministic, effects=effects)
    prompt = captured["input"][0]["content"][0]["text"]
    report, _ = json.JSONDecoder().raw_decode(prompt.split("Deterministic QA report: ", 1)[1])
    assert report == deterministic.model_dump(mode="json")
    assert json.dumps(effects) in prompt
    assert result.value == deterministic
    assert captured["input"][0]["content"][1]["detail"] == service.settings.openai_visual_qa_detail


@pytest.mark.asyncio
async def test_live_brief_rewrite_receives_and_records_recovery_strategy() -> None:
    service, concept, brief = await fixture_brief()
    captured: dict[str, Any] = {}
    context = RecoveryContext(
        attempt=3,
        strategy="structural_simplification",
        reasons=("Repeated motif-separation failure across two artwork versions",),
        history=(
            ArtworkFailureSummary(
                version=1,
                revision=2,
                issue_codes=("NEGATIVE_SPACE",),
                issue_families=("layout_separation",),
            ),
            ArtworkFailureSummary(
                version=2,
                revision=2,
                issue_codes=("ELEMENT_SEPARATION",),
                issue_families=("layout_separation",),
            ),
        ),
    )

    class Responses:
        async def parse(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                output_parsed=brief,
                usage=None,
                id="rewrite-contract-fixture",
                model=kwargs["model"],
                output=[],
            )

    service.client = SimpleNamespace(responses=Responses())  # type: ignore[assignment]
    result = await service.revise_brief(
        concept, brief, [], brief.shirt_colors, recovery_context=context
    )
    prompt = captured["input"][0]["content"][0]["text"]
    assert json.loads(prompt.split("Recovery context: ", 1)[1]) == context.to_dict()
    assert result.metadata["recovery_context"] == context.to_dict()
    assert "Replace BOTH composition and generation_brief" in prompt
    assert "appending a repair paragraph is unacceptable" in prompt
    assert captured["reasoning"] == {"effort": service.settings.openai_creative_reasoning_effort}


@pytest.mark.asyncio
async def test_fake_rewrites_replace_failed_geometry_and_preserve_identity() -> None:
    service, concept, original = await fixture_brief()
    colors = [f"Catalog color {index}" for index in range(14)]
    brief = original.model_copy(
        update={
            "composition": "Pack five repeated motifs inside a narrow enclosing ring.",
            "generation_brief": "Lock every motif to exact vector coordinates; require physical proof.",
            "typography_style": "steep overlapping arch",
            "artwork_distress_level": 5,
        }
    )
    issues = [
        QAIssue(code="MOTIF_SEPARATION", severity="error", message="Motifs merge into the ring."),
        QAIssue(code="BRIEF_CONTRACT", severity="error", message="Brief demands a vector proof."),
        QAIssue(code="TYPOGRAPHY_LAYOUT", severity="error", message="Arched letters overlap."),
        QAIssue(code="DISTRESS_PRINTABILITY", severity="error", message="Distress damages text."),
    ]
    targeted = await service.revise_brief(concept, brief, issues, colors)
    structural = await service.revise_brief(
        concept,
        brief,
        issues,
        colors,
        recovery_context=RecoveryContext(attempt=3, strategy="structural_simplification"),
    )
    for result in (targeted, structural):
        revised = result.value
        for field in (
            "concept_name", "target_customer", "customer_motivation", "slogan", "design_mode"
        ):
            assert getattr(revised, field) == getattr(brief, field)
        assert revised.shirt_colors == colors
        assert revised.composition != brief.composition
        assert brief.composition not in revised.composition
        assert brief.generation_brief not in revised.generation_brief
        assert "vector coordinates" not in revised.generation_brief
        assert "physical proof" not in revised.generation_brief
        assert revised.artwork_distress_level == 0
        assert revised.typography_style == "bold readable straight lettering with generous spacing"
    assert targeted.value.composition != structural.value.composition
    assert targeted.value.generation_brief != structural.value.generation_brief
    assert "Remove enclosing frames and rings" in structural.value.composition
    assert "optional repeats" in structural.value.generation_brief
    assert targeted.metadata["recovery_context"]["strategy"] == "targeted"
    assert structural.metadata["recovery_context"]["attempt"] == 3
