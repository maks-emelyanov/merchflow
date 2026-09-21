from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.domain.artwork_recovery import RecoveryContext
from merch.domain.prepress import largest_generation_size
from merch.models import AuditEvent, Base
from merch.pipeline import create_run, generate_package_run, rewrite_failed_brief_run
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import CandidateConcept, CreativeBrief, QAIssue, QAReport, RunInput, RunStatus
from merch.services.openai_service import ModelResult, OpenAIService
from merch.temporal import retry_failed_artwork_run


async def failed_run(version: int = 1) -> tuple[str, CreativeBrief]:
    Base.metadata.create_all(get_engine())
    template = fixture_product_template()
    service = OpenAIService(get_settings())
    research = await service.research(date.today(), "fixture")
    concept = research.value.candidates[0]
    result = await service.creative(concept, template.model_dump(mode="json"))
    brief = result.value.model_copy(update={
        "shirt_colors": sorted({variant.color for variant in template.variants}),
    })
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=False)
    run_id = str(value.run_id)
    create_run(value, f"fixture-{run_id}")
    qa = QAReport(
        passed=False, revision=1, width=400, height=500, has_alpha=True, color_profile="RGBA",
        issues=[QAIssue(code="NEGATIVE_SPACE", severity="error", message="Crowded figures")],
    )
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
        repo = RunRepository(session)
        run = repo.get(run_id)
        run.selected_concept = concept.model_dump(mode="json")
        run.creative_brief = brief.model_dump(mode="json")
        run.version = version
        run.status = RunStatus.AWAITING_BRIEF_REVISION.value
        repo.add_artifact(
            run_id, kind=f"production-v{version}", revision=1,
            object_key=f"fixture-{run_id}", sha256="0" * 64, width=400, height=500,
            metadata={"qa": qa.model_dump(mode="json")},
        )
    return run_id, brief


def attempts_for(run_id: str) -> list[dict[str, Any]]:
    with session_scope() as session:
        events = session.scalars(select(AuditEvent).where(
            AuditEvent.run_id == run_id, AuditEvent.action == "artwork.brief_rewrite_attempt",
        ).order_by(AuditEvent.created_at, AuditEvent.id))
        return [event.detail for event in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_historical_artwork_recovery_keeps_saved_garment_and_palette(
    isolated_app: Path, monkeypatch: pytest.MonkeyPatch, automatic: bool,
) -> None:
    run_id, brief = await failed_run()
    saved_template = fixture_product_template()
    active = saved_template.model_copy(update={
        "blueprint_id": 706, "print_provider_id": 99,
        "print_width": 4494, "print_height": 5097,
        "variants": [item.model_copy(update={"color": "Pepper"}) for item in saved_template.variants],
    })
    with session_scope() as session:
        RunRepository(session).get(run_id).template_snapshot = saved_template.model_dump(mode="json")
        ConfigurationRepository(session).save_template(active)

    if automatic:
        assert await rewrite_failed_brief_run(run_id) == RunStatus.PENDING.value
    else:
        class Client:
            async def start_workflow(self, workflow: Any, value: RunInput, **kwargs: Any) -> None:
                pass

        async def client(settings: Any) -> Client:
            return Client()

        monkeypatch.setattr("merch.temporal.temporal_client", client)
        await retry_failed_artwork_run(run_id, revised_brief=brief.model_copy(update={
            "composition": "Broad standalone trail shapes with clear gaps and no enclosing border",
        }))
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert run.template_snapshot == saved_template.model_dump(mode="json")
        assert CreativeBrief.model_validate(run.creative_brief).shirt_colors == brief.shirt_colors

    class GenerationReached(Exception):
        pass

    async def artwork(
        self: OpenAIService, current_brief: CreativeBrief, width: int, height: int,
    ) -> tuple[bytes, dict[str, Any]]:
        assert (width, height) == largest_generation_size(saved_template.print_width, saved_template.print_height)
        assert current_brief.shirt_colors == brief.shirt_colors
        raise GenerationReached

    monkeypatch.setattr(OpenAIService, "artwork", artwork)
    with pytest.raises(GenerationReached):
        await generate_package_run(run_id, regenerate=not automatic, preserve_brief=True)
    with session_scope() as session:
        assert RunRepository(session).get(run_id).template_snapshot == saved_template.model_dump(mode="json")
        assert ConfigurationRepository(session).get_template() == active


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_fields", [("strategy",), ("strategy", "artwork_distress_level")])
async def test_legacy_brief_defaults_do_not_supersede_automatic_recovery(
    isolated_app: Path, missing_fields: tuple[str, ...],
) -> None:
    run_id, original = await failed_run()
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert run.creative_brief is not None and run.selected_concept is not None
        run.creative_brief = {
            key: value for key, value in run.creative_brief.items() if key not in missing_fields
        }
        run.selected_concept = {
            key: value for key, value in run.selected_concept.items() if key != "strategy"
        }
        assert CreativeBrief.model_validate(run.creative_brief).model_dump(mode="json") != run.creative_brief

    assert await rewrite_failed_brief_run(run_id) == RunStatus.PENDING.value
    assert [attempt["outcome"] for attempt in attempts_for(run_id)] == ["accepted"]
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert run.version == 2
        revised = CreativeBrief.model_validate(run.creative_brief)
        assert revised.strategy is None
        assert revised.slogan == original.slogan
        assert revised.composition != original.composition
        assert revised.generation_brief != original.generation_brief
        assert run.provider_calls[-1]["outcome"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 5])
