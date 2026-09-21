from __future__ import annotations

import json

import pytest

from merch.domain.artwork_recovery import (
    ArtworkFailure,
    RecoveryContext,
    build_recovery_context,
    build_safe_layout_fallback,
    normalize_issue_family,
    structural_rewrite_is_material,
    typography_fallback_required,
    unsupported_brief_requirement_codes,
)
from merch.schemas import ConceptStrategy, CreativeBrief, DesignMode, QAIssue


def _issue(code: str, *, warning: bool = False) -> QAIssue:
    return QAIssue(code=code, severity="warning" if warning else "error", message="Visible finding")


def _brief() -> CreativeBrief:
    return CreativeBrief(
        concept_name="Evening outings", target_customer="Runners",
        customer_motivation="Celebrating outdoor exercise", slogan=None,
        design_mode="illustration", visual_concept="Runners enjoying an evening together",
        composition="Five runners enclosed in a circular badge.",
        graphic_style="Flat illustration", palette=["#E38D45"], shirt_colors=["Black"],
        typography_style=None,
        generation_brief="Arrange five runners inside a circular border, with a sunset behind them.",
    )


@pytest.mark.parametrize("code", [
    "NEGATIVE_SPACE", "element_separation", " INTERIOR_COMPOSITION ", "Overlap",
    "layout-separation", "element separation",
])
def test_known_layout_aliases_share_a_family(code: str) -> None:
    assert normalize_issue_family(code) == "layout_separation"


@pytest.mark.parametrize("code", [
    "TYPOGRAPHY_LAYOUT", "TYPOGRAPHY_READABILITY", "DISTRESS_PRINTABILITY", "FINE_DETAIL",
    "GARMENT_CONTRAST", "STRAY_PIXELS", "UNKNOWN_DEFECT",
])
def test_real_and_effect_defects_keep_their_own_family(code: str) -> None:
    assert normalize_issue_family(code) == code.casefold()


def test_brief_contract_findings_trigger_brief_correction_without_waiving_errors() -> None:
    codes = {
        "BRIEF_CONTRACT", "PRODUCTION_VERIFICATION", "PRODUCT_VARIANT_MISMATCH",
        "UNSUPPORTED_BRIEF_REQUIREMENT", "UNVERIFIABLE_BRIEF_REQUIREMENT",
    }
    issues = [_issue(code) for code in codes]
    issues.extend([_issue("GARMENT_CONTRAST"), _issue("FINE_DETAIL")])
    assert unsupported_brief_requirement_codes(issues) == codes
    assert {normalize_issue_family(code) for code in codes} == {"brief_contract"}
    assert all(issue.severity == "error" for issue in issues)
    assert unsupported_brief_requirement_codes([_issue("BRIEF_CONTRACT", warning=True)]) == set()


def test_revisions_of_one_brief_do_not_count_as_distinct_failing_versions() -> None:
    context = build_recovery_context([
        ArtworkFailure(1, 2, (_issue("element_separation"),)),
        ArtworkFailure(1, 1, (_issue("NEGATIVE_SPACE"),)),
        ArtworkFailure(1, 2, (_issue("element_separation"),)),
    ], attempt=2)
    assert context.strategy == "targeted"
    assert context.to_dict()["history"] == [{
        "version": 1, "revision": 2,
        "issue_codes": ["ELEMENT_SEPARATION", "NEGATIVE_SPACE"],
        "issue_families": ["layout_separation"],
    }]


def test_repeated_layout_family_across_briefs_requires_structural_simplification() -> None:
    context = build_recovery_context([
        ArtworkFailure(2, 1, (_issue("INTERIOR_COMPOSITION"), _issue("FINE_DETAIL"))),
        ArtworkFailure(1, 2, (_issue("NEGATIVE_SPACE"),)),
    ], attempt=2)
    assert context.strategy == "structural_simplification"
    assert [item.version for item in context.history] == [1, 2]
    assert context.history[1].issue_families == ("fine_detail", "layout_separation")
    assert "versions 1, 2" in context.reasons[0]
    assert json.loads(json.dumps(context.to_dict())) == context.to_dict()


def test_warnings_do_not_trigger_repeat_escalation_or_enter_error_history() -> None:
    context = build_recovery_context([
        ArtworkFailure(1, 1, (_issue("NEGATIVE_SPACE", warning=True),)),
        ArtworkFailure(2, 2, (_issue("OVERLAP"),)),
    ], attempt=2)
    assert context.strategy == "targeted"
    assert [item.version for item in context.history] == [2]


