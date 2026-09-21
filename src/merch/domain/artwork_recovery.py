"""Deterministic recovery decisions for failed artwork briefs."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from merch.schemas import (
    CreativeBrief,
    DesignMode,
    QAIssue,
    TypographySpec,
    normalize_opaque_color,
)

type RecoveryStrategy = Literal["targeted", "structural_simplification"]

_LAYOUT_SEPARATION_CODES = frozenset({
    "NEGATIVE_SPACE", "ELEMENT_SEPARATION", "INTERIOR_COMPOSITION", "OVERLAP",
    "LAYOUT_SEPARATION",
})
_BRIEF_CONTRACT_CODES = frozenset({
    "BRIEF_CONTRACT", "PRODUCTION_VERIFICATION", "PRODUCT_VARIANT_MISMATCH",
    "UNSUPPORTED_BRIEF_REQUIREMENT", "UNVERIFIABLE_BRIEF_REQUIREMENT",
})
_TYPOGRAPHY_FALLBACK_CODES = frozenset({
    "TYPOGRAPHY_FORMATTING",
    "TYPOGRAPHY_LAYOUT",
    "TYPOGRAPHY_READABILITY",
})


def _normalized_code(code: str) -> str:
    return re.sub(r"[\s-]+", "_", code.strip().upper())


def normalize_issue_family(code: str) -> str:
    """Group known aliases without conflating unrelated print or effect defects."""
    normalized = _normalized_code(code)
    if normalized in _LAYOUT_SEPARATION_CODES:
        return "layout_separation"
    if normalized in _BRIEF_CONTRACT_CODES:
        return "brief_contract"
    return normalized.casefold()


def unsupported_brief_requirement_codes(issues: Iterable[QAIssue]) -> set[str]:
    """Find blocking contract findings; do not downgrade or remove the findings."""
    return {
        _normalized_code(issue.code) for issue in issues
        if issue.severity == "error" and _normalized_code(issue.code) in _BRIEF_CONTRACT_CODES
    }


@dataclass(frozen=True, slots=True)
class ArtworkFailure:
    version: int
    revision: int
    issues: tuple[QAIssue, ...]


@dataclass(frozen=True, slots=True)
class ArtworkFailureSummary:
    version: int
    revision: int
    issue_codes: tuple[str, ...]
    issue_families: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "revision": self.revision,
            "issue_codes": list(self.issue_codes),
            "issue_families": list(self.issue_families),
        }


@dataclass(frozen=True, slots=True)
class RecoveryContext:
    attempt: int
    strategy: RecoveryStrategy
    reasons: tuple[str, ...] = ()
    history: tuple[ArtworkFailureSummary, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "strategy": self.strategy,
            "reasons": list(self.reasons),
            "history": [item.to_dict() for item in self.history],
        }


def build_recovery_context(
    failures: Iterable[ArtworkFailure],
    *,
    attempt: int,
    previous_strategy: RecoveryStrategy | None = None,
) -> RecoveryContext:
    """Summarize persisted failures and select the next bounded rewrite strategy.

    The caller owns the overall retry budget. ``attempt`` is the one-based number
    of the next rewrite, including earlier rejected rewrite attempts.
    """
    if attempt < 1:
        raise ValueError("recovery attempt must be positive")
    versions: dict[int, tuple[int, set[str]]] = {}
    for failure in failures:
        revision, codes = versions.setdefault(failure.version, (failure.revision, set()))
        codes.update(
            _normalized_code(issue.code) for issue in failure.issues if issue.severity == "error"
        )
        versions[failure.version] = (max(revision, failure.revision), codes)
    history = tuple(
        ArtworkFailureSummary(
            version=version,
            revision=revision,
            issue_codes=tuple(sorted(codes)),
            issue_families=tuple(sorted({normalize_issue_family(code) for code in codes})),
        )
        for version, (revision, codes) in sorted(versions.items()) if codes
    )
    layout_versions = [
        item.version for item in history if "layout_separation" in item.issue_families
    ]
    reasons = []
    if len(layout_versions) >= 2:
        reasons.append(
            "Layout/separation defects failed across brief versions "
            + ", ".join(map(str, layout_versions)) + "."
        )
    if attempt >= 3:
        reasons.append("The third or later rewrite requires structural simplification.")
    if previous_strategy == "structural_simplification":
        reasons.append("Structural simplification was already selected for this run.")
    strategy: RecoveryStrategy = "structural_simplification" if reasons else "targeted"
    if any("brief_contract" in item.issue_families for item in history):
        reasons.append("Correct unsupported brief requirements before regenerating artwork.")
    return RecoveryContext(
        attempt=attempt, strategy=strategy, reasons=tuple(reasons), history=history,
    )


def _normalized_instructions(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"\w+", normalized))


def structural_rewrite_is_material(previous: CreativeBrief, revised: CreativeBrief) -> bool:
    """Require new layout and rendering instructions, rather than appended repairs.

    This checks replacement of instructions, not their artistic meaning. Visible
    improvements still have to pass the ordinary artwork QA gates.
    """
    for field in ("composition", "generation_brief"):
        old = _normalized_instructions(getattr(previous, field))
        new = _normalized_instructions(getattr(revised, field))
        if not new or (old and f" {old} " in f" {new} "):
            return False
    return True


def typography_fallback_required(
    failures: Iterable[ArtworkFailure],
    *,
    brief: CreativeBrief,
    budget_exhausted: bool,
    already_applied: bool = False,
) -> bool:
    """Choose the one-time deterministic-layout escape hatch for slogan-bearing artwork.

    Repeated layout failures prove that prose-only brief rewrites cannot repair the
    compositor. The fallback preserves the selected concept and design mode while
    regenerating its illustration above exact typography in a reserved band.
    """
    if already_applied or not brief.slogan or not brief.slogan.strip():
        return False
    if brief.design_mode == DesignMode.TYPOGRAPHY:
        return False
    failures = tuple(failures)
    affected_versions = {
        failure.version
        for failure in failures
        if any(
            issue.severity == "error" and _normalized_code(issue.code) in _TYPOGRAPHY_FALLBACK_CODES
            for issue in failure.issues
        )
    }
    if not failures:
        return False
    latest = max(failures, key=lambda failure: (failure.version, failure.revision))
    latest_has_typography_failure = any(
        issue.severity == "error"
        and _normalized_code(issue.code) in _TYPOGRAPHY_FALLBACK_CODES
        for issue in latest.issues
    )
    if not latest_has_typography_failure:
        return False
    return budget_exhausted or len(affected_versions) >= 2


def _balanced_soft_wrap(line: str) -> list[str]:
    words = line.split(" ")
    if len(line) <= 20 or len(words) < 2 or " ".join(words) != line:
        return [line]
    line_count = min(len(words), 2 if len(line) <= 52 else 3)
    prefix = [0]
    for word in words:
        prefix.append(prefix[-1] + len(word))

    def segment_length(start: int, end: int) -> int:
        return prefix[end] - prefix[start] + max(0, end - start - 1)

    best_key: tuple[int, int] | None = None
    best_cuts: tuple[int, ...] | None = None
    candidates: Iterable[tuple[int, ...]]
    if line_count == 2:
        candidates = ((cut,) for cut in range(1, len(words)))
    else:
        candidates = (
            (first, second)
            for first in range(1, len(words) - 1)
            for second in range(first + 1, len(words))
        )
    for cuts in candidates:
        boundaries = (0, *cuts, len(words))
        lengths = [
            segment_length(boundaries[index], boundaries[index + 1])
            for index in range(line_count)
        ]
        key = (max(lengths), max(lengths) - min(lengths))
        if best_key is None or key < best_key:
            best_key = key
            best_cuts = cuts
    if best_cuts is None:
        return [line]
    boundaries = (0, *best_cuts, len(words))
    return [
        " ".join(words[boundaries[index]:boundaries[index + 1]])
        for index in range(line_count)
    ]


def _fallback_line_breaks(slogan: str) -> list[str]:
    return [line for hard_line in slogan.split("\n") for line in _balanced_soft_wrap(hard_line)]


def build_safe_layout_fallback(
    brief: CreativeBrief,
    allowed_shirt_colors: list[str],
) -> tuple[CreativeBrief, TypographySpec]:
    """Preserve concept identity while regenerating art above a safe typography band."""
    slogan = brief.slogan
    if slogan is None or not slogan.strip():
        raise ValueError("Typography fallback requires a nonblank approved slogan")
    lines = _fallback_line_breaks(slogan)
    palette_colors: list[str] = []
    for entry in brief.palette:
        try:
            color = normalize_opaque_color(entry)
        except ValueError:
            continue
        if color not in palette_colors:
            palette_colors.append(color)
    if not palette_colors:
        palette_colors = ["#F4E6CC", "#1A1F35"]
    primary_color = palette_colors[0]
    outline = None
    if len(palette_colors) > 1:
        primary_rgb = tuple(int(primary_color[index:index + 2], 16) for index in (1, 3, 5))
        outline = max(
            palette_colors[1:],
            key=lambda color: sum(
                (int(color[index:index + 2], 16) - primary_rgb[offset]) ** 2
                for offset, index in enumerate((1, 3, 5))
            ),
        )
    fallback = CreativeBrief.model_validate({
        **brief.model_dump(mode="json"),
        "composition": (
            "Use one dominant premise-specific illustration in the upper region and reserve a "
            "separate lower band for the exact approved slogan. Keep a clear gap between art and "
            "lettering, confident print presence, and an unmistakable focal hierarchy."
        ),
        # Keep the approved creative palette verbatim. Descriptive palette
        # entries are valid generation guidance even when the deterministic
        # typography renderer needs concrete fallback colors of its own.
        "palette": list(brief.palette),
        "shirt_colors": list(allowed_shirt_colors),
        "typography_style": (
            "Exact approved wording in bold upright slab-serif lettering, straight and centered, "
            f"with {primary_color} fill"
            + (f" and a {outline} outline." if outline else " and no outline.")
        ),
        "generation_brief": (
            f"Create one original, premise-specific illustration of: {brief.visual_concept}. "
            "Use a strong dominant silhouette and restrained supporting detail. Generate no text, "
            "letters, pseudo-writing, badge, border, background panel, garment, wearer, mockup, "
            "texture, or distress. Application prepress places the exact slogan below the art."
        ),
        "artwork_distress_level": 0,
    })
    typography = TypographySpec(
        exact_text=slogan,
        font_category="slab",
        font_weight=700,
        capitalization="exact",
        line_breaks=lines,
        letter_spacing=0.0,
        line_spacing=1.15,
        text_alignment="center",
        text_arc_or_shape="none",
        outline=outline,
        shadow=None,
        distress_level=0,
        primary_color=primary_color,
        secondary_color=None,
        vertical_placement="bottom",
        interaction_with_illustration=(
            "Straight centered text in a reserved lower band, separated from the illustration."
        ),
        relative_width=0.88,
        relative_height=0.28 if len(lines) <= 2 else 0.30,
    )
    return fallback, typography
