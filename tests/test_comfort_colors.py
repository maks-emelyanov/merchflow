from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from merch.config import Settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.models import Base, ProductTemplateRecord
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import Channel, ProductTemplate, RunInput, RunStatus
from merch.services.printify import PrintifyClient
from merch.setup_comfort_colors import (
    BLUEPRINT_ID,
    COLORS,
    PROVIDER_ID,
    SIZES,
    ReviewedCosts,
    build_template,
    catalog_selection,
    read_reviewed_costs,
    setup_comfort_colors,
)


@pytest.fixture
def catalog() -> dict[str, Any]:
    return {
        "id": PROVIDER_ID,
        "variants": [
            {
                "id": index, "title": f"{color} / {size}",
                "options": {"color": color, "size": size},
                "placeholders": [{
                    "position": "front", "decoration_method": "dtg", "width": 4500, "height": 5100,
                }],
            }
            for index, (color, size) in enumerate(
                ((color, size) for color in COLORS for size in SIZES), start=20001,
            )
        ],
    }


def _costs(catalog: dict[str, Any]) -> ReviewedCosts:
    return ReviewedCosts(
        currency="USD", reviewed_at=datetime.now(UTC).date(),
        costs_by_variant={str(row["id"]): 1300 + index for index, row in enumerate(catalog["variants"])},
    )


@pytest.fixture
def configured(isolated_app: Path) -> ProductTemplate:
    Base.metadata.create_all(get_engine())
    template = fixture_product_template()
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
    return template