def test_contract_failure_targets_brief_without_premature_structural_escalation() -> None:
    context = build_recovery_context([
        ArtworkFailure(1, 1, (_issue("PRODUCTION_VERIFICATION"),)),
    ], attempt=1)
    assert context.strategy == "targeted"
    assert context.history[0].issue_families == ("brief_contract",)
    assert "unsupported brief requirements" in context.reasons[0]


def test_third_rewrite_escalates_even_when_visual_error_codes_keep_changing() -> None:
    context = build_recovery_context([
        ArtworkFailure(1, 1, (_issue("FINE_DETAIL"),)),
        ArtworkFailure(2, 1, (_issue("UNREADABLE_SUBJECT"),)),
    ], attempt=3)
    assert context.strategy == "structural_simplification"


def test_structural_strategy_stays_selected_despite_different_current_defect() -> None:
    context = build_recovery_context([
        ArtworkFailure(4, 1, (_issue("FINE_DETAIL"),)),
    ], attempt=2, previous_strategy="structural_simplification")
    assert context.strategy == "structural_simplification"
    assert any("already selected" in reason for reason in context.reasons)


def test_context_allows_small_explicit_service_fixtures() -> None:
    assert RecoveryContext(attempt=1, strategy="targeted").to_dict() == {
        "attempt": 1, "strategy": "targeted", "reasons": [], "history": [],
    }


def test_rewrite_number_is_one_based() -> None:
    with pytest.raises(ValueError, match="positive"):
        build_recovery_context([], attempt=0)


@pytest.mark.parametrize("field", ["composition", "generation_brief"])
@pytest.mark.parametrize("change", ["unchanged", "append", "prepend", "formatting", "empty"])
def test_structural_rewrite_rejects_retained_old_instructions(field: str, change: str) -> None:
    previous = _brief()
    old = getattr(previous, field)
    changes = {
        "unchanged": old,
        "append": old + " Make more space between the shapes.",
        "prepend": "Please simplify this layout. " + old,
        "formatting": "\n" + old.upper().replace(" ", "  ").replace(".", "!") + "\t",
        "empty": " ",
    }
    revised = previous.model_copy(update={
        "composition": "Detached broad runner motifs in an open arrangement.",
        "generation_brief": "Draw a few bold running silhouettes with generous clear gaps.",
        field: changes[change],
    })
    assert not structural_rewrite_is_material(previous, revised)


def test_structural_rewrite_accepts_replaced_layout_and_generation_instructions() -> None:
    previous = _brief()
    revised = previous.model_copy(update={
        "composition": "Detached broad runner motifs in an open arrangement.",
        "generation_brief": "Draw a few bold running silhouettes with generous clear gaps.",
    })
    assert structural_rewrite_is_material(previous, revised)


def test_structural_rewrite_can_remove_constraints_by_deleting_old_instructions() -> None:
    previous = _brief().model_copy(update={
        "composition": "Bold runners. Enclose everything in a circular badge.",
        "generation_brief": "Draw bold running silhouettes. Add a detailed sunset behind them.",
    })
    revised = previous.model_copy(update={
        "composition": "Bold runners.", "generation_brief": "Draw bold running silhouettes.",
    })
    assert structural_rewrite_is_material(previous, revised)


def test_repeated_typography_failures_select_provider_free_fallback() -> None:
    brief = _brief().model_copy(update={
        "slogan": "KILN WEATHER BUREAU\nHEAT ADVISORY IN EFFECT",
        "design_mode": DesignMode.HYBRID,
    })
    failures = [
        ArtworkFailure(1, 1, (_issue("TYPOGRAPHY_LAYOUT"),)),
        ArtworkFailure(2, 1, (_issue("TYPOGRAPHY_READABILITY"),)),
    ]
    assert typography_fallback_required(
        failures, brief=brief, budget_exhausted=False
    )
    assert not typography_fallback_required(
        failures[:1], brief=brief, budget_exhausted=False
    )


def test_typography_formatting_counts_as_a_current_fallback_failure() -> None:
    brief = _brief().model_copy(update={
        "slogan": "THE EXACT WORDS",
        "design_mode": DesignMode.HYBRID,
    })
    failures = [
        ArtworkFailure(1, 1, (_issue("TYPOGRAPHY_LAYOUT"),)),
        ArtworkFailure(2, 1, (_issue("TYPOGRAPHY_FORMATTING"),)),
    ]

    assert typography_fallback_required(
        failures, brief=brief, budget_exhausted=False
    )


