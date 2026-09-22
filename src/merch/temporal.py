from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from temporalio import activity, workflow
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.common import (
    RetryPolicy,
    SearchAttributeKey,
    WorkflowIDReusePolicy,
)
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from merch.config import Settings, get_settings
from merch.schemas import (
    ApprovalSignal,
    CreativeBrief,
    ProductTemplate,
    PublishInput,
    PublishStatus,
    RunInput,
    RunStatus,
)

logger = logging.getLogger(__name__)

with workflow.unsafe.imports_passed_through():
    from merch.catalog_pipeline import (
        attempt_catalog_opportunity,
        finish_no_qualified_opportunity,
        research_catalog_run,
    )
    from merch.catalog_publisher import publish_catalog_run
    from merch.copy_refresh import apply_copy_refresh_batch, prepare_copy_refresh_batch
    from merch.pipeline import (
        ApprovalInvalid,
        automatic_approval_signal,
        create_run,
        finish_publishing,
        finish_retry,
        generate_package_run,
        publish_channel_run,
        record_approval,
        record_publish_failure,
        record_rejection,
        research_run,
        revalidate_approval_run,
        rewrite_failed_brief_run,
        screen_and_select_run,
        set_status,
        sync_analytics,
    )


ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=4,
    non_retryable_error_types=[
        "ApprovalInvalid",
        "ProviderConfigurationError",
        "OpenAINonRetryableError",
        "ValueError",
    ],
)

DAILY_DESIGN_SCHEDULE_ID = "merch-daily-design"
DAILY_ANALYTICS_SCHEDULE_ID = "merch-daily-analytics"
DAILY_SCHEDULE_POLICY = SchedulePolicy(
    overlap=ScheduleOverlapPolicy.BUFFER_ONE,
    catchup_window=timedelta(hours=24),
    pause_on_failure=False,
)
TEMPORAL_SCHEDULED_START_TIME = SearchAttributeKey.for_datetime(
    "TemporalScheduledStartTime"
)
SCHEDULE_REPAIR_RETRY_SECONDS = 60


async def temporal_client(settings: Settings | None = None) -> Client:
    settings = settings or get_settings()
    return await Client.connect(
        settings.temporal_target,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
        interceptors=[TracingInterceptor(always_create_workflow_spans=True)],
    )


@activity.defn(name="research_run")
async def research_activity(run_id: str) -> None:
    await research_run(run_id)


@activity.defn(name="research_catalog_run")
async def research_catalog_activity(run_id: str) -> int:
    return await research_catalog_run(run_id)


@activity.defn(name="attempt_catalog_opportunity")
async def attempt_catalog_activity(input: dict[str, object]) -> bool:
    rank = input["rank"]
    if not isinstance(rank, int):
        raise ValueError("catalog opportunity rank must be an integer")
    return await attempt_catalog_opportunity(str(input["run_id"]), rank)


@activity.defn(name="publish_catalog_run")
async def publish_catalog_activity(run_id: str) -> str:
    return await publish_catalog_run(run_id)


@activity.defn(name="finish_no_qualified_opportunity")
async def finish_no_qualified_activity(run_id: str) -> None:
    finish_no_qualified_opportunity(run_id)


@activity.defn(name="screen_and_select_run")
async def screen_activity(run_id: str) -> bool:
    return await screen_and_select_run(run_id)


@activity.defn(name="generate_package_run")
async def generate_activity(input: dict[str, object]) -> bool:
    return await generate_package_run(
        str(input["run_id"]),
        bool(input.get("regenerate", False)),
        preserve_brief=bool(input.get("preserve_brief", False)),
    )


@activity.defn(name="rewrite_failed_brief")
async def rewrite_failed_brief_activity(run_id: str) -> str:
    return await rewrite_failed_brief_run(run_id)


@activity.defn(name="record_approval")
async def approval_activity(input: dict[str, object]) -> bool:
    try:
        record_approval(str(input["run_id"]), ApprovalSignal.model_validate(input["signal"]))
        return True
    except ApprovalInvalid:
        return False


@activity.defn(name="automatic_approval_signal")
async def automatic_approval_activity(run_id: str) -> ApprovalSignal | None:
    return automatic_approval_signal(run_id)


