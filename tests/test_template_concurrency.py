from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.models import Base, ProductTemplateRecord
from merch.repository import ConfigurationRepository, RunRepository, lock_active_template
from merch.schemas import CandidateConcept, CreativeBrief, ProductTemplate, RunInput, RunStatus
from merch.services.openai_service import OpenAIService
from merch.temporal import resume_researched_run, retry_failed_artwork_run


@pytest.fixture
async def configured_run(
    isolated_app: Path,
) -> tuple[RunInput, ProductTemplate, CandidateConcept, CreativeBrief]:
    Base.metadata.create_all(get_engine())
    template = fixture_product_template()
    service = OpenAIService(get_settings())
    candidate = (await service.research(date.today(), "none")).value.candidates[0]
    brief = (await service.creative(candidate, {})).value
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    return value, template, candidate, brief


@pytest.mark.parametrize("operation", ["create", "resume_research", "retry_artwork"])
def test_catalog_activation_waits_for_run_start_and_then_rejects_it(
    configured_run: tuple[RunInput, ProductTemplate, CandidateConcept, CreativeBrief],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Exercise real SQLite transactions, including its lack of FOR UPDATE."""
    value, original, candidate, brief = configured_run
    run_id = str(value.run_id)
    with session_scope() as session:
        original_updated_at = ConfigurationRepository(session).get_template_record().updated_at
        if operation != "create":
            repository = RunRepository(session)
            record = repository.create(value, f"failed-{operation}-{run_id}")
            record.status = RunStatus.FAILED.value
            record.research_report = {"market_summary": "Saved paid research"}
            record.template_snapshot = original.model_dump(mode="json")
            if operation == "retry_artwork":
                record.selected_concept = candidate.model_dump(mode="json")
                record.creative_brief = brief.model_dump(mode="json")
                repository.add_artifact(
                    run_id, kind="production-v1", revision=1,
                    object_key=f"failed-{run_id}", sha256="0" * 64,
                    width=400, height=500, metadata={},
                )

    class Client:
        async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
            pass

    async def client(settings: Any) -> Client:
        return Client()

    monkeypatch.setattr("merch.temporal.temporal_client", client)
    run_has_lock = threading.Event()
    release_run = threading.Event()
    activation_attempted = threading.Event()
    activation_finished = threading.Event()
    role = threading.local()

    def coordinated_lock(session: Session) -> ProductTemplateRecord | None:
        if getattr(role, "name", None) == "activation":
            activation_attempted.set()
        template = lock_active_template(session)
        if getattr(role, "name", None) == "run":
            run_has_lock.set()
            assert release_run.wait(timeout=5), "Test did not release the run transaction"
        return template

    monkeypatch.setattr("merch.repository.lock_active_template", coordinated_lock)

    def start_run() -> None:
        role.name = "run"
        if operation == "create":
            with session_scope() as session:
                RunRepository(session).create(value, f"new-{run_id}")
        elif operation == "resume_research":
            asyncio.run(resume_researched_run(run_id, get_settings()))
        else:
            asyncio.run(retry_failed_artwork_run(run_id, get_settings()))

    def activate() -> None:
        role.name = "activation"
        try:
            with session_scope() as session:
                ConfigurationRepository(session).activate_template(
                    original.model_copy(update={"name": "Replacement garment"}), expected_version=1,
                )
        finally:
            activation_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        run_future = pool.submit(start_run)
        try:
            assert run_has_lock.wait(timeout=5)
            activation_future = pool.submit(activate)
            assert activation_attempted.wait(timeout=5)
            assert not activation_finished.wait(timeout=0.1), "Activation skipped the transaction lock"
        finally:
            release_run.set()
        run_future.result(timeout=5)
        with pytest.raises(ValueError, match="Resolve active run"):
            activation_future.result(timeout=5)

    with session_scope() as session:
        current = ConfigurationRepository(session).get_template_record()
        assert current.version == 1
        assert current.updated_at == original_updated_at
        assert ProductTemplate.model_validate(current.data) == original
        run = RunRepository(session).get(run_id)
        assert run.status == RunStatus.PENDING.value
        if operation != "create":
            assert run.template_snapshot == original.model_dump(mode="json")
