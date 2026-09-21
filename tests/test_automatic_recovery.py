from __future__ import annotations

import io
import os
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.domain.artwork_recovery import RecoveryContext
from merch.models import Base
from merch.pipeline import create_run
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import (
    ApprovalSignal,
    CandidateConcept,
    Channel,
    ChannelConfig,
    CreativeBrief,
    DesignMode,
    PublishInput,
    PublishStatus,
    QAIssue,
    QAReport,
    ResearchReport,
    RunInput,
    RunStatus,
)
from merch.services.openai_service import ModelResult, OpenAIService
from merch.setup_etsy_tee import COLORS, SIZES, build_template
from merch.temporal import (
    MerchWorkflow,
    approval_activity,
    automatic_approval_activity,
    finish_activity,
    generate_activity,
    mark_failed_activity,
    publish_activity,
    research_activity,
    revalidate_approval_activity,
    rewrite_failed_brief_activity,
    screen_activity,
)


@pytest.mark.asyncio
async def test_scheduled_run_recovers_contract_and_separation_failures_without_intervention(
    isolated_app: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay the manual recovery through real workflow activities and dry-run publication."""
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    catalog = {
        "variants": [
            {
                "id": index + 1, "title": f"{color} / {size}",
                "options": {"color": color, "size": size},
                "placeholders": [{
                    "position": "front", "decoration_method": "dtg", "width": 400, "height": 500,
                }],
            }
            for index, (color, size) in enumerate(
                (color, size) for color in COLORS for size in SIZES
            )
        ],
    }
    template = build_template(catalog, ChannelConfig(
        channel=Channel.ETSY, printify_shop_id="fixture-etsy",
        percent_fee=0.12, fixed_fee_cents=45,
    )).model_copy(update={"print_width": 400, "print_height": 500})
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=False)
    run_id = str(value.run_id)
    create_run(value, f"merch-daily-recovery-{run_id}")

    metadata = {"model": "fixture", "estimated_cost_usd": 0.0}
    immutable_fields = {
        "concept_name", "target_customer", "customer_motivation", "slogan", "design_mode",
    }
    original_identity: dict[str, Any] = {}
    contexts: list[RecoveryContext] = []
    generated_versions: list[int] = []
    edited_versions: list[int] = []
    qa_calls: list[tuple[int, int]] = []
    original_research = OpenAIService.research

    def current_version() -> int:
        with session_scope() as session:
            return RunRepository(session).get(run_id).version

    async def research(
        self: OpenAIService, current_date: date, performance_summary: str,
        **context: Any,
    ) -> ModelResult[ResearchReport]:
        result = await original_research(self, current_date, performance_summary, **context)
        first = result.value.candidates[0].model_copy(update={
            "concept_name": "Evening running club", "target_customer": "Social runners",
            "customer_motivation": "Celebrating an evening run with friends",
            "slogan_if_any": "MEET AT DUSK", "design_mode": DesignMode.HYBRID,
            "visual_concept": "A group of runners enjoying an evening outing",
            "palette": ["#111111", "#FFFFFF", "#D97148"],
        })
        return ModelResult(result.value.model_copy(update={
            "candidates": [first, *result.value.candidates[1:]],
        }), result.metadata)

    async def creative(
        self: OpenAIService, concept: CandidateConcept, product_template: dict[str, Any],
    ) -> ModelResult[CreativeBrief]:
        brief = CreativeBrief(
            concept_name=concept.concept_name, target_customer=concept.target_customer,
            customer_motivation=concept.customer_motivation,
            slogan=concept.slogan_if_any, design_mode=concept.design_mode,
            visual_concept=concept.visual_concept,
            composition="Five runners inside a closed circular badge with a sunset behind them.",
            graphic_style=concept.graphic_style, palette=concept.palette,
            shirt_colors=list(COLORS), typography_style="bold geometric sans",
            generation_brief=(
                "Draw the five-runner enclosed sunset badge. Supply vector source and physical "
                "shirt proofs proving exact pixel gaps on every garment color."
            ),
        )
        original_identity.update(brief.model_dump(mode="json", include=immutable_fields))
        return ModelResult(brief, metadata)

    async def artwork(
        self: OpenAIService, brief: CreativeBrief, width: int, height: int,
    ) -> tuple[bytes, dict[str, Any]]:
        assert brief.model_dump(mode="json", include=immutable_fields) == original_identity
        assert set(brief.shirt_colors) == set(COLORS)
        version = current_version()
        generated_versions.append(version)
        image = Image.new("RGBA", (width, height))
        draw = ImageDraw.Draw(image)
        left, right = width // 8, width * 7 // 8
        top, bottom = height // 8 + version, height * 3 // 4
        # Broad balanced fills pass real contrast QA on all fourteen fixture garments.
        first = left + (right - left) * 2 // 5
        second = left + (right - left) * 4 // 5
        draw.rectangle((left, top, first, bottom), fill="#111111")
        draw.rectangle((first, top, second, bottom), fill="#FFFFFF")
        draw.rectangle((second, top, right, bottom), fill="#D97148")
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue(), metadata

    async def edit(
        self: OpenAIService, image: bytes, brief: CreativeBrief, issues: list[QAIssue],
    ) -> tuple[bytes, dict[str, Any]]:
        edited_versions.append(current_version())
        assert all(issue.code != "BRIEF_CONTRACT" for issue in issues)
        return image, metadata

    async def visual(
        self: OpenAIService, image: bytes, brief: CreativeBrief, deterministic: QAReport,
        *, effects: dict[str, Any] | None = None,
    ) -> ModelResult[QAReport]:
        assert deterministic.passed
        assert set(brief.shirt_colors) == set(COLORS)
        assert brief.model_dump(mode="json", include=immutable_fields) == original_identity
        version = current_version()
        qa_calls.append((version, deterministic.revision))
        if version == 1:
            issues = [
                QAIssue(
                    code="BRIEF_CONTRACT", severity="error",
                    message="A raster image cannot supply vector source or physical garment proofs.",
                    recommended_fix="Remove the unsupported proof and exact-pixel requirements.",
                ),
                QAIssue(code="NEGATIVE_SPACE", severity="error", message="Badge crowds the runners."),
            ]
        elif version == 2:
            assert "physical" not in brief.generation_brief
            issues = [QAIssue(
                code="NEGATIVE_SPACE" if deterministic.revision == 1 else "ELEMENT_SEPARATION",
                severity="error", message="Runner shapes still merge inside the closed border.",
                recommended_fix="Use detached broad figures in an open arrangement.",
            )]
        else:
            assert version == 3
            assert brief.composition == "Detached running silhouettes in an open arrangement."
            issues = []
        return ModelResult(deterministic.model_copy(update={
            "passed": not issues, "issues": issues,
        }), metadata)

    async def rewrite(
        self: OpenAIService, concept: CandidateConcept, brief: CreativeBrief,
        issues: list[QAIssue], shirt_colors: list[str],
        *, recovery_context: RecoveryContext | None = None,
    ) -> ModelResult[CreativeBrief]:
        assert recovery_context is not None
        contexts.append(recovery_context)
        assert set(shirt_colors) == set(COLORS)
        assert concept.concept_name == original_identity["concept_name"]
        changes: dict[str, Any] = {
            "concept_name": "Unrelated concept", "target_customer": "Different audience",
            "customer_motivation": "Different motivation", "slogan": "WRONG SLOGAN",
            "design_mode": DesignMode.TYPOGRAPHY, "shirt_colors": ["White"],
        }
        if recovery_context.strategy == "targeted":
            assert recovery_context.attempt == 1
            changes["generation_brief"] = (
                "Draw five broad runners within the evening circular badge using clear spacing."
            )
        else:
            assert recovery_context.attempt == 2
            changes.update({
                "composition": "Detached running silhouettes in an open arrangement.",
                "generation_brief": (
                    "Draw a few bold independent runners with broad printable gaps and no frame."
                ),
            })
        return ModelResult(brief.model_copy(update=changes), metadata)

    monkeypatch.setattr(OpenAIService, "research", research)
    monkeypatch.setattr(OpenAIService, "creative", creative)
    monkeypatch.setattr(OpenAIService, "artwork", artwork)
    monkeypatch.setattr(OpenAIService, "revise_artwork", edit)
    monkeypatch.setattr(OpenAIService, "visual_qa", visual)
    monkeypatch.setattr(OpenAIService, "revise_brief", rewrite)

    activities = {
        "research_run": research_activity,
        "screen_and_select_run": screen_activity,
        "generate_package_run": generate_activity,
        "rewrite_failed_brief": rewrite_failed_brief_activity,
        "automatic_approval_signal": automatic_approval_activity,
        "record_approval": approval_activity,
        "revalidate_approval": revalidate_approval_activity,
        "publish_channel": publish_activity,
        "finish_publishing": finish_activity,
        "mark_failed": mark_failed_activity,
    }
    activity_calls: list[str] = []

    async def execute(name: str, argument: Any, **kwargs: Any) -> Any:
        activity_calls.append(name)
        return await activities[name](argument)

    async def wait_condition(predicate: Any) -> None:
        assert predicate(), "A recovered daily run must never wait for a manual signal"

    monkeypatch.setattr("merch.temporal.workflow.execute_activity", execute)
    monkeypatch.setattr("merch.temporal.workflow.wait_condition", wait_condition)
    monkeypatch.setattr("merch.temporal.workflow.patched", lambda _: True)
    assert await MerchWorkflow().run(value) == RunStatus.PUBLISHED.value

    counts = Counter(activity_calls)
    assert counts["research_run"] == counts["screen_and_select_run"] == 1
    assert counts["generate_package_run"] == 3
    assert counts["rewrite_failed_brief"] == 2 < get_settings().max_brief_rewrites
    assert counts["record_approval"] == counts["publish_channel"] == counts["finish_publishing"] == 1
    assert [context.strategy for context in contexts] == ["targeted", "structural_simplification"]
    assert [item.version for item in contexts[-1].history] == [1, 2]
    assert generated_versions == [1, 2, 3]
    assert edited_versions == [2]
    assert qa_calls == [(1, 1), (2, 1), (2, 2), (3, 1)]
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.PUBLISHED.value and run.version == 3
        assert run.qa_report["passed"]
        assert {key: run.creative_brief[key] for key in immutable_fields} == original_identity
        assert set(run.creative_brief["shirt_colors"]) == set(COLORS)
        published_variants = run.publication_template_snapshot["variants"]
        assert len(published_variants) == 98
        assert {item["color"] for item in published_variants if item["enabled"]} == set(COLORS)
        assert len(run.price_quotes) == 98
        assert [(item.actor, item.version) for item in run.approvals] == [("system", 3)]
        assert [(item.channel, item.status) for item in run.publishes] == [
            (Channel.ETSY.value, PublishStatus.DRY_RUN.value),
        ]
        stages = Counter(call["stage"] for call in run.provider_calls)
        assert stages["research"] == stages["selection"] == stages["creative"] == 1
        sources = [item for item in run.artifacts if item.kind.startswith("source-v")]
        assert {item.kind for item in sources} == {"source-v1", "source-v2", "source-v3"}
        assert len({item.sha256 for item in sources if item.revision == 1}) == 3


@pytest.mark.temporal
@pytest.mark.skipif(os.getenv("RUN_TEMPORAL_TESTS") != "1", reason="opt-in Temporal test")
@pytest.mark.parametrize("successful", [True, False])
@pytest.mark.asyncio
async def test_time_skipping_daily_workflow_recovers_or_stops_at_rewrite_limit(
    successful: bool,
) -> None:
    calls: Counter[str] = Counter()

    @activity.defn(name="research_run")
    async def research(_: str) -> None:
        calls["research"] += 1

    @activity.defn(name="screen_and_select_run")
    async def select(_: str) -> bool:
        calls["selection"] += 1
        return True

    @activity.defn(name="generate_package_run")
    async def generate(argument: dict[str, object]) -> bool:
        calls["generation"] += 1
        assert argument["regenerate"] is False
        assert argument["preserve_brief"] is True
        return successful and calls["generation"] == 3

    @activity.defn(name="rewrite_failed_brief")
    async def rewrite(_: str) -> str:
        calls["rewrite"] += 1
        # The real activity owns the bounded policy; this checks workflow termination.
        return (
            RunStatus.AWAITING_BRIEF_REVISION.value
            if calls["rewrite"] == 8 else RunStatus.PENDING.value
        )

    @activity.defn(name="automatic_approval_signal")
    async def automatic(_: str) -> ApprovalSignal:
        assert successful and calls["generation"] == 3
        return ApprovalSignal(
            channels=[Channel.ETSY], expected_version=3, actor="system", ip_attested=False,
        )

    @activity.defn(name="record_approval")
    async def approve(argument: dict[str, object]) -> bool:
        calls["approval"] += 1
        signal = ApprovalSignal.model_validate(argument["signal"])
        assert signal.actor == "system" and signal.expected_version == 3
        return True

    @activity.defn(name="revalidate_approval")
    async def revalidate(_: str) -> bool:
        return True

    @activity.defn(name="publish_channel")
    async def publish(argument: PublishInput) -> PublishStatus:
        calls["publish"] += 1
        assert argument.channel == Channel.ETSY
        return PublishStatus.DRY_RUN

    @activity.defn(name="finish_publishing")
    async def finish(argument: dict[str, object]) -> None:
        calls["finish"] += 1
        assert argument["results"] == [PublishStatus.DRY_RUN.value]

    @activity.defn(name="mark_failed")
    async def failed(_: dict[str, str]) -> None:
        calls["failed"] += 1

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
    ) as environment:
        async with Worker(
            environment.client, task_queue="automatic-artwork-recovery",
            workflows=[MerchWorkflow], activities=[
                research, select, generate, rewrite, automatic, approve, revalidate,
                publish, finish, failed,
            ],
        ):
            value = RunInput(
                run_id=uuid4(), scheduled_for=await environment.get_current_time(), manual=False,
            )
            result = await environment.client.execute_workflow(
                MerchWorkflow.run, value, id=f"recovery-{value.run_id}",
                task_queue="automatic-artwork-recovery", execution_timeout=timedelta(minutes=10),
            )
    assert result == (
        RunStatus.PUBLISHED.value if successful else RunStatus.AWAITING_BRIEF_REVISION.value
    )
    assert calls["research"] == calls["selection"] == 1
    assert calls["generation"] == (3 if successful else 8)
    assert calls["rewrite"] == (2 if successful else 8)
    assert calls["approval"] == calls["publish"] == calls["finish"] == int(successful)
    assert calls["failed"] == 0
