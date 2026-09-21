from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.models import Base
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import (
    ApprovalSignal,
    CandidateConcept,
    Channel,
    CreativeBrief,
    PublishStatus,
    RunInput,
    RunStatus,
)
from merch.services.openai_service import OpenAIService
from merch.temporal import MerchWorkflow, retry_failed_artwork_run


@pytest.mark.asyncio
@pytest.mark.parametrize("patched,successful,expected", [
    (True, True, RunStatus.PUBLISHED.value),
    (True, False, RunStatus.AWAITING_BRIEF_REVISION.value),
    (False, False, RunStatus.REJECTED.value),
])
async def test_requested_regeneration_recovers_or_preserves_old_workflow_history(
    monkeypatch: pytest.MonkeyPatch, patched: bool, successful: bool, expected: str,
) -> None:
    workflow = MerchWorkflow()
    calls: list[tuple[str, Any]] = []
    generations = 0
    rewrites = 0

    async def execute(name: str, argument: Any, **kwargs: Any) -> Any:
        nonlocal generations, rewrites
        calls.append((name, argument))
        if name == "research_run":
            return None
        if name == "screen_and_select_run":
            return True
        if name == "generate_package_run":
            generations += 1
            return generations == 1 or (successful and generations == 4)
        if name == "rewrite_failed_brief":
            rewrites += 1
            # The activity owns the existing rewrite cap. Its terminal result
            # must stop this workflow without another generation/publication.
            if not successful and rewrites == 2:
                return RunStatus.AWAITING_BRIEF_REVISION.value
            return RunStatus.PENDING.value
        if name == "automatic_approval_signal":
            if generations == 1:
                return None
            return ApprovalSignal(
                channels=[Channel.ETSY], expected_version=4, actor="system", ip_attested=False,
            )
        if name == "record_approval":
            assert argument["signal"]["expected_version"] == 4
            return True
        if name == "revalidate_approval":
            return True
        if name == "publish_channel":
            return PublishStatus.DRY_RUN
        if name in {"finish_publishing", "record_rejection"}:
            return None
        raise AssertionError(f"Unexpected workflow activity: {name}")

    async def wait_condition(predicate: Any) -> None:
        if predicate():
            return
        if generations == 1:
            await workflow.regenerate()
        else:
            assert not patched, "New failures must recover or terminate before waiting again"
            await workflow.reject("test-admin")
        assert predicate()

    def patch(identifier: str) -> bool:
        assert identifier == "artwork-effects-regeneration-recovery-v1"
        return patched

    monkeypatch.setattr("merch.temporal.workflow.execute_activity", execute)
    monkeypatch.setattr("merch.temporal.workflow.wait_condition", wait_condition)
    monkeypatch.setattr("merch.temporal.workflow.patched", patch)
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    assert await workflow.run(value) == expected

    names = [name for name, _ in calls]
    assert names.count("research_run") == names.count("screen_and_select_run") == 1
    generation_calls = [argument for name, argument in calls if name == "generate_package_run"]
    assert generation_calls[1] == {"run_id": str(value.run_id), "regenerate": True}
    if patched:
        assert rewrites == 2
        assert all(argument == {
            "run_id": str(value.run_id), "regenerate": False, "preserve_brief": True,
        } for argument in generation_calls[2:])
    else:
        assert rewrites == 0
        assert generations == 2
    assert names.count("publish_channel") == int(successful)


async def _failed_artwork_run(*, legacy: bool = False) -> tuple[str, CreativeBrief, CandidateConcept]:
    Base.metadata.create_all(get_engine())
    template = fixture_product_template()
    service = OpenAIService(get_settings())
    concept = (await service.research(date.today(), "none")).value.candidates[0]
    brief = (await service.creative(concept, {})).value.model_copy(update={
        "shirt_colors": sorted({item.color for item in template.variants if item.enabled}),
    })
    concept_data = concept.model_dump(mode="json")
    brief_data = brief.model_dump(mode="json")
    if legacy:
        concept_data.pop("strategy")
        brief_data.pop("strategy")
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    run_id = str(value.run_id)
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
        repository = RunRepository(session)
        record = repository.create(value, f"retry-test-{run_id}")
        record.selected_concept = concept_data
        record.creative_brief = brief_data
        record.status = RunStatus.AWAITING_BRIEF_REVISION.value
        repository.add_artifact(
            run_id, kind="production-v1", revision=1, object_key=f"failed-{run_id}",
            sha256="0" * 64, width=400, height=500, metadata={},
        )
    return run_id, CreativeBrief.model_validate(brief_data), concept


