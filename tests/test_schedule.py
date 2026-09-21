from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from temporalio.client import ScheduleOverlapPolicy, ScheduleState
from temporalio.common import (
    SearchAttributePair,
    TypedSearchAttributes,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from merch.config import Settings
from merch.temporal import (
    DAILY_DESIGN_SCHEDULE_ID,
    TEMPORAL_SCHEDULED_START_TIME,
    DailyLauncherWorkflow,
    _start_daily_workflow,
    _today_scheduled_time,
    reconcile_schedules,
    run_worker,
    start_due_daily_run,
)


@pytest.mark.asyncio
async def test_both_daily_schedules_run_at_930_and_keep_pause_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeHandle:
        def __init__(self, state: ScheduleState) -> None:
            self.state = state
            self.schedule = None

        async def describe(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(schedule=SimpleNamespace(state=self.state))

        async def update(self, callback):  # type: ignore[no-untyped-def]
            description = await self.describe()
            self.schedule = callback(SimpleNamespace(description=description)).schedule

    handles = {
        "merch-daily-design": FakeHandle(
            ScheduleState(paused=True, note="Billing pause")
        ),
        "merch-daily-analytics": FakeHandle(ScheduleState(paused=False)),
    }

    class FakeClient:
        def get_schedule_handle(self, schedule_id):  # type: ignore[no-untyped-def]
            return handles[schedule_id]

    async def fake_temporal_client(settings):  # type: ignore[no-untyped-def]
        return FakeClient()

    monkeypatch.setattr("merch.temporal.temporal_client", fake_temporal_client)
    await reconcile_schedules(Settings(_env_file=None))

    for handle in handles.values():
        assert handle.schedule is not None
        assert handle.schedule.spec.cron_expressions == ["30 9 * * *"]
        assert handle.schedule.spec.time_zone_name == "America/New_York"
        assert handle.schedule.policy.catchup_window == timedelta(hours=24)
        assert handle.schedule.policy.overlap == ScheduleOverlapPolicy.BUFFER_ONE
        assert not handle.schedule.policy.pause_on_failure
    assert handles["merch-daily-design"].schedule.state.paused
    assert handles["merch-daily-design"].schedule.state.note == "Billing pause"
    assert not handles["merch-daily-analytics"].schedule.state.paused


@pytest.mark.asyncio
async def test_schedule_creation_uses_explicit_catchup_policy(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    created = {}

    class MissingHandle:
        async def describe(self):  # type: ignore[no-untyped-def]
            raise RPCError("missing", RPCStatusCode.NOT_FOUND, b"")

    class FakeClient:
        def get_schedule_handle(self, schedule_id):  # type: ignore[no-untyped-def]
            return MissingHandle()

        async def create_schedule(self, schedule_id, schedule):  # type: ignore[no-untyped-def]
            created[schedule_id] = schedule

    async def fake_temporal_client(settings):  # type: ignore[no-untyped-def]
        return FakeClient()

    monkeypatch.setattr("merch.temporal.temporal_client", fake_temporal_client)
    await reconcile_schedules(Settings(_env_file=None))

    assert set(created) == {"merch-daily-design", "merch-daily-analytics"}
    for schedule in created.values():
        assert schedule.policy.catchup_window == timedelta(hours=24)
        assert schedule.policy.overlap == ScheduleOverlapPolicy.BUFFER_ONE
        assert not schedule.policy.pause_on_failure


@pytest.mark.asyncio
async def test_launcher_uses_nominal_temporal_schedule_time(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    nominal = datetime(2026, 9, 21, 13, 30, tzinfo=UTC)
    actual = datetime(2026, 9, 21, 14, 48, tzinfo=UTC)
    attributes = TypedSearchAttributes(
        [SearchAttributePair(TEMPORAL_SCHEDULED_START_TIME, nominal)]
    )
    captured = {}

    async def execute_activity(name, arg, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(name=name, arg=arg, kwargs=kwargs)
        return "merch-daily-2026-09-21"

    monkeypatch.setattr(
        "merch.temporal.workflow.info",
        lambda: SimpleNamespace(typed_search_attributes=attributes),
    )
    monkeypatch.setattr("merch.temporal.workflow.patched", lambda _: True)
    monkeypatch.setattr("merch.temporal.workflow.now", lambda: actual)
    monkeypatch.setattr("merch.temporal.workflow.execute_activity", execute_activity)

    result = await DailyLauncherWorkflow().run()

    assert result == "merch-daily-2026-09-21"
    assert captured["name"] == "launch_daily_workflow"
    assert captured["arg"] == nominal.isoformat()


@pytest.mark.asyncio
async def test_start_due_daily_run_uses_nominal_time_and_respects_pause(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None)
    start = AsyncMock(return_value="merch-daily-2026-09-21")
    monkeypatch.setattr("merch.temporal._start_daily_workflow", start)

    class FakeClient:
        def __init__(self, paused: bool) -> None:
            self.paused = paused

        def get_schedule_handle(self, schedule_id):  # type: ignore[no-untyped-def]
            assert schedule_id == DAILY_DESIGN_SCHEDULE_ID
            state = ScheduleState(paused=self.paused)

            async def describe():  # type: ignore[no-untyped-def]
                return SimpleNamespace(schedule=SimpleNamespace(state=state))

            return SimpleNamespace(describe=describe)

    now = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    result = await start_due_daily_run(settings, FakeClient(False), now)

    assert result == "merch-daily-2026-09-21"
    start.assert_awaited_once_with(
        datetime(2026, 9, 21, 13, 30, tzinfo=UTC), settings, ANY
    )

    start.reset_mock()
    assert await start_due_daily_run(settings, FakeClient(True), now) is None
    start.assert_not_awaited()

    assert await start_due_daily_run(
        settings,
        FakeClient(False),
        datetime(2026, 9, 21, 13, 0, tzinfo=UTC),
    ) is None
    start.assert_not_awaited()


def test_today_scheduled_time_tracks_new_york_dst() -> None:
    settings = Settings(_env_file=None)

    before_dst = _today_scheduled_time(datetime(2026, 3, 7, 15, tzinfo=UTC), settings)
    after_dst = _today_scheduled_time(datetime(2026, 3, 8, 15, tzinfo=UTC), settings)

    assert before_dst == datetime(2026, 3, 7, 14, 30, tzinfo=UTC)
    assert after_dst == datetime(2026, 3, 8, 13, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_daily_start_rejects_duplicate_workflow_ids(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None)
    start = Mock()

    class FakeClient:
        async def start_workflow(self, workflow, value, **kwargs):  # type: ignore[no-untyped-def]
            start(workflow, value, **kwargs)
            raise WorkflowAlreadyStartedError(kwargs["id"], "MerchWorkflow")

    create_run = Mock()
    monkeypatch.setattr("merch.temporal.create_run", create_run)
    result = await _start_daily_workflow(
        datetime(2026, 9, 21, 13, 30, tzinfo=UTC), settings, FakeClient()
    )

    assert result == "merch-daily-2026-09-21"
    assert start.call_args.kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE


@pytest.mark.asyncio
async def test_worker_reconciles_and_checks_due_run_before_polling(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None)
    client = object()
    calls = []

    async def fake_temporal_client(value):  # type: ignore[no-untyped-def]
        assert value is settings
        return client

    async def fake_reconcile(value, connected):  # type: ignore[no-untyped-def]
        assert value is settings
        assert connected is client
        calls.append("reconcile")

    async def fake_due(value, connected):  # type: ignore[no-untyped-def]
        assert value is settings
        assert connected is client
        calls.append("due")

    class FakeWorker:
        def __init__(self, connected, **kwargs):  # type: ignore[no-untyped-def]
            assert connected is client

        async def run(self) -> None:
            calls.append("run")

    monkeypatch.setattr("merch.temporal.temporal_client", fake_temporal_client)
    monkeypatch.setattr("merch.temporal.reconcile_schedules", fake_reconcile)
    monkeypatch.setattr("merch.temporal.start_due_daily_run", fake_due)
    monkeypatch.setattr("merch.temporal.Worker", FakeWorker)

    await run_worker(settings)

    assert calls == ["reconcile", "due", "run"]


@pytest.mark.asyncio
async def test_schedule_repair_failure_never_blocks_worker_polling(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None)
    client = object()
    recovered = asyncio.Event()
    calls: list[str] = []
    attempts = 0

    async def fake_temporal_client(value):  # type: ignore[no-untyped-def]
        return client

    async def fake_reconcile(value, connected):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        calls.append(f"reconcile-{attempts}")
        if attempts == 1:
            raise RuntimeError("schedule API unavailable")

    async def fake_due(value, connected):  # type: ignore[no-untyped-def]
        calls.append("due")
        recovered.set()

    class FakeWorker:
        def __init__(self, connected, **kwargs):  # type: ignore[no-untyped-def]
            assert connected is client

        async def run(self) -> None:
            calls.append("run")
            await asyncio.wait_for(recovered.wait(), timeout=1)

    monkeypatch.setattr("merch.temporal.temporal_client", fake_temporal_client)
    monkeypatch.setattr("merch.temporal.reconcile_schedules", fake_reconcile)
    monkeypatch.setattr("merch.temporal.start_due_daily_run", fake_due)
    monkeypatch.setattr("merch.temporal.Worker", FakeWorker)
    monkeypatch.setattr("merch.temporal.SCHEDULE_REPAIR_RETRY_SECONDS", 0)

    await run_worker(settings)

    assert attempts == 2
    assert calls.index("run") < calls.index("reconcile-2")
    assert calls[-1] == "due"