@pytest.fixture
def mocked_printify(
    monkeypatch: pytest.MonkeyPatch, catalog: dict[str, Any], configured: ProductTemplate,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "catalog": catalog, "requests": [], "variant_reads": 0, "on_recheck": None,
        "blueprint": {"id": BLUEPRINT_ID, "brand": "Comfort Colors®", "model": "1717"},
        "providers": [{"id": PROVIDER_ID, "title": "SwiftPOD", "decoration_methods": ["dtg"]}],
        "shops": [
            {"id": channel.printify_shop_id, "sales_channel": channel.channel.value}
            for channel in configured.channels
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append((request.method, request.url.path))
        assert request.method == "GET"
        if request.url.path.endswith("/shops.json"):
            value = state["shops"]
        elif request.url.path.endswith("/706.json"):
            value = state["blueprint"]
        elif request.url.path.endswith("/print_providers.json"):
            value = state["providers"]
        elif request.url.path.endswith("/39/variants.json"):
            state["variant_reads"] += 1
            if state["variant_reads"] % 2 == 0 and state["on_recheck"]:
                state["on_recheck"]()
            value = state["catalog"]
        else:
            raise AssertionError(f"Unexpected request: {request.url}")
        return httpx.Response(200, json=copy.deepcopy(value))

    def client(settings: Settings) -> PrintifyClient:
        return PrintifyClient(settings, client=httpx.AsyncClient(
            base_url="https://api.printify.com/v1", transport=httpx.MockTransport(handler),
        ))

    monkeypatch.setattr("merch.setup_comfort_colors.PrintifyClient", client)
    return state


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, printify_api_token="test-only-token", **kwargs)


def _file_costs(path: Path, catalog: dict[str, Any]) -> ReviewedCosts:
    costs_path = path / "costs.json"
    costs_path.write_text(_costs(catalog).model_dump_json())
    return read_reviewed_costs(costs_path)


def test_build_template_preserves_channels_and_specific_costs(catalog: dict[str, Any]) -> None:
    original = fixture_product_template()
    costs = _costs(catalog)
    result = build_template(catalog, original, costs, verified_at=date(2026, 9, 20))
    assert (result.blueprint_id, result.print_provider_id) == (706, 39)
    assert len(result.variants) == 48
    assert {item.size for item in result.variants} == set(SIZES)
    assert {item.color for item in result.variants} == set(COLORS)
    assert result.channels == original.channels
    assert result.etsy_production_partner_confirmed == original.etsy_production_partner_confirmed
    assert result.featured_variant().color == "Pepper"
    assert result.featured_variant().size == "L"
    assert all(item.production_cost_cents == costs.costs_by_variant[str(item.variant_id)] for item in result.variants)
    assert result.garment_facts and result.garment_facts.fit == "relaxed"
    assert result.garment_facts.finish == "garment-dyed"
    assert result.production_costs_reviewed_at == costs.reviewed_at


def test_catalog_accepts_provider_size_scaling_and_one_pixel_rounding(catalog: dict[str, Any]) -> None:
    size_dimensions = {"S": (3461, 3955), "M": (3839, 4387)}
    for row in catalog["variants"]:
        width, height = size_dimensions.get(row["options"]["size"], (4200, 4800))
        row["placeholders"][0].update(width=width, height=height)
    selected, width, height = catalog_selection(catalog)
    assert len(selected) == 48
    assert (width, height) == (4200, 4800)


@pytest.mark.parametrize("price", [None, 0, -1, 12.50, True, "1299"])
def test_reviewed_costs_reject_invalid_prices(price: Any) -> None:
    with pytest.raises(ValidationError):
        ReviewedCosts.model_validate({
            "currency": "USD", "reviewed_at": "2026-01-01", "costs_by_variant": {"123": price},
        })


@pytest.mark.parametrize("change", [
    {"currency": "EUR"}, {"reviewed_at": (datetime.now(UTC).date() + timedelta(days=1)).isoformat()},
    {"costs_by_variant": {"01": 1200}}, {"costs_by_variant": {}},
])
def test_reviewed_costs_reject_unsupported_currency_date_and_ids(change: dict[str, Any]) -> None:
    values = {"currency": "USD", "reviewed_at": "2026-01-01", "costs_by_variant": {"123": 1200}}
    with pytest.raises(ValidationError):
        ReviewedCosts.model_validate({**values, **change})


@pytest.mark.parametrize("mutation,match", [
    ("missing", "missing requested"), ("unavailable", "missing requested"),
    ("dimensions", "incompatible"), ("method", "one front-DTG"),
    ("duplicate", "Ambiguous"), ("duplicate_id", "not unique"),
])
def test_catalog_requires_all_requested_variants_and_front_areas(
    catalog: dict[str, Any], mutation: str, match: str,
) -> None:
    if mutation == "missing":
        catalog["variants"].pop()
    elif mutation == "unavailable":
        catalog["variants"][0]["is_available"] = False
    elif mutation == "dimensions":
        catalog["variants"][0]["placeholders"][0]["width"] = 4000
    elif mutation == "method":
        catalog["variants"][0]["placeholders"][0]["decoration_method"] = "dtf"
    elif mutation == "duplicate":
        catalog["variants"].append(copy.deepcopy(catalog["variants"][0]))
    elif mutation == "duplicate_id":
        catalog["variants"][0]["id"] = catalog["variants"][1]["id"]
    with pytest.raises(ValueError, match=match):
        catalog_selection(catalog)


@pytest.mark.parametrize("extra", [False, True])
def test_costs_must_cover_exact_catalog_selection(catalog: dict[str, Any], extra: bool) -> None:
    costs = _costs(catalog)
    if extra:
        costs.costs_by_variant["99999"] = 1200
    else:
        costs.costs_by_variant.pop(str(catalog["variants"][0]["id"]))
    with pytest.raises(ValueError, match="match selected variants exactly"):
        build_template(catalog, fixture_product_template(), costs, verified_at=date.today())


@pytest.mark.asyncio
async def test_preview_without_costs_exposes_real_ids_and_never_writes(
    mocked_printify: dict[str, Any], configured: ProductTemplate,
) -> None:
    preview = await setup_comfort_colors(settings=_settings())
    assert preview["status"] == "preview"
    assert preview["activation_ready"] is False
    assert preview["variant_count"] == 48
    assert all(value is None for value in preview["costs_file_template"]["costs_by_variant"].values())
    assert mocked_printify["variant_reads"] == 1
    with session_scope() as session:
        assert ConfigurationRepository(session).get_template() == configured
        assert session.scalar(select(func.count()).select_from(ProductTemplateRecord)) == 1


@pytest.mark.asyncio
async def test_preview_with_costs_prices_every_preserved_channel(
    mocked_printify: dict[str, Any], catalog: dict[str, Any], tmp_path: Path,
) -> None:
    result = await setup_comfort_colors(_file_costs(tmp_path, catalog), settings=_settings())
    assert result["activation_ready"] is True
    assert len(result["retail_prices"]) == 48 * 3
    assert {item["channel"] for item in result["retail_prices"]} == {item.value for item in Channel}
    assert all(item["retail_price_cents"] % 100 == 99 for item in result["retail_prices"])
    with session_scope() as session:
        assert ConfigurationRepository(session).get_template_record().version == 1


@pytest.mark.asyncio
async def test_activation_rechecks_catalog_and_is_idempotent(
    mocked_printify: dict[str, Any], catalog: dict[str, Any], configured: ProductTemplate, tmp_path: Path,
) -> None:
    costs = _file_costs(tmp_path, catalog)
    result = await setup_comfort_colors(costs, activate=True, settings=_settings())
    assert result["status"] == "activated"
    assert result["active_template_version"] == 2
    assert mocked_printify["variant_reads"] == 2
    with session_scope() as session:
        original = session.get(ProductTemplateRecord, 1)
        assert original and ProductTemplate.model_validate(original.data) == configured
    repeated = await setup_comfort_colors(costs, activate=True, settings=_settings())
    assert repeated["status"] == "already_active"
    assert repeated["active_template_version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [RunStatus.PENDING, RunStatus.PARTIALLY_PUBLISHED, RunStatus.VERIFICATION_REQUIRED, RunStatus.AWAITING_BRIEF_REVISION])
async def test_activation_rejects_active_runs_and_preserves_snapshot(
    mocked_printify: dict[str, Any], catalog: dict[str, Any], configured: ProductTemplate,
    tmp_path: Path, state: RunStatus,
) -> None:
    with session_scope() as session:
        run = RunRepository(session).create(RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC)), "active-run")
        run.status = state.value
        run.template_snapshot = configured.model_dump(mode="json")
        run_id = run.id
    with pytest.raises(ValueError, match="Resolve active run"):
        await setup_comfort_colors(_file_costs(tmp_path, catalog), activate=True, settings=_settings())
    with session_scope() as session:
        assert ConfigurationRepository(session).get_template() == configured
        assert RunRepository(session).get(run_id).template_snapshot == configured.model_dump(mode="json")


