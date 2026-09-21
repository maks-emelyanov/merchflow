from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.domain.performance import summarize_performance
from merch.models import Base, ConceptRecord, DailyMetricRecord, ProductMappingRecord
from merch.pipeline import research_run, screen_and_select_run
from merch.repository import ConfigurationRepository, MetricsRepository, RunRepository
from merch.schemas import Channel, DailyPerformance, GarmentFacts, RunInput
from merch.services.openai_service import OpenAIService
from merch.web import create_app


def observation(source: str, **values):
    return {
        "metric_date": "2026-09-20", "channel": "etsy", "external_product_id": "listing-1",
        "concept_id": "concept-1", "source": source, **values,
    }


def test_performance_sources_do_not_double_count_or_invent_missing_metrics() -> None:
    rows = [
        observation("etsy_api", orders=2, gross_revenue_cents=4000,
                    completeness={"receipt_window_complete": True}),
        observation("etsy_csv", orders=3, gross_revenue_cents=5000, visits=12),
        observation("etsy_csv", metric_date="2026-09-19", visits=0),
    ]
    result = summarize_performance(rows, {"concept-1": {"name": "Night Readers"}})
    api = next(row for row in result["performance"] if row["source"] == "etsy_api")
    csv = next(row for row in result["performance"] if row["source"] == "etsy_csv")
    assert api["metrics"]["orders"] == 2
    assert api["metrics"]["gross_revenue_cents"] == 4000
    assert api["metrics"]["visits"] is None
    assert csv["metrics"]["orders"] is None
    assert csv["metrics"]["visits"] == 12
    assert csv["observations"]["visits"] == 2
    assert csv["concept"]["name"] == "Night Readers"


def test_legacy_etsy_partial_api_totals_do_not_override_csv() -> None:
    rows = [
        observation("etsy_api", orders=100, gross_revenue_cents=200000),
        observation("etsy_csv", orders=101, gross_revenue_cents=202000, visits=450),
    ]
    result = summarize_performance(rows, {})
    assert len(result["performance"]) == 1
    assert result["performance"][0]["source"] == "etsy_csv"
    assert result["performance"][0]["metrics"]["orders"] == 101
    assert result["performance"][0]["metrics"]["gross_revenue_cents"] == 202000


def test_amazon_uses_latest_window_for_each_product() -> None:
    rows = [observation(
        "amazon_sales_and_traffic", channel="amazon_us", metric_date=when,
        period_start="2026-06-22", period_end=when, orders=orders,
    ) for when, orders in [("2026-09-19", 9), ("2026-09-20", 10)]]
    data = summarize_performance(rows, {})["performance"]
    assert len(data) == 1
    assert data[0]["metrics"]["orders"] == 10
    assert data[0]["period_start"] == "2026-06-22"
    assert data[0]["period_end"] == "2026-09-20"
    assert data[0]["reporting"] == "latest_window_per_product"


