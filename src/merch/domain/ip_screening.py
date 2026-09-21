from __future__ import annotations

import re
from urllib.parse import quote_plus

from merch.domain.concept_ranking import weighted_concept_score as weighted_concept_score
from merch.schemas import CandidateConcept, IPMatch, IPScreeningReport

FORBIDDEN_TERMS = {
    "barbie",
    "beyonce",
    "disney",
    "fortnite",
    "harry potter",
    "marvel",
    "minecraft",
    "mlb",
    "nba",
    "nfl",
    "nhl",
    "nintendo",
    "pokemon",
    "star wars",
    "taylor swift",
}


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def screen_concept(concept: CandidateConcept, threshold: int = 20) -> IPScreeningReport:
    text = _normalise(
        " ".join(
            filter(
                None,
                [
                    concept.concept_name,
                    concept.slogan_if_any,
                    concept.visual_concept,
                    concept.graphic_style,
                ],
            )
        )
    )
    matches: list[IPMatch] = []
    for term in sorted(FORBIDDEN_TERMS):
        if re.search(rf"\b{re.escape(term)}\b", text):
            matches.append(
                IPMatch(
                    source="policy-denylist",
                    term=term,
                    explanation="Recognizable brand, franchise, league, game, or celebrity reference",
                    blocking=True,
                )
            )
    risk = max(concept.scores.ip_risk, 100 if matches else 0)
    status = "block" if matches or risk > threshold else ("review" if risk else "pass")
    phrase = concept.slogan_if_any or concept.concept_name
    url = f"https://tmsearch.uspto.gov/search?query={quote_plus(phrase)}"
    return IPScreeningReport(
        status=status,
        risk_score=risk,
        searched_terms=[phrase, concept.concept_name],
        matches=matches,
        uspto_search_url=url,
        notes=[
            "Automated screening is not legal clearance.",
            "Review exact and confusingly similar apparel uses before publication.",
        ],
        legal_clearance=False,
    )


def ip_report_eligible(report: IPScreeningReport, threshold: int = 20) -> bool:
    """AI status alone cannot override blocking matches or the hard risk cutoff."""
    return (
        report.status != "block"
        and report.risk_score <= threshold
        and not any(match.blocking for match in report.matches)
    )