@pytest.mark.asyncio
async def test_activation_rejects_concurrent_template_change(
    mocked_printify: dict[str, Any], catalog: dict[str, Any], tmp_path: Path,
) -> None:
    def change_template() -> None:
        with session_scope() as session:
            repository = ConfigurationRepository(session)
            repository.save_template(repository.get_template().model_copy(update={"name": "Operator changed"}))

    mocked_printify["on_recheck"] = change_template
    with pytest.raises(ValueError, match="Active template changed"):
        await setup_comfort_colors(_file_costs(tmp_path, catalog), activate=True, settings=_settings())
    with session_scope() as session:
        assert ConfigurationRepository(session).get_template().name == "Operator changed"


@pytest.mark.asyncio
async def test_activation_rejects_catalog_changes_after_preview(
    mocked_printify: dict[str, Any], catalog: dict[str, Any], tmp_path: Path,
) -> None:
    def change_catalog() -> None:
        mocked_printify["catalog"]["variants"][0]["title"] = "Changed title"

    mocked_printify["on_recheck"] = change_catalog
    with pytest.raises(ValueError, match="catalog changed during preflight"):
        await setup_comfort_colors(_file_costs(tmp_path, catalog), activate=True, settings=_settings())
    with session_scope() as session:
        assert ConfigurationRepository(session).get_template_record().version == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change,match", [
    ("blueprint", "expected Comfort Colors"), ("provider", "available provider"),
    ("shop", "not connected to Etsy"), ("missing_shop", "shop is unavailable"),
])
async def test_preflight_verifies_real_catalog_identity_and_shops(
    mocked_printify: dict[str, Any], change: str, match: str,
) -> None:
    if change == "blueprint":
        mocked_printify["blueprint"]["model"] = "1745"
    elif change == "provider":
        mocked_printify["providers"] = []
    elif change == "shop":
        mocked_printify["shops"][1]["sales_channel"] = "disconnected"
    else:
        mocked_printify["shops"].pop()
    with pytest.raises(ValueError, match=match):
        await setup_comfort_colors(settings=_settings())


@pytest.mark.asyncio
async def test_activation_without_costs_cannot_read_or_mutate_provider(
    mocked_printify: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="requires --costs-file"):
        await setup_comfort_colors(activate=True, settings=_settings())
    assert mocked_printify["requests"] == []


@pytest.mark.asyncio
async def test_partner_confirmation_toggle_is_preserved(
    mocked_printify: dict[str, Any],
) -> None:
    with session_scope() as session:
        repository = ConfigurationRepository(session)
        repository.save_template(repository.get_template().model_copy(update={"etsy_production_partner_confirmed": False}))
    with pytest.raises(ValueError, match="Confirm the Etsy production partner"):
        await setup_comfort_colors(settings=_settings(etsy_production_partner_check_enabled=True))
    result = await setup_comfort_colors(settings=_settings(etsy_production_partner_check_enabled=False))
    assert result["status"] == "preview"


def test_cli_registers_setup_command() -> None:
    from typer.testing import CliRunner

    from merch.cli import app

    result = CliRunner().invoke(app, ["setup-comfort-colors", "--help"])
    assert result.exit_code == 0
    assert "--costs-file" in result.output and "--activate" in result.output


def test_cost_file_template_requires_reviewed_values(catalog: dict[str, Any]) -> None:
    sample = json.loads(_costs(catalog).model_dump_json())
    sample["costs_by_variant"][str(catalog["variants"][0]["id"])] = None
    with pytest.raises(ValidationError):
        ReviewedCosts.model_validate(sample)
