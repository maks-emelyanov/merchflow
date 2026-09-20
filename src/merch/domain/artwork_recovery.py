"""Deterministic recovery decisions for failed artwork briefs."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from merch.schemas import CreativeBrief, QAIssue

type RecoveryStrategy = Literal["targeted", "structural_simplification"]

_LAYOUT_SEPARATION_CODES = frozenset({
    "NEGATIVE_SPACE", "ELEMENT_SEPARATION", "INTERIOR_COMPOSITION", "OVERLAP",
    "LAYOUT_SEPARATION",
})
_BRIEF_CONTRACT_CODES = frozenset({
    "BRIEF_CONTRACT", "PRODUCTION_VERIFICATION", "PRODUCT_VARIANT_MISMATCH",
    "UNSUPPORTED_BRIEF_REQUIREMENT", "UNVERIFIABLE_BRIEF_REQUIREMENT",
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