@activity.defn(name="revalidate_approval")
async def revalidate_approval_activity(run_id: str) -> bool:
    return await revalidate_approval_run(run_id)


@activity.defn(name="record_rejection")
async def rejection_activity(input: dict[str, str]) -> None:
    record_rejection(input["run_id"], input.get("actor", "admin"))


@activity.defn(name="publish_channel")
async def publish_activity(input: PublishInput) -> PublishStatus:
    try:
        return await publish_channel_run(str(input.run_id), input.channel)
    except Exception as exc:
        record_publish_failure(str(input.run_id), input.channel, str(exc))
        raise


@activity.defn(name="finish_publishing")
async def finish_activity(input: dict[str, object]) -> None:
    raw_results = input["results"]
    if not isinstance(raw_results, list):
        raise ValueError("publish results must be a list")
    finish_publishing(
        str(input["run_id"]),
        [PublishStatus(str(value)) for value in raw_results],
    )


@activity.defn(name="finish_retry")
async def finish_retry_activity(run_id: str) -> None:
    finish_retry(run_id)


@activity.defn(name="mark_failed")
async def mark_failed_activity(input: dict[str, str]) -> None:
    set_status(input["run_id"], RunStatus.FAILED, input["error"][:4000])


@activity.defn(name="mark_cancelled")
async def mark_cancelled_activity(run_id: str) -> None:
    set_status(run_id, RunStatus.CANCELLED, "Cancelled by operator")


@activity.defn(name="sync_analytics")
async def analytics_activity(_: str) -> dict[str, str]:
    return await sync_analytics()