@pytest.mark.asyncio
async def test_late_metric_mapping_updates_column_and_research_context(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    candidate = (await OpenAIService(get_settings()).research(date.today(), "none")).value.candidates[0]
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    metric = DailyPerformance(
        metric_date=date.today(), channel=Channel.ETSY, external_product_id="listing-1",
        orders=2, source="etsy_api",
    )
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.create(value, "mapped-run")
        concept = ConceptRecord(run_id=run.id, rank=1, data=candidate.model_dump(mode="json"))
        session.add(concept)
        session.flush()
        concept_id = concept.id
        MetricsRepository(session).upsert(metric)
    with session_scope() as session:
        session.add(ProductMappingRecord(
            run_id=str(value.run_id), concept_id=concept_id, channel="etsy",
            printify_product_id="product-1", marketplace_listing_id="listing-1", skus=[],
        ))
    with session_scope() as session:
        repo = MetricsRepository(session)
        repo.upsert(metric)
        session.flush()
        row = session.scalar(select(DailyMetricRecord))
        assert row.concept_id == concept_id
        assert row.data["concept_id"] == concept_id
        summary = json.loads(repo.summary())
        assert summary["performance"][0]["concept"]["strategy"]["micro_niche"] == candidate.strategy.micro_niche


@pytest.mark.asyncio
async def test_once_imported_csv_is_attributed_when_publication_mapping_arrives(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    candidate = (await OpenAIService(get_settings()).research(date.today(), "none")).value.candidates[0]
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.create(value, "csv-before-publication")
        concept = ConceptRecord(
            run_id=run.id, rank=1, selected=True, data=candidate.model_dump(mode="json"),
        )
        session.add(concept)
        session.flush()
        concept_id = concept.id
        MetricsRepository(session).upsert(DailyPerformance(
            metric_date=date.today(), channel=Channel.ETSY, external_product_id="listing-1",
            orders=2, source="etsy_csv",
        ))
    with session_scope() as session:
        assert json.loads(MetricsRepository(session).summary())["performance"][0]["concept_id"] is None
        RunRepository(session).save_product_mapping(
            str(value.run_id), "etsy", "printify-1", {"external": {"id": "listing-1"}},
        )
    with session_scope() as session:
        summary = json.loads(MetricsRepository(session).summary())
        observation = summary["performance"][0]
        assert observation["concept_id"] == concept_id
        assert observation["concept"]["strategy"]["micro_niche"] == candidate.strategy.micro_niche
        assert observation["metrics"]["orders"] == 2
        # Attribution needs no rewritten metric or repeated CSV upload.
        assert session.scalar(select(DailyMetricRecord)).concept_id is None


@pytest.mark.asyncio
async def test_research_receives_recent_identity_and_garment_then_reuses_checkpoint(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    now = datetime.now(UTC)
    value = RunInput(run_id=uuid4(), scheduled_for=now, manual=True)
    template = fixture_product_template().model_copy(update={
        "garment_facts": GarmentFacts(brand="Comfort Colors", model="1717", finish="garment-dyed", fit="relaxed"),
    })
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
        repo = RunRepository(session)
        for index in range(35):
            previous = repo.create(RunInput(run_id=uuid4(), scheduled_for=now, manual=True), f"past-{index}")
            previous.created_at = now - timedelta(days=index + 1)
            previous.status = "failed" if index == 0 else "published"
            previous.selected_concept = {"concept_name": f"Past concept {index}", "slogan_if_any": f"PAST {index}"}
        old = repo.create(RunInput(run_id=uuid4(), scheduled_for=now, manual=True), "too-old")
        old.created_at = now - timedelta(days=95)
        old.selected_concept = {"concept_name": "Ancient concept"}
        repo.create(value, "current")
    await research_run(str(value.run_id))
    await research_run(str(value.run_id))
    await screen_and_select_run(str(value.run_id))
    with session_scope() as session:
        record = RunRepository(session).get(str(value.run_id), full=True)
        calls = [call for call in record.provider_calls if call["stage"] == "research"]
        assert len(calls) == 1
        prompt = calls[0]["prompt"]
        assert "garment-dyed" in prompt and "1717" in prompt
        assert "Past concept 0" in prompt and "Past concept 29" in prompt
        assert "Past concept 30" not in prompt and "Ancient concept" not in prompt
        assert len(record.concepts) == 25
        assert len(record.selection["score_breakdowns"]) == 25
        selected = next(item for item in record.concepts if item.selected)
        assert record.selection["weighted_score"] == selected.weighted_score


@pytest.mark.asyncio
async def test_run_review_shows_strategy_sources_and_scores_without_ip_when_disabled(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        ConfigurationRepository(session).save_template(fixture_product_template())
        RunRepository(session).create(value, "strategy-review")
    await research_run(str(value.run_id))
    await screen_and_select_run(str(value.run_id))
    with TestClient(create_app(get_settings())) as client:
        login = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
        client.post("/login", data={"password": "test-password", "csrf_token": token})
        page = client.get(f"/runs/{value.run_id}")
        assert page.status_code == 200
        assert "Odd Hour Press" in page.text and "Premise, scores &amp; sources" in page.text
        assert "Fixture evidence" in page.text
        assert "ip_penalty" not in page.text
        payload = client.get(f"/api/runs/{value.run_id}").json()
        assert payload["selected_concept"]["strategy"]["premise"]
        assert payload["concepts"][0]["score_breakdown"]["version"] == "strategy-v1"
