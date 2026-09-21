from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.domain.listing_copy import normalize_listing_copy
from merch.domain.pricing import quote_price
from merch.domain.product_options import publication_template
from merch.models import Base
from merch.pipeline import (
    ApprovalInvalid,
    automatic_approval_signal,
    record_approval,
    revalidate_approval_run,
)
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import (
    Channel,
    MarketplaceListing,
    MarketplaceListingSet,
    ProductTemplate,
    RunInput,
    RunStatus,
)
from merch.services.printify import PrintifyClient


def quotes_for(template: ProductTemplate) -> list[dict[str, Any]]:
    return [
        quote_price(
            channel=channel.channel, variant_id=variant.variant_id,
            production_cost_cents=variant.production_cost_cents,
            percent_fee=channel.percent_fee, fixed_fee_cents=channel.fixed_fee_cents,
            target_margin=get_settings().target_margin,
        ).model_dump(mode="json")
        for channel in template.channels if channel.enabled
        for variant in template.variants if variant.enabled
    ]


def saved_package(active: ProductTemplate) -> tuple[str, ProductTemplate, ProductTemplate]:
    Base.metadata.create_all(get_engine())
    approved = fixture_product_template()
    excluded = [approved.variants[-1].color]
    publication = publication_template(approved, excluded)
    listings = normalize_listing_copy(MarketplaceListingSet(listings=[
        MarketplaceListing(
            channel=channel, title="Original hiking shirt", short_description="A hiking shirt.",
            long_description="An original trail illustration.", tags=["hiking shirt"],
            bullet_points=[], alt_text="A winding trail", target_customer="Hikers",
            gift_occasions=[], seo_meta_title="Original hiking shirt",
            seo_meta_description="An original hiking shirt.",
        ) for channel in Channel
    ]))
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        ConfigurationRepository(session).save_template(active)
        run = RunRepository(session).create(value, f"fixture-{value.run_id}")
        run.status = RunStatus.PUBLISHING.value
        run.template_snapshot = approved.model_dump(mode="json")
        run.publication_template_snapshot = publication.model_dump(mode="json")
        run.excluded_shirt_colors = excluded
        run.qa_report = {"passed": True}
        run.listings = listings.model_dump(mode="json")
        run.price_quotes = quotes_for(publication)
    return str(value.run_id), approved, publication


def another_garment() -> ProductTemplate:
    return fixture_product_template().model_copy(update={
        "name": "Comfort Colors 1717", "blueprint_id": 706, "print_provider_id": 99,
        "print_width": 4494, "print_height": 5097,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("cost_increase", [0, 400])
async def test_historical_garment_revalidation_never_replaces_active_template(
    isolated_app: Path, monkeypatch: pytest.MonkeyPatch, cost_increase: int,
) -> None:
    active = another_garment()
    run_id, approved, publication = saved_package(active)
    seen: list[ProductTemplate] = []

    async def validate(self: PrintifyClient, template: ProductTemplate) -> ProductTemplate:
        seen.append(template)
        return template.model_copy(update={"variants": [
            item.model_copy(update={
                "production_cost_cents": approved.variants[index].production_cost_cents + cost_increase,
            }) for index, item in enumerate(template.variants)
        ]})

    monkeypatch.setattr(PrintifyClient, "validate_template", validate)
    assert await revalidate_approval_run(run_id) is (cost_increase == 0)
    assert {(item.blueprint_id, item.print_provider_id) for item in seen} == {(6, 1)}
    with session_scope() as session:
        config = ConfigurationRepository(session)
        assert config.get_template() == active
        assert config.get_template_record().version == 1
        run = RunRepository(session).get(run_id)
        current = ProductTemplate.model_validate(run.template_snapshot)
        effective = ProductTemplate.model_validate(run.publication_template_snapshot)
        assert current.blueprint_id == effective.blueprint_id == approved.blueprint_id
        assert effective.featured_variant_id == publication.featured_variant_id
        assert run.excluded_shirt_colors == [approved.variants[-1].color]
        assert run.price_quotes == quotes_for(effective)
        assert effective.variants[0].production_cost_cents == 950 + cost_increase
        assert run.status == (
            RunStatus.AWAITING_APPROVAL.value if cost_increase else RunStatus.PUBLISHING.value
        )
    if cost_increase:
        signal = automatic_approval_signal(run_id)
        assert signal is not None
        record_approval(run_id, signal)
        assert await revalidate_approval_run(run_id)


@pytest.mark.asyncio
async def test_same_garment_reviewed_costs_and_fees_refresh_publication_prices(
    isolated_app: Path,
) -> None:
    original = fixture_product_template()
    active = original.model_copy(update={
        "variants": [item.model_copy(update={"production_cost_cents": 1800}) for item in original.variants],
        "channels": [item.model_copy(update={"fixed_fee_cents": 100}) for item in original.channels],
    })
    run_id, _, publication = saved_package(active)
    assert not await revalidate_approval_run(run_id)
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert run.template_snapshot == active.model_dump(mode="json")
        effective = ProductTemplate.model_validate(run.publication_template_snapshot)
        assert effective.channels == active.channels
        assert {item.production_cost_cents for item in effective.variants} == {1800}
        assert [item.enabled for item in effective.variants] == [item.enabled for item in publication.variants]
        assert effective.featured_variant_id == publication.featured_variant_id
        assert run.price_quotes == quotes_for(effective)
        assert ConfigurationRepository(session).get_template_record().version == 1
    signal = automatic_approval_signal(run_id)
    assert signal is not None
    record_approval(run_id, signal)
    assert await revalidate_approval_run(run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["dimensions", "variant_id", "size"])
async def test_same_garment_changed_options_invalidate_artwork_before_snapshot_validation(
    isolated_app: Path, change: str,
) -> None:
    original = fixture_product_template()
    active = original.model_copy(update={"print_width": original.print_width + 10}) if change == "dimensions" else original.model_copy(update={
        "variants": [
            original.variants[0].model_copy(update={"variant_id": 9001} if change == "variant_id" else {"size": "XL"}),
            original.variants[1],
        ],
    })
    run_id, _, _ = saved_package(active)
    assert not await revalidate_approval_run(run_id)
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert run.status == RunStatus.AWAITING_APPROVAL.value
        assert run.qa_report is None and run.publication_template_snapshot is None
        assert run.template_snapshot == active.model_dump(mode="json")
        assert run.price_quotes == []


def test_same_garment_changed_configuration_still_blocks_stale_approval(isolated_app: Path) -> None:
    original = fixture_product_template()
    active = original.model_copy(update={
        "variants": [item.model_copy(update={"production_cost_cents": 1800}) for item in original.variants],
    })
    run_id, _, _ = saved_package(active)
    with session_scope() as session:
        RunRepository(session).get(run_id).status = RunStatus.AWAITING_APPROVAL.value
    signal = automatic_approval_signal(run_id)
    assert signal is not None
    with pytest.raises(ApprovalInvalid, match="product template changed"):
        record_approval(run_id, signal)