async def _start_daily_workflow(
    scheduled: datetime,
    settings: Settings,
    client: Client | None = None,
) -> str:
    run_date = scheduled.astimezone(ZoneInfo(settings.schedule_timezone)).date().isoformat()
    run_id = uuid5(NAMESPACE_URL, f"merch-daily-{run_date}")
    value = RunInput(
        run_id=run_id,
        scheduled_for=scheduled,
        manual=False,
        pipeline_version=settings.pipeline_version,
        max_opportunity_attempts=settings.max_opportunity_attempts,
    )
    workflow_id = f"merch-daily-{run_date}"
    create_run(value, workflow_id)
    client = client or await temporal_client(settings)
    try:
        await client.start_workflow(
            (CatalogMerchWorkflow.run if value.pipeline_version == 2 else MerchWorkflow.run),
            value,
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        pass
    return workflow_id


@activity.defn(name="launch_daily_workflow")
async def launch_daily_activity(scheduled_iso: str) -> str:
    scheduled = datetime.fromisoformat(scheduled_iso)
    settings = get_settings()
    return await _start_daily_workflow(scheduled, settings)


@activity.defn(name="prepare_copy_refresh")
async def prepare_copy_refresh_activity(_: str) -> str:
    return await prepare_copy_refresh_batch()


@activity.defn(name="apply_copy_refresh")
async def apply_copy_refresh_activity(batch_id: str) -> str:
    return await apply_copy_refresh_batch(batch_id)


@workflow.defn
class CopyRefreshPrepareWorkflow:
    @workflow.run
    async def run(self, request_id: str) -> str:
        return cast(str, await workflow.execute_activity(
            "prepare_copy_refresh", request_id,
            start_to_close_timeout=timedelta(minutes=30), retry_policy=ACTIVITY_RETRY,
            result_type=str,
        ))


@workflow.defn
class CopyRefreshApplyWorkflow:
    @workflow.run
    async def run(self, batch_id: str) -> str:
        return cast(str, await workflow.execute_activity(
            "apply_copy_refresh", batch_id,
            start_to_close_timeout=timedelta(minutes=30), retry_policy=ACTIVITY_RETRY,
            result_type=str,
        ))


@workflow.defn
class MerchWorkflow:
    def __init__(self) -> None:
        self.approval: ApprovalSignal | None = None
        self.rejected_by: str | None = None
        self.regenerate_requested = False
        self.cancel_requested = False

    @workflow.signal
    async def approve(self, signal: ApprovalSignal) -> None:
        self.approval = signal

    @workflow.signal
    async def reject(self, actor: str = "admin") -> None:
        self.rejected_by = actor

    @workflow.signal
    async def regenerate(self) -> None:
        self.regenerate_requested = True

    @workflow.signal
    async def cancel(self) -> None:
        self.cancel_requested = True

    @workflow.query
    def state(self) -> dict[str, object]:
        return {
            "approval_pending": self.approval is None,
            "regenerate_requested": self.regenerate_requested,
            "rejected": self.rejected_by is not None,
            "cancel_requested": self.cancel_requested,
        }

    @workflow.run
    async def run(self, input: RunInput) -> str:
        run_id = str(input.run_id)
        try:
            if not input.reuse_research:
                await workflow.execute_activity(
                    "research_run",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=30),
                    retry_policy=ACTIVITY_RETRY,
                )
            safe = input.reuse_selection or await workflow.execute_activity(
                "screen_and_select_run",
                run_id,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=ACTIVITY_RETRY,
                result_type=bool,
            )
            if not safe:
                return RunStatus.NO_SAFE_CANDIDATE.value
            regenerate = input.reuse_selection
            while True:
                ready = await workflow.execute_activity(
                    "generate_package_run",
                    {
                        "run_id": run_id,
                        "regenerate": regenerate,
                        "preserve_brief": input.reuse_brief if regenerate else True,
                    },
                    start_to_close_timeout=timedelta(minutes=45),
                    retry_policy=ACTIVITY_RETRY,
                    result_type=bool,
                )
                if ready:
                    break
                outcome = await workflow.execute_activity(
                    "rewrite_failed_brief",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=ACTIVITY_RETRY,
                    result_type=str,
                )
                if outcome != RunStatus.PENDING.value:
                    return cast(str, outcome)
                regenerate = False
            automatic = await workflow.execute_activity(
                "automatic_approval_signal",
                run_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=ACTIVITY_RETRY,
                result_type=cast(type, ApprovalSignal | None),
            )
            if automatic and self.approval is None:
                self.approval = automatic
            automatic_reapproval_attempts = 0
            while True:
                await workflow.wait_condition(
                    lambda: bool(
                        self.approval
                        or self.rejected_by
                        or self.regenerate_requested
                        or self.cancel_requested
                    )
                )
                if self.cancel_requested:
                    await workflow.execute_activity(
                        "mark_cancelled",
                        run_id,
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                    return RunStatus.CANCELLED.value
                if self.rejected_by:
                    await workflow.execute_activity(
                        "record_rejection",
                        {"run_id": run_id, "actor": self.rejected_by},
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                    return RunStatus.REJECTED.value
                if self.regenerate_requested:
                    self.regenerate_requested = False
                    ready = await workflow.execute_activity(
                        "generate_package_run",
                        {"run_id": run_id, "regenerate": True},
                        start_to_close_timeout=timedelta(minutes=45),
                        retry_policy=ACTIVITY_RETRY,
                        result_type=bool,
                    )
                    if not ready and workflow.patched("artwork-effects-regeneration-recovery-v1"):
                        while not ready:
                            outcome = await workflow.execute_activity(
                                "rewrite_failed_brief",
                                run_id,
                                start_to_close_timeout=timedelta(minutes=5),
                                retry_policy=ACTIVITY_RETRY,
                                result_type=str,
                            )
                            if outcome != RunStatus.PENDING.value:
                                return cast(str, outcome)
                            ready = await workflow.execute_activity(
                                "generate_package_run",
                                {"run_id": run_id, "regenerate": False, "preserve_brief": True},
                                start_to_close_timeout=timedelta(minutes=45),
                                retry_policy=ACTIVITY_RETRY,
                                result_type=bool,
                            )
                    if ready:
                        automatic = await workflow.execute_activity(
                            "automatic_approval_signal",
                            run_id,
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=ACTIVITY_RETRY,
                            result_type=cast(type, ApprovalSignal | None),
                        )
                        if automatic and self.approval is None:
                            self.approval = automatic
                    continue
                if self.approval:
                    approval = self.approval
                    self.approval = None
                    accepted = await workflow.execute_activity(
                        "record_approval",
                        {"run_id": run_id, "signal": approval.model_dump(mode="json")},
                        start_to_close_timeout=timedelta(seconds=30),
                        result_type=bool,
                    )
                    if not accepted:
                        if approval.actor == "system":
                            raise ApprovalInvalid("automatic release failed package validation")
                        continue
                    still_valid = await workflow.execute_activity(
                        "revalidate_approval",
                        run_id,
                        start_to_close_timeout=timedelta(minutes=5),
                        retry_policy=ACTIVITY_RETRY,
                        result_type=bool,
                    )
                    if not still_valid:
                        if approval.actor == "system":
                            automatic_reapproval_attempts += 1
                            if automatic_reapproval_attempts > 1:
                                raise ApprovalInvalid(
                                    "automatic release stopped after repeated catalog changes"
                                )
                            self.approval = await workflow.execute_activity(
                                "automatic_approval_signal",
                                run_id,
                                start_to_close_timeout=timedelta(seconds=30),
                                retry_policy=ACTIVITY_RETRY,
                                result_type=cast(type, ApprovalSignal | None),
                            )
                            if self.approval is None:
                                raise ApprovalInvalid("automatic release requires a new package")
                        continue
                    tasks = [
                        workflow.execute_activity(
                            "publish_channel",
                            PublishInput(run_id=input.run_id, channel=channel),
                            start_to_close_timeout=timedelta(minutes=20),
                            retry_policy=ACTIVITY_RETRY,
                            result_type=PublishStatus,
                        )
                        for channel in approval.channels
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    statuses = [
                        item if isinstance(item, PublishStatus) else PublishStatus.FAILED
                        for item in results
                    ]
                    await workflow.execute_activity(
                        "finish_publishing",
                        {"run_id": run_id, "results": [item.value for item in statuses]},
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                    if all(
                        item in {PublishStatus.SUCCEEDED, PublishStatus.DRY_RUN}
                        for item in statuses
                    ):
                        return RunStatus.PUBLISHED.value
                    if not any(
                        item in {PublishStatus.SUCCEEDED, PublishStatus.DRY_RUN}
                        for item in statuses
                    ) and PublishStatus.RECONCILIATION_REQUIRED in statuses:
                        return RunStatus.VERIFICATION_REQUIRED.value
                    return RunStatus.PARTIALLY_PUBLISHED.value
        except Exception as exc:
            reason = str(exc.__cause__) if exc.__cause__ else str(exc)
            await workflow.execute_activity(
                "mark_failed",
                {"run_id": run_id, "error": reason},
                start_to_close_timeout=timedelta(seconds=30),
            )
            raise


@workflow.defn
class CatalogMerchWorkflow:
    """Research 25 opportunities and release the first of three that passes all gates."""

    @workflow.run
    async def run(self, input: RunInput) -> str:
        run_id = str(input.run_id)
        try:
            count = cast(
                int,
                await workflow.execute_activity(
                    "research_catalog_run",
                    run_id,
                    start_to_close_timeout=timedelta(hours=4),
                    retry_policy=ACTIVITY_RETRY,
                    result_type=int,
                ),
            )
            if count == 0:
                return RunStatus.NO_QUALIFIED_OPPORTUNITY.value
            attempts = min(count, input.max_opportunity_attempts)
            for rank in range(1, attempts + 1):
                ready = await workflow.execute_activity(
                    "attempt_catalog_opportunity",
                    {"run_id": run_id, "rank": rank},
                    start_to_close_timeout=timedelta(hours=2),
                    retry_policy=ACTIVITY_RETRY,
                    result_type=bool,
                )
                if not ready:
                    continue
                outcome = cast(
                    str,
                    await workflow.execute_activity(
                        "publish_catalog_run",
                        run_id,
                        start_to_close_timeout=timedelta(minutes=45),
                        retry_policy=ACTIVITY_RETRY,
                        result_type=str,
                    ),
                )
                if outcome == "hard_gate_failed":
                    continue
                if outcome == PublishStatus.RECONCILIATION_REQUIRED.value:
                    return RunStatus.VERIFICATION_REQUIRED.value
                if outcome in {
                    PublishStatus.SUCCEEDED.value, PublishStatus.DRY_RUN.value,
                }:
                    return RunStatus.PUBLISHED.value
                raise RuntimeError(f"unexpected catalog publication outcome: {outcome}")
            await workflow.execute_activity(
                "finish_no_qualified_opportunity",
                run_id,
                start_to_close_timeout=timedelta(seconds=30),
            )
            return RunStatus.NO_QUALIFIED_OPPORTUNITY.value
        except Exception as exc:
            reason = str(exc.__cause__) if exc.__cause__ else str(exc)
            await workflow.execute_activity(
                "mark_failed",
                {"run_id": run_id, "error": reason},
                start_to_close_timeout=timedelta(seconds=30),
            )
            raise


@workflow.defn
class RetryPublishWorkflow:
    @workflow.run
    async def run(self, input: PublishInput) -> str:
        status = cast(
            PublishStatus,
            await workflow.execute_activity(
                "publish_channel",
                input,
                start_to_close_timeout=timedelta(minutes=20),
                retry_policy=ACTIVITY_RETRY,
                result_type=PublishStatus,
            ),
        )
        await workflow.execute_activity(
            "finish_retry", str(input.run_id), start_to_close_timeout=timedelta(seconds=30)
        )
        return status.value


@workflow.defn
class AnalyticsWorkflow:
    @workflow.run
    async def run(self) -> dict[str, str]:
        return cast(
            dict[str, str],
            await workflow.execute_activity(
                "sync_analytics",
                "daily",
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=ACTIVITY_RETRY,
                result_type=dict[str, str],
            ),
        )


@workflow.defn
class DailyLauncherWorkflow:
    @workflow.run
    async def run(self) -> str:
        scheduled = workflow.now()
        if workflow.patched("daily-launcher-nominal-schedule-time-v1"):
            scheduled = workflow.info().typed_search_attributes.get(
                TEMPORAL_SCHEDULED_START_TIME
            ) or scheduled
        return cast(
            str,
            await workflow.execute_activity(
                "launch_daily_workflow",
                scheduled.isoformat(),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=ACTIVITY_RETRY,
                result_type=str,
            ),
        )


async def start_manual_run(settings: Settings | None = None) -> RunInput:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    value = RunInput(
        run_id=uuid4(),
        scheduled_for=now,
        manual=True,
        pipeline_version=settings.pipeline_version,
        max_opportunity_attempts=settings.max_opportunity_attempts,
    )
    workflow_id = f"merch-manual-{value.run_id}"
    create_run(value, workflow_id)
    client = await temporal_client(settings)
    await client.start_workflow(
        (CatalogMerchWorkflow.run if value.pipeline_version == 2 else MerchWorkflow.run),
        value,
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )
    return value


async def resume_researched_run(run_id: str, settings: Settings | None = None) -> RunInput:
    """Resume a failed run after research without repeating its paid research call."""
    from merch.database import session_scope
    from merch.repository import RunRepository, lock_active_template

    settings = settings or get_settings()
    with session_scope() as session:
        lock_active_template(session)
        repository = RunRepository(session)
        record = repository.get(run_id, full=True)
        if (
            record.status != RunStatus.FAILED.value
            or not record.research_report
            or record.artifacts
            or record.approvals
            or record.publishes
        ):
            raise ValueError(
                "Only a failed run with completed research and no review package can resume"
            )
        previous_selected = (record.selected_concept or {}).get("concept_name")
        record.selection = None
        record.selected_concept = None
        record.creative_brief = None
        record.typography_spec = None
        record.qa_report = None
        record.listings = None
        record.listing_generation_state = None
        record.price_quotes = None
        for concept in record.concepts:
            concept.selected = False
            concept.eligible = True
            concept.rejection_reason = None
            concept.weighted_score = None
        value = RunInput(
            run_id=record.id,
            scheduled_for=record.scheduled_for,
            manual=record.manual,
            reuse_research=True,
        )
        workflow_id = f"merch-resume-{record.id}-{uuid4().hex[:8]}"
        record.workflow_id = workflow_id
        repository.status(run_id, RunStatus.PENDING)
        repository.audit(
            run_id,
            "operator",
            "run.research_resumed",
            {"workflow_id": workflow_id, "previous_selected": previous_selected},
        )
    client = await temporal_client(settings)
    await client.start_workflow(
        MerchWorkflow.run,
        value,
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )
    return value


async def retry_failed_artwork_run(
    run_id: str,
    settings: Settings | None = None,
    revised_brief: CreativeBrief | None = None,
) -> RunInput:
    """Retry artwork after a QA failure without repeating research or selection."""
    from merch.database import session_scope
    from merch.domain.ip_screening import ip_report_eligible
    from merch.repository import ConfigurationRepository, RunRepository, lock_active_template
    from merch.schemas import CandidateConcept, IPScreeningReport

    settings = settings or get_settings()
    with session_scope() as session:
        lock_active_template(session)
        repository = RunRepository(session)
        record = repository.get(run_id, full=True)
        ip_eligible = True
        if settings.ip_check_enabled:
            packet = record.ip_report or {}
            try:
                report = IPScreeningReport.model_validate(
                    {key: value for key, value in packet.items() if key != "candidate_reports"}
                )
            except ValueError as exc:
                raise ValueError("Run has no valid IP screening report") from exc
            ip_eligible = ip_report_eligible(report, settings.ip_risk_threshold)
        if (
            record.status not in {
                RunStatus.FAILED.value,
                RunStatus.AWAITING_BRIEF_REVISION.value,
            }
            or not record.selected_concept
            or not ip_eligible
            or not any(item.kind.startswith("production-v") for item in record.artifacts)
            or record.qa_report is not None
            or record.approvals
            or record.publishes
        ):
            raise ValueError("Only a failed-artwork run without approval can retry")
        if record.status == RunStatus.AWAITING_BRIEF_REVISION.value and revised_brief is None:
            raise ValueError("A revised creative brief is required after a repeated visual defect")
        if revised_brief is not None:
            if revised_brief.concept_name != record.selected_concept["concept_name"]:
                raise ValueError("Revised brief must retain the selected concept")
            selected = CandidateConcept.model_validate(record.selected_concept)
            previous_brief = (
                CreativeBrief.model_validate(record.creative_brief) if record.creative_brief else None
            )
            if selected.strategy is not None:
                if revised_brief.slogan != selected.slogan_if_any:
                    raise ValueError("Revised brief must preserve the selected concept's exact printed slogan")
                revised_brief = revised_brief.model_copy(update={"strategy": selected.strategy})
                if previous_brief is not None:
                    previous_brief = previous_brief.model_copy(update={"strategy": selected.strategy})
            if previous_brief is not None and revised_brief == previous_brief:
                raise ValueError("Revise the creative brief before retrying artwork")
            template = (
                ProductTemplate.model_validate(record.template_snapshot)
                if record.template_snapshot else ConfigurationRepository(session).get_template()
            )
            allowed_colors = {item.color for item in template.variants if item.enabled}
            if not set(revised_brief.shirt_colors).issubset(allowed_colors):
                raise ValueError("Revised brief contains colors outside the enabled template")
            record.creative_brief = revised_brief.model_dump(mode="json")
        value = RunInput(
            run_id=record.id,
            scheduled_for=record.scheduled_for,
            manual=record.manual,
            reuse_research=True,
            reuse_selection=True,
            reuse_brief=revised_brief is not None,
        )
        workflow_id = f"merch-artwork-retry-{record.id}-{uuid4().hex[:8]}"
        record.workflow_id = workflow_id
        repository.status(run_id, RunStatus.PENDING)
        repository.audit(
            run_id,
            "operator",
            "run.artwork_retried",
            {
                "workflow_id": workflow_id,
                "previous_artifacts": len(record.artifacts),
                "brief_revised": revised_brief is not None,
            },
        )
    client = await temporal_client(settings)
    await client.start_workflow(
        MerchWorkflow.run,
        value,
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )
    return value


async def reconcile_schedules(
    settings: Settings | None = None,
    client: Client | None = None,
) -> None:
    settings = settings or get_settings()
    client = client or await temporal_client(settings)
    schedules = {
        DAILY_DESIGN_SCHEDULE_ID: Schedule(
            action=ScheduleActionStartWorkflow(
                DailyLauncherWorkflow.run,
                id="merch-daily-launcher",
                task_queue=settings.temporal_task_queue,
            ),
            spec=ScheduleSpec(
                cron_expressions=[f"{settings.schedule_minute} {settings.workflow_hour} * * *"],
                time_zone_name=settings.schedule_timezone,
            ),
            policy=DAILY_SCHEDULE_POLICY,
            state=ScheduleState(note="Daily merch product development"),
        ),
        DAILY_ANALYTICS_SCHEDULE_ID: Schedule(
            action=ScheduleActionStartWorkflow(
                AnalyticsWorkflow.run,
                id="merch-analytics",
                task_queue=settings.temporal_task_queue,
            ),
            spec=ScheduleSpec(
                cron_expressions=[f"{settings.schedule_minute} {settings.analytics_hour} * * *"],
                time_zone_name=settings.schedule_timezone,
            ),
            policy=DAILY_SCHEDULE_POLICY,
            state=ScheduleState(note="Daily marketplace analytics synchronization"),
        ),
    }
    for schedule_id, schedule in schedules.items():
        try:
            handle = client.get_schedule_handle(schedule_id)
            await handle.describe()

            def update_schedule(
                input: ScheduleUpdateInput, desired: Schedule = schedule
            ) -> ScheduleUpdate:
                current_state = input.description.schedule.state
                note = current_state.note if current_state.paused else desired.state.note
                return ScheduleUpdate(replace(desired, state=replace(current_state, note=note)))

            await handle.update(update_schedule)
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            try:
                await client.create_schedule(schedule_id, schedule)
            except RPCError as create_exc:
                if create_exc.status != RPCStatusCode.ALREADY_EXISTS:
                    raise


def _today_scheduled_time(now: datetime, settings: Settings) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Current time must include a timezone")
    local_now = now.astimezone(ZoneInfo(settings.schedule_timezone))
    return local_now.replace(
        hour=settings.workflow_hour,
        minute=settings.schedule_minute,
        second=0,
        microsecond=0,
    ).astimezone(UTC)


async def start_due_daily_run(
    settings: Settings | None = None,
    client: Client | None = None,
    now: datetime | None = None,
) -> str | None:
    """Start today's missed design run after startup unless its schedule is paused."""
    settings = settings or get_settings()
    client = client or await temporal_client(settings)
    description = await client.get_schedule_handle(DAILY_DESIGN_SCHEDULE_ID).describe()
    if description.schedule.state.paused:
        return None
    current = now or datetime.now(UTC)
    scheduled = _today_scheduled_time(current, settings)
    if current.astimezone(UTC) < scheduled:
        return None
    return await _start_daily_workflow(scheduled, settings, client)


async def _repair_schedules_until_ready(settings: Settings, client: Client) -> None:
    """Repair scheduling without ever preventing the activity worker from polling."""
    while True:
        try:
            await reconcile_schedules(settings, client)
            await start_due_daily_run(settings, client)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Daily schedule startup repair failed; retrying in 60 seconds")
            await asyncio.sleep(SCHEDULE_REPAIR_RETRY_SECONDS)


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    client = await temporal_client(settings)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[MerchWorkflow, CatalogMerchWorkflow, RetryPublishWorkflow, AnalyticsWorkflow, DailyLauncherWorkflow,
                   CopyRefreshPrepareWorkflow, CopyRefreshApplyWorkflow],
        activities=[
            research_activity,
            research_catalog_activity,
            attempt_catalog_activity,
            publish_catalog_activity,
            finish_no_qualified_activity,
            screen_activity,
            generate_activity,
            rewrite_failed_brief_activity,
            automatic_approval_activity,
            approval_activity,
            revalidate_approval_activity,
            rejection_activity,
            publish_activity,
            finish_activity,
            finish_retry_activity,
            mark_failed_activity,
            mark_cancelled_activity,
            analytics_activity,
            launch_daily_activity,
            prepare_copy_refresh_activity,
            apply_copy_refresh_activity,
        ],
    )
    schedule_repair = asyncio.create_task(
        _repair_schedules_until_ready(settings, client),
        name="daily-schedule-startup-repair",
    )
    try:
        # Give fast local reconciliation a head start, but never await its network
        # path before the worker begins polling already-queued activities.
        await asyncio.sleep(0)
        await worker.run()
    finally:
        schedule_repair.cancel()
        with suppress(asyncio.CancelledError):
            await schedule_repair