async def test_rejected_rewrites_consume_budget_including_legacy_versions(
    isolated_app: Path, monkeypatch: pytest.MonkeyPatch, version: int,
) -> None:
    run_id, original = await failed_run(version)
    # Slogan-bearing artwork has a final deterministic typography fallback.
    # Keep this test focused on the ordinary no-fallback budget path.
    original = original.model_copy(update={"slogan": None})
    with session_scope() as session:
        RunRepository(session).get(run_id).creative_brief = original.model_dump(mode="json")
    contexts: list[RecoveryContext] = []

    async def reject(
        self: OpenAIService, concept: CandidateConcept, brief: CreativeBrief,
        issues: list[QAIssue], colors: list[str], *, recovery_context: RecoveryContext | None = None,
    ) -> ModelResult[CreativeBrief]:
        assert recovery_context is not None
        contexts.append(recovery_context)
        # Targeted no-ops and structural append-only proposals must both consume attempts.
        candidate = brief if recovery_context.attempt < 3 else brief.model_copy(update={
            "composition": brief.composition + ". Also make more space.",
            "generation_brief": brief.generation_brief + ". Remove the border.",
        })
        return ModelResult(candidate, {"model": "fixture"})

    monkeypatch.setattr(OpenAIService, "revise_brief", reject)
    assert await rewrite_failed_brief_run(run_id) == RunStatus.AWAITING_BRIEF_REVISION.value
    assert [context.attempt for context in contexts] == list(range(version, 9))
    assert all(context.strategy == "structural_simplification" for context in contexts if context.attempt >= 3)
    assert len(attempts_for(run_id)) == 9 - version
    assert all(attempt["outcome"] == "rejected" for attempt in attempts_for(run_id))
    assert await rewrite_failed_brief_run(run_id) == RunStatus.AWAITING_BRIEF_REVISION.value
    assert len(contexts) == 9 - version
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.version == version
        assert run.creative_brief == original.model_dump(mode="json")
        assert len(run.artifacts) == 1
        assert "budget exhausted (8 attempts)" in (run.error or "")
        assert not run.approvals and not run.publishes


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_provider_interruption_consumes_attempt_and_completed_retry_is_idempotent(
    isolated_app: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool,
) -> None:
    run_id, original = await failed_run()
    contexts: list[RecoveryContext] = []

    async def rewrite(
        self: OpenAIService, concept: CandidateConcept, brief: CreativeBrief,
        issues: list[QAIssue], colors: list[str], *, recovery_context: RecoveryContext | None = None,
    ) -> ModelResult[CreativeBrief]:
        assert recovery_context is not None
        contexts.append(recovery_context)
        if len(contexts) == 1:
            if interrupted:
                raise asyncio.CancelledError
            raise RuntimeError("Provider unavailable")
        return ModelResult(brief.model_copy(update={
            "composition": "A freestanding sun above separate broad runner shapes",
            "generation_brief": "Draw a simple open running scene with clear gaps",
        }), {"model": "fixture"})

    monkeypatch.setattr(OpenAIService, "revise_brief", rewrite)
    with pytest.raises(asyncio.CancelledError if interrupted else RuntimeError):
        await rewrite_failed_brief_run(run_id)
    assert attempts_for(run_id)[0]["outcome"] == ("started" if interrupted else "provider_error")
    assert await rewrite_failed_brief_run(run_id) == RunStatus.PENDING.value
    assert await rewrite_failed_brief_run(run_id) == RunStatus.PENDING.value
    assert [context.attempt for context in contexts] == [1, 2]
    assert [attempt["outcome"] for attempt in attempts_for(run_id)] == [
        "interrupted" if interrupted else "provider_error", "accepted",
    ]
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert run.version == 2
        assert run.creative_brief["slogan"] == original.slogan


@pytest.mark.asyncio
async def test_late_provider_response_cannot_overwrite_retry_or_reset_strategy(
    isolated_app: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, _ = await failed_run()
    with session_scope() as session:
        RunRepository(session).audit(run_id, "worker", "artwork.brief_rewrite_attempt", {
            "outcome": "rejected", "from_version": 1,
            "recovery_context": {"attempt": 1, "strategy": "structural_simplification"},
        })
    started, release = asyncio.Event(), asyncio.Event()
    contexts: list[RecoveryContext] = []

    async def rewrite(
        self: OpenAIService, concept: CandidateConcept, brief: CreativeBrief,
        issues: list[QAIssue], colors: list[str], *, recovery_context: RecoveryContext | None = None,
    ) -> ModelResult[CreativeBrief]:
        assert recovery_context is not None
        contexts.append(recovery_context)
        if len(contexts) == 1:
            started.set()
            await release.wait()
            composition = "Superseded open layout"
        else:
            composition = "Accepted open layout"
        return ModelResult(brief.model_copy(update={
            "composition": composition,
            "generation_brief": "Draw clearly separated primary motifs in a spacious composition",
        }), {"model": "fixture"})

    monkeypatch.setattr(OpenAIService, "revise_brief", rewrite)
    stale = asyncio.create_task(rewrite_failed_brief_run(run_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert await rewrite_failed_brief_run(run_id) == RunStatus.PENDING.value
    finally:
        release.set()
        await stale
    assert [context.attempt for context in contexts] == [2, 3]
    assert all(context.strategy == "structural_simplification" for context in contexts)
    assert [attempt["outcome"] for attempt in attempts_for(run_id)] == [
        "rejected", "superseded", "accepted",
    ]
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert run.version == 2
        assert run.creative_brief["composition"] == "Accepted open layout"
        assert {call["outcome"] for call in run.provider_calls} == {"accepted", "superseded"}