def test_stale_typography_failures_do_not_override_latest_unrelated_failure() -> None:
    brief = _brief().model_copy(update={
        "slogan": "THE EXACT WORDS",
        "design_mode": DesignMode.HYBRID,
    })
    failures = [
        ArtworkFailure(1, 1, (_issue("TYPOGRAPHY_LAYOUT"),)),
        ArtworkFailure(2, 1, (_issue("TYPOGRAPHY_READABILITY"),)),
        ArtworkFailure(3, 1, (_issue("ANATOMY"),)),
    ]

    assert not typography_fallback_required(
        failures, brief=brief, budget_exhausted=True
    )


def test_budget_exhaustion_uses_fallback_only_for_current_typography_failure() -> None:
    slogan_brief = _brief().model_copy(update={
        "slogan": "THE EXACT WORDS",
        "design_mode": DesignMode.HYBRID,
    })
    unrelated = [ArtworkFailure(8, 1, (_issue("ANATOMY"),))]
    typography = [ArtworkFailure(8, 1, (_issue("TYPOGRAPHY_LAYOUT"),))]
    assert not typography_fallback_required(
        unrelated, brief=slogan_brief, budget_exhausted=True
    )
    assert typography_fallback_required(
        typography, brief=slogan_brief, budget_exhausted=True
    )
    assert not typography_fallback_required(
        [*typography, ArtworkFailure(9, 1, (_issue("ANATOMY"),))],
        brief=slogan_brief,
        budget_exhausted=True,
    )
    assert not typography_fallback_required(
        typography, brief=_brief(), budget_exhausted=True
    )
    assert not typography_fallback_required(
        typography,
        brief=slogan_brief.model_copy(update={"design_mode": DesignMode.TYPOGRAPHY}),
        budget_exhausted=True,
    )
    assert not typography_fallback_required(
        typography, brief=slogan_brief, budget_exhausted=True, already_applied=True
    )


def test_safe_layout_fallback_preserves_identity_art_and_exact_text() -> None:
    slogan = "KILN WEATHER BUREAU\nHEAT ADVISORY IN EFFECT"
    original = _brief().model_copy(update={
        "slogan": slogan,
        "design_mode": DesignMode.HYBRID,
        "strategy": ConceptStrategy(
            micro_niche="Ceramicists managing kiln-firing anxiety",
            premise="A studio treats firing temperature as a weather emergency.",
            brand_connection="A fictional public bureau fits the brand.",
            brand_fit=96,
            shareability=87,
            etsy_angle="Gift for a pottery-class friend.",
            amazon_angle="Searchable pottery identity.",
            shopify_angle="A maker-weather service bulletin.",
        ),
    })
    fallback, typography = build_safe_layout_fallback(
        original, ["Black", "Navy", "Natural"]
    )
    for field in (
        "concept_name", "target_customer", "customer_motivation", "slogan", "strategy"
    ):
        assert getattr(fallback, field) == getattr(original, field)
    assert fallback.design_mode == DesignMode.HYBRID
    assert fallback.visual_concept == original.visual_concept
    assert fallback.graphic_style == original.graphic_style
    assert fallback.palette == original.palette
    assert fallback.shirt_colors == ["Black", "Navy", "Natural"]
    assert fallback.artwork_distress_level == 0
    assert typography.exact_text == slogan
    assert typography.line_breaks == [
        "KILN WEATHER BUREAU", "HEAT ADVISORY", "IN EFFECT"
    ]
    assert typography.primary_color == "#E38D45"
    assert typography.outline is None
    assert typography.text_arc_or_shape == "none"
    assert typography.vertical_placement == "bottom"
    assert typography.distress_level == 0


def test_safe_layout_fallback_balances_long_one_line_slogan_without_changing_text() -> None:
    slogan = "CERAMIC EMERGENCY WEATHER OBSERVATION DEPARTMENT REPORTING FOR DUTY"
    original = _brief().model_copy(update={
        "slogan": slogan,
        "design_mode": DesignMode.HYBRID,
    })
    _, typography = build_safe_layout_fallback(original, ["Black"])
    assert len(typography.line_breaks) == 3
    assert " ".join(typography.line_breaks) == slogan
    assert typography.exact_text == slogan
    assert typography.relative_height == pytest.approx(0.30)


def test_safe_layout_fallback_keeps_descriptive_creative_palette() -> None:
    original = _brief().model_copy(update={
        "slogan": "KILN WEATHER",
        "palette": ["burnt orange", "warm cream", "smoky navy"],
    })

    fallback, typography = build_safe_layout_fallback(original, ["Black"])

    assert fallback.palette == original.palette
    assert typography.primary_color == "#F4E6CC"
    assert typography.outline == "#1A1F35"