@pytest.mark.asyncio
async def test_unchanged_legacy_brief_is_rejected_after_schema_defaults(
    isolated_app: Path,
) -> None:
    run_id, brief, _ = await _failed_artwork_run(legacy=True)
    with session_scope() as session:
        saved = RunRepository(session).get(run_id).creative_brief
        assert saved is not None
        assert "strategy" not in saved
        assert brief.model_dump(mode="json") != saved
    with pytest.raises(ValueError, match="Revise the creative brief"):
        await retry_failed_artwork_run(run_id, revised_brief=brief)
    with session_scope() as session:
        assert RunRepository(session).get(run_id).status == RunStatus.AWAITING_BRIEF_REVISION.value


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_strategy", [False, True])
async def test_manual_revision_restores_selected_strategy(
    isolated_app: Path, monkeypatch: pytest.MonkeyPatch, replace_strategy: bool,
) -> None:
    run_id, brief, concept = await _failed_artwork_run()
    assert concept.strategy is not None
    revised = brief.model_copy(update={
        "composition": "Use an open composition with no surrounding frame",
        "strategy": (
            concept.strategy.model_copy(update={"premise": "A different unselected idea"})
            if replace_strategy else None
        ),
    })
    starts: list[RunInput] = []

    class Client:
        async def start_workflow(self, workflow: Any, value: RunInput, **kwargs: Any) -> None:
            starts.append(value)

    async def client(settings: Any) -> Client:
        return Client()

    monkeypatch.setattr("merch.temporal.temporal_client", client)
    result = await retry_failed_artwork_run(run_id, revised_brief=revised)
    assert result.reuse_brief and result.reuse_selection and result.reuse_research
    assert starts == [result]
    with session_scope() as session:
        record = RunRepository(session).get(run_id)
        saved = CreativeBrief.model_validate(record.creative_brief)
        assert saved.strategy == concept.strategy
        assert saved.composition == revised.composition
        assert record.status == RunStatus.PENDING.value


@pytest.mark.asyncio
async def test_removing_strategy_is_not_an_artwork_revision(isolated_app: Path) -> None:
    run_id, brief, _ = await _failed_artwork_run()
    with pytest.raises(ValueError, match="Revise the creative brief"):
        await retry_failed_artwork_run(
            run_id, revised_brief=brief.model_copy(update={"strategy": None}),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("slogan", ["DIFFERENT PRINTED WORDS", None])
async def test_manual_strategy_revision_rejects_changed_printed_words(
    isolated_app: Path, slogan: str | None,
) -> None:
    run_id, brief, _ = await _failed_artwork_run()
    revised = brief.model_copy(update={
        "composition": "An open composition with more separation",
        "slogan": slogan,
    })
    with pytest.raises(ValueError, match="exact printed slogan"):
        await retry_failed_artwork_run(run_id, revised_brief=revised)
    with session_scope() as session:
        record = RunRepository(session).get(run_id)
        assert record.status == RunStatus.AWAITING_BRIEF_REVISION.value
        assert CreativeBrief.model_validate(record.creative_brief).slogan == brief.slogan


@pytest.mark.asyncio
async def test_manual_legacy_revision_retains_slogan_edit_behavior(
    isolated_app: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, brief, _ = await _failed_artwork_run(legacy=True)
    revised = brief.model_copy(update={"slogan": "REVISED LEGACY WORDING"})

    class Client:
        async def start_workflow(self, workflow: Any, value: RunInput, **kwargs: Any) -> None:
            pass

    async def client(settings: Any) -> Client:
        return Client()

    monkeypatch.setattr("merch.temporal.temporal_client", client)
    await retry_failed_artwork_run(run_id, revised_brief=revised)
    with session_scope() as session:
        saved = CreativeBrief.model_validate(RunRepository(session).get(run_id).creative_brief)
        assert saved.slogan == revised.slogan
