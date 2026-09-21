from __future__ import annotations

import io
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw

from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.domain.listing_copy import validate_listing_copy
from merch.domain.pricing import quote_price
from merch.models import Base
from merch.pipeline import (
    automatic_approval_signal,
    create_run,
    finish_publishing,
    generate_package_run,
    publish_channel_run,
    record_approval,
    research_run,
    revalidate_approval_run,
    screen_and_select_run,
)
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import (
    CreativeBrief,
    MarketplaceListingSet,
    ProductTemplate,
    PublishStatus,
    QAReport,
    RunInput,
    RunStatus,
)
from merch.services.openai_service import ModelResult, OpenAIService
from merch.services.printify import PrintifyClient
from merch.services.storage import ArtifactStorage
from merch.setup_comfort_colors import COLORS, SIZES, ReviewedCosts, build_template


@pytest.mark.asyncio
async def test_comfort_colors_daily_run_preserves_strategy_and_publishes_approved_sizes(
    isolated_app: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use real prepress, pricing, approval and publication with fictional providers.

    MERCH_TEST_FULL_PRINT_SIZE=1 additionally exercises the actual 4494x5097
    production canvas; the default scales only the output dimensions for CI.
    """
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    dimensions = {"S": (3703, 4200), "M": (4107, 4658)}
    catalog = {
        "variants": [
            {
                "id": index, "title": f"{color} / {size}",
                "options": {"color": color, "size": size},
                "placeholders": [{
                    "position": "front", "decoration_method": "dtg",
                    "width": dimensions.get(size, (4494, 5097))[0],
                    "height": dimensions.get(size, (4494, 5097))[1],
                }],
            }
            for index, (color, size) in enumerate(
                ((color, size) for color in COLORS for size in SIZES), start=20001,
            )
        ],
    }
    today = datetime.now(UTC).date()
    cost_by_size = {size: 1300 + index * 100 for index, size in enumerate(SIZES)}
    costs = ReviewedCosts(
        currency="USD", reviewed_at=today,
        costs_by_variant={
            str(row["id"]): cost_by_size[row["options"]["size"]]
            for row in catalog["variants"]
        },
    )
    template = build_template(catalog, fixture_product_template(), costs, verified_at=today)
    assert (template.print_width, template.print_height) == (4494, 5097)
    if os.environ.get("MERCH_TEST_FULL_PRINT_SIZE") != "1":
        template = template.model_copy(update={"print_width": 449, "print_height": 510})
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
    settings = get_settings().model_copy(update={
        "provider_mode": "fake", "publish_mode": "dry_run", "storage_backend": "local",
        "font_family": "DejaVu Sans",
        "font_file": Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        "realesrgan_binary": None, "realesrgan_endpoint": None,
        "flat_artwork_cleanup_enabled": False,
        "manual_approval_enabled": False, "ip_check_enabled": False,
        "etsy_production_partner_check_enabled": False,
    })
    metadata = {"model": "fixture", "estimated_cost_usd": 0.0}
    original_visual = OpenAIService.visual_qa
    visual_colors: list[list[str]] = []

    async def no_provider_requests(self: PrintifyClient, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A fake/dry-run acceptance test must never call Printify")

    async def artwork(
        self: OpenAIService, brief: CreativeBrief, width: int, height: int,
    ) -> tuple[bytes, dict[str, Any]]:
        assert brief.strategy is not None
        assert set(brief.shirt_colors) == set(COLORS)
        image = Image.new("RGBA", (width, height))
        draw = ImageDraw.Draw(image)
        left, right = width // 8, width * 7 // 8
        span = right - left
        white_end = left + span * 47 // 100
        black_end = left + span * 94 // 100
        draw.rectangle(
            (left, height // 8, white_end - 1, height * 7 // 8), fill="#FFFFFF"
        )
        draw.rectangle(
            (white_end, height // 8, black_end - 1, height * 7 // 8), fill="#000000"
        )
        draw.rectangle(
            (black_end, height // 8, right - 1, height * 7 // 8), fill="#FF00FF"
        )
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue(), metadata

    async def visual(
        self: OpenAIService, image: bytes, brief: CreativeBrief, deterministic: QAReport,
        *, effects: dict[str, Any] | None = None,
    ) -> ModelResult[QAReport]:
        result = await original_visual(self, image, brief, deterministic, effects=effects)
        assert result.value.passed
        assert set(brief.shirt_colors) == set(COLORS)
        visual_colors.append(brief.shirt_colors)
        return result

    monkeypatch.setattr(PrintifyClient, "_request", no_provider_requests)
    monkeypatch.setattr(OpenAIService, "artwork", artwork)
    monkeypatch.setattr(OpenAIService, "visual_qa", visual)
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=False)
    run_id = str(value.run_id)
    create_run(value, f"comfort-colors-daily-{run_id}")
    await research_run(run_id, settings)
    assert await screen_and_select_run(run_id, settings)
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        selected = dict(run.selected_concept or {})
        selected["slogan_if_any"] = None
        run.selected_concept = selected
    assert await generate_package_run(run_id, settings=settings)

    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.AWAITING_APPROVAL.value
        assert run.research_report and len(run.research_report["candidates"]) == 25
        assert run.selected_concept and run.creative_brief and run.listings
        strategy = run.selected_concept["strategy"]
        assert strategy and run.creative_brief["strategy"] == strategy
        assert run.template_snapshot == template.model_dump(mode="json")
        publication = ProductTemplate.model_validate(run.publication_template_snapshot)
        assert publication.garment_facts == template.garment_facts
        assert publication.production_costs_reviewed_at == today
        assert run.excluded_shirt_colors == []
        enabled = [item for item in publication.variants if item.enabled]
        assert len(enabled) == 98
        assert {item.color for item in enabled} == set(COLORS)
        for color in {item.color for item in enabled}:
            assert {item.size for item in enabled if item.color == color} == set(SIZES)
        assert publication.featured_variant() in enabled
        assert publication.featured_variant().size == "L"
        approved_ids = {item.variant_id for item in enabled}
        expected_quotes = [
            quote_price(
                channel=channel.channel, variant_id=variant.variant_id,
                production_cost_cents=costs.costs_by_variant[str(variant.variant_id)],
                percent_fee=channel.percent_fee, fixed_fee_cents=channel.fixed_fee_cents,
                target_margin=settings.target_margin,
            ).model_dump(mode="json")
            for channel in publication.channels if channel.enabled for variant in enabled
        ]
        assert run.price_quotes == expected_quotes
        assert len(run.price_quotes) == 98 * 3
        listings = MarketplaceListingSet.model_validate(run.listings)
        validate_listing_copy(listings)
        assert all(item.target_customer == strategy["micro_niche"] for item in listings.listings)
        assert run.listing_generation_state and run.listing_generation_state["final"] == run.listings
        for stage in ("listings", "listing_polish"):
            prompt = next(call["prompt"] for call in run.provider_calls if call["stage"] == stage)
            assert strategy["micro_niche"] in prompt and strategy["premise"] in prompt
            assert "garment-dyed" in prompt and '"model": "1717"' in prompt
        artifact = next(item for item in run.artifacts if item.kind == "production-v1")
        png = ArtifactStorage(settings).get(artifact.object_key)
        output_image = Image.open(io.BytesIO(png))
        assert output_image.size == (template.print_width, template.print_height)
        assert output_image.mode == "RGBA" and output_image.info.get("icc_profile")
        assert output_image.info["dpi"][0] == pytest.approx(300, abs=0.1)
        assert run.qa_report and run.qa_report["passed"]
    assert visual_colors
    signal = automatic_approval_signal(run_id, settings)
    assert signal is not None and signal.actor == "system"
    record_approval(run_id, signal, settings)
    assert await revalidate_approval_run(run_id, settings)
    results = [await publish_channel_run(run_id, channel, settings) for channel in signal.channels]
    assert results == [PublishStatus.DRY_RUN] * 3
    finish_publishing(run_id, results)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.PUBLISHED.value
        assert len(run.approvals) == 1 and run.approvals[0].actor == "system"
        assert len(run.publishes) == 3
        for publish in run.publishes:
            payload = publish.response_data
            assert publish.status == PublishStatus.DRY_RUN.value
            assert payload["blueprint_id"] == 706 and payload["print_provider_id"] == 99
            assert {item["id"] for item in payload["variants"]} == approved_ids
            assert {item["id"] for item in payload["variants"] if item["is_default"]} == {publication.featured_variant_id}
            assert set(payload["print_areas"][0]["variant_ids"]) == approved_ids
            assert payload["verification"]["variant_count"] == 98
        assert run.template_snapshot == template.model_dump(mode="json")
        assert ProductTemplate.model_validate(run.publication_template_snapshot) == publication
        assert run.creative_brief and run.creative_brief["strategy"] == strategy
        assert ConfigurationRepository(session).get_template() == template
