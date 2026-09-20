from __future__ import annotations

import io
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw
from pydantic import SecretStr

from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.domain.prepress import deterministic_qa as actual_deterministic_qa
from merch.domain.prepress import prepare_artwork as actual_prepare_artwork
from merch.domain.product_options import (
    catalog_replacement_groups,
    full_color_publication_template,
    publication_template,
)
from merch.models import Base, ProductMappingRecord, ProductTemplateRecord
from merch.pipeline import (
    ApprovalInvalid,
    _qa_with_color_replacements,
    automatic_approval_signal,
    create_run,
    finish_publishing,
    generate_package_run,
    import_etsy_listing_defaults,
    publish_channel_run,
    record_approval,
    repair_published_etsy_listing,
    research_run,
    rewrite_failed_brief_run,
    run_fixture_pipeline,
    screen_and_select_run,
)
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import (
    ApprovalSignal,
    Channel,
    ChannelConfig,
    CreativeBrief,
    DesignMode,
    EtsyListingDefaults,
    PriceQuote,
    ProductTemplate,
    PublishStatus,
    QAIssue,
    QAReport,
    RunInput,
    RunStatus,
    ShirtColorRanking,
    ShirtColorScore,
    VariantConfig,
)
from merch.services.etsy_publisher import direct_inventory
from merch.services.openai_service import ModelResult, OpenAIService
from merch.services.printify import PrintifyClient
from merch.setup_etsy_tee import COLORS, COST_CENTS, SIZES, build_template
from merch.temporal import retry_failed_artwork_run


def test_product_template_can_be_replaced_without_overwriting_history(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    first = fixture_product_template()
    second = first.model_copy(update={"name": "Etsy-only tee"})
    with session_scope() as session:
        repo = ConfigurationRepository(session)
        assert repo.save_template(first).version == 1
        assert repo.save_template(second).version == 2
    with session_scope() as session:
        rows = list(session.query(ProductTemplateRecord).order_by(ProductTemplateRecord.version))
        assert [(row.id, row.version, row.active) for row in rows] == [
            (1, 1, False),
            (2, 2, True),
        ]
        assert ConfigurationRepository(session).get_template().name == "Etsy-only tee"


@pytest.mark.asyncio
async def test_automatic_brief_rewrites_stop_after_eight_and_keep_concept(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(update={"print_width": 400, "print_height": 500})
        )
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=False)
    await run_fixture_pipeline(value, get_settings())
    run_id = str(value.run_id)
    with session_scope() as session:
        original = RunRepository(session).get(run_id, full=True)
        concept_name = original.creative_brief["concept_name"]
        slogan = original.creative_brief["slogan"]
    calls = 0

    async def rewrite(self, concept, brief, issues, colors, *, recovery_context=None):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return ModelResult(
            brief.model_copy(update={
                "composition": f"Detached primary motifs with generous open space, layout {calls}",
                "generation_brief": f"Draw broad independent flat motifs, arrangement {calls}",
            }),
            {"model": "fixture", "estimated_cost_usd": 0.25},
        )

    monkeypatch.setattr(OpenAIService, "revise_brief", rewrite)
    failed_qa = QAReport(
        passed=False, revision=1, issues=[
            QAIssue(code="OVERLAP", severity="error", message="Shapes overlap")
        ], width=400, height=500, has_alpha=True, color_profile="RGBA",
    )
    for version in range(1, 10):
        with session_scope() as session:
            repo = RunRepository(session)
            run = repo.get(run_id, full=True)
            assert run.version == version
            artifact = next(
                (item for item in run.artifacts if item.kind == f"production-v{version}"), None
            )
            if artifact is None:
                artifact = repo.add_artifact(
                    run_id, kind=f"production-v{version}", revision=1,
                    object_key=f"fixture-production-{run_id}-{version}", sha256="0" * 64,
                    width=400, height=500, metadata={},
                )
            artifact.metadata_json = {**artifact.metadata_json, "qa": failed_qa.model_dump(mode="json")}
            run.qa_report = None
            run.status = RunStatus.AWAITING_BRIEF_REVISION.value
        outcome = await rewrite_failed_brief_run(run_id)
        assert outcome == (
            RunStatus.PENDING.value if version <= 8 else RunStatus.AWAITING_BRIEF_REVISION.value
        )
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.version == 9
        assert run.creative_brief["concept_name"] == concept_name
        assert run.creative_brief["slogan"] == slogan
        assert len([call for call in run.provider_calls if call["stage"] == "brief_rewrite"]) == 8
        assert not run.approvals and not run.publishes
    assert calls == 8


@pytest.mark.asyncio
async def test_native_etsy_poll_accepts_delayed_printify_link(isolated_app, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from merch.pipeline import _wait_for_native_etsy_link

    calls = 0

    class FakePrintify:
        async def product(self, shop_id, product_id):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return {"id": product_id, "external": {"id": "99"} if calls == 2 else None}

    async def no_wait(seconds):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("merch.pipeline.asyncio.sleep", no_wait)
    settings = get_settings().model_copy(update={"etsy_native_publish_grace_seconds": 600})
    remote = await _wait_for_native_etsy_link(
        "unused", FakePrintify(), "fixture-etsy", "product-1", settings,
        {"native_poll_started": datetime.now(UTC).isoformat()},
    )
    assert remote["external"]["id"] == "99"
    assert calls == 2


@pytest.mark.asyncio
async def test_import_etsy_defaults_from_verified_listing(isolated_app, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(update={"print_width": 400, "print_height": 500})
        )
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=False)
    await run_fixture_pipeline(value, get_settings())
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.get(str(value.run_id))
        title = next(item["title"] for item in run.listings["listings"] if item["channel"] == "etsy")
        publish = repo.publish_record(str(value.run_id), "etsy", "fixture")
        publish.status = "succeeded"
        publish.external_product_id = "99"

    class FakeEtsy:
        async def listing(self, listing_id):  # type: ignore[no-untyped-def]
            return {
                "listing_id": listing_id, "shop_id": 42, "state": "active", "title": title,
                "taxonomy_id": 482, "shipping_profile_id": 11, "return_policy_id": 12,
                "readiness_state_id": 13,
                "production_partners": [{"production_partner_id": 14}],
            }

        async def inventory(self, listing_id):  # type: ignore[no-untyped-def]
            return {"products": []}

        async def close(self):  # type: ignore[no-untyped-def]
            pass

    async def token(settings):  # type: ignore[no-untyped-def]
        return "token"

    monkeypatch.setattr("merch.pipeline.etsy_access_token", token)
    monkeypatch.setattr("merch.pipeline.EtsyStorefrontClient", lambda *args, **kwargs: FakeEtsy())
    settings = get_settings().model_copy(update={"etsy_shop_id": 42})
    assert await import_etsy_listing_defaults(99, settings) == 2
    assert await import_etsy_listing_defaults(99, settings) == 2
    with session_scope() as session:
        template = ConfigurationRepository(session).get_template()
        etsy = next(item for item in template.channels if item.channel == Channel.ETSY)
        assert etsy.etsy_listing_defaults.production_partner_ids == [14]


def test_swiftpod_etsy_template_has_98_real_catalog_combinations() -> None:
    catalog = {
        "variants": [
            {
                "id": index + 1,
                "title": f"{color} / {size}",
                "options": {"color": color, "size": size},
                "placeholders": [
                    {"position": "front", "decoration_method": "dtg", "width": 3692, "height": 4800}
                ],
            }
            for index, (color, size) in enumerate(
                (color, size) for color in COLORS for size in SIZES
            )
        ]
    }
    channel = ChannelConfig(
        channel=Channel.ETSY,
        printify_shop_id="test-etsy",
        percent_fee=0.12,
        fixed_fee_cents=45,
    )
    template = build_template(catalog, channel)
    assert len(template.variants) == 98
    assert {item.size for item in template.variants} == set(SIZES)
    assert {item.color for item in template.variants} == set(COLORS)
    assert len(template.qa_shirt_colors()) == 14
    assert all(item.production_cost_cents == COST_CENTS[item.size] for item in template.variants)
    assert template.channels == [channel]
    publication = publication_template(template, ["Olive", "Maroon"])
    assert len([item for item in publication.variants if item.enabled]) == 84
    assert {item.color for item in publication.variants if item.enabled} == set(COLORS) - {
        "Olive", "Maroon"
    }
    assert all(item.enabled for item in template.variants)
    inventory_payload = direct_inventory(
        {"variants": [{"id": item.variant_id, "sku": f"sku-{item.variant_id}"}
                      for item in template.variants]},
        template,
        [PriceQuote(channel=Channel.ETSY, variant_id=item.variant_id,
                    production_cost_cents=item.production_cost_cents,
                    retail_price_cents=2500 if item.size not in {"2XL", "3XL"} else 3000,
                    estimated_fee_cents=300, estimated_margin=0.4)
         for item in template.variants],
        EtsyListingDefaults(taxonomy_id=482, shipping_profile_id=1, return_policy_id=2,
                            readiness_state_id=3, production_partner_ids=[4]),
    )
    assert len(inventory_payload["products"]) == 98
    assert inventory_payload["price_on_property"] == [513, 514]
    assert len({item["sku"] for item in inventory_payload["products"]}) == 98
    with pytest.raises(ValueError, match="does not offer"):
        build_template({"variants": catalog["variants"][:-1]}, channel)


def test_catalog_replacements_require_all_sizes_and_keep_fourteen_colors() -> None:
    channel = ChannelConfig(
        channel=Channel.ETSY, printify_shop_id="test-etsy", percent_fee=0.12,
        fixed_fee_cents=45,
    )
    rows = [
        {
            "id": index + 1,
            "title": f"{color} / {size}",
            "options": {"color": color, "size": size},
            "placeholders": [
                {"position": "front", "decoration_method": "dtg", "width": 3692, "height": 4800}
            ],
        }
        for index, (color, size) in enumerate(
            (color, size) for color in (*COLORS, "Silver", "Forest", "Steel Blue") for size in SIZES
        )
    ]
    template = build_template({"variants": rows}, channel)
    smaller = next(
        item for item in rows
        if item["options"] == {"color": "Silver", "size": "XS"}
    )
    smaller["placeholders"][0].update(width=2767, height=3598)
    # A partially available color must never enter a publication.
    rows = [item for item in rows if item["options"] != {"color": "Forest", "size": "3XL"}]
    groups = catalog_replacement_groups(template, {"variants": rows})
    assert [group[0].color for group in groups] == ["Silver", "Steel Blue"]
    publication = full_color_publication_template(template, {"Olive"}, groups, set())
    assert publication is not None
    assert len(publication.variants) == 98
    assert {item.color for item in publication.variants} == set(COLORS) - {"Olive"} | {"Silver"}
    assert publication.featured_variant().color == "Black"
    assert full_color_publication_template(template, {"Olive"}, groups, {"Silver", "Steel Blue"}) is None


@pytest.mark.asyncio
async def test_visual_qa_rechecks_replacements_until_fourteen_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = ProductTemplate(
        name="Test SwiftPOD tee", blueprint_id=12, print_provider_id=39,
        print_width=100, print_height=100,
        variants=[
            VariantConfig(
                variant_id=index + 1, title=f"{color} / L", color=color,
                color_hex=swatch, size="L", production_cost_cents=1000,
            )
            for index, (color, swatch) in enumerate(COLORS.items())
        ],
        featured_variant_id=1,
        channels=[ChannelConfig(
            channel=Channel.ETSY, printify_shop_id="test-etsy", percent_fee=0.12,
            fixed_fee_cents=45,
        )],
    )
    catalog = {
        "variants": [
            {
                "id": index + 100,
                "title": f"{color} / L",
                "options": {"color": color, "size": "L"},
                "placeholders": [
                    {"position": "front", "decoration_method": "dtg", "width": 100, "height": 100}
                ],
            }
            for index, color in enumerate(("Silver", "Forest", "Steel Blue"))
        ]
    }

    async def variants(self, blueprint_id, provider_id):  # type: ignore[no-untyped-def]
        assert (blueprint_id, provider_id) == (12, 39)
        return catalog

    visual_colors: list[list[str]] = []

    async def visual_qa(self, image, brief, deterministic, *, effects=None):  # type: ignore[no-untyped-def]
        visual_colors.append(brief.shirt_colors)
        failing = "Black" if len(visual_colors) == 1 else "Silver" if len(visual_colors) == 2 else None
        issues = ([QAIssue(
            code="GARMENT_CONTRAST", severity="error", message=f"Low contrast on {failing}",
            affected_shirt_colors=[failing],
        )] if failing else [])
        return ModelResult(
            deterministic.model_copy(update={"passed": not issues, "issues": issues}),
            {"model": "fixture", "estimated_cost_usd": 0.0},
        )

    monkeypatch.setattr(PrintifyClient, "variants", variants)
    monkeypatch.setattr(OpenAIService, "visual_qa", visual_qa)
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((15, 15, 85, 85), fill=(0, 0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    brief = CreativeBrief(
        concept_name="Test", target_customer="Adults", customer_motivation="A gift",
        slogan=None, design_mode=DesignMode.ILLUSTRATION, visual_concept="Shape",
        composition="Centered", graphic_style="Flat", palette=["#000000"],
        shirt_colors=list(COLORS), typography_style=None, generation_brief="One black shape",
    )
    report = QAReport(
        passed=True, revision=1, issues=[], width=100, height=100,
        has_alpha=True, color_profile="sRGB",
    )
    settings = get_settings()
    result = await _qa_with_color_replacements(
        buffer.getvalue(), brief, report, template,
        settings.model_copy(update={"provider_mode": "live"}), OpenAIService(settings),
        source_scale=1.0, used_realesrgan=True, rendered_text=None,
    )
    assert result.report.passed and not result.shortfall
    assert result.excluded_base_colors == ["Black"]
    assert result.rejected_replacements == ["Forest", "Silver"]
    assert len(result.visual_calls) == 3
    assert result.publication is not None
    assert len({item.color for item in result.publication.variants}) == 14
    assert result.publication.featured_variant().color == "White"
    assert "Steel Blue" in visual_colors[-1]


@pytest.mark.asyncio
async def test_color_shortfall_blocks_publication_without_visual_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = ProductTemplate(
        name="Test SwiftPOD tee", blueprint_id=12, print_provider_id=39,
        print_width=100, print_height=100,
        variants=[
            VariantConfig(
                variant_id=index + 1, title=f"{color} / L", color=color,
                color_hex=swatch, size="L", production_cost_cents=1000,
            )
            for index, (color, swatch) in enumerate(COLORS.items())
        ],
        featured_variant_id=1,
        channels=[ChannelConfig(
            channel=Channel.ETSY, printify_shop_id="test-etsy", percent_fee=0.12,
            fixed_fee_cents=45,
        )],
    )
    async def no_visual(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("visual QA must not run without 14 colors")
    monkeypatch.setattr(OpenAIService, "visual_qa", no_visual)
    report = QAReport(
        passed=False, revision=1,
        issues=[QAIssue(
            code="contrast", severity="error", message="Black artwork on Black",
            affected_shirt_colors=["Black"],
        )],
        width=100, height=100, has_alpha=True, color_profile="sRGB",
    )
    brief = CreativeBrief(
        concept_name="Test", target_customer="Adults", customer_motivation="A gift",
        slogan=None, design_mode=DesignMode.ILLUSTRATION, visual_concept="Shape",
        composition="Centered", graphic_style="Flat", palette=["#000000"],
        shirt_colors=list(COLORS), typography_style=None, generation_brief="One black shape",
    )
    settings = get_settings()
    result = await _qa_with_color_replacements(
        b"unused", brief, report, template, settings, OpenAIService(settings),
        source_scale=1.0, used_realesrgan=True, rendered_text=None,
    )
    assert result.shortfall and not result.report.passed
    assert result.publication is None
    assert result.report.issues[-1].code == "insufficient_contrast_colors"


@pytest.mark.asyncio
async def test_resume_skips_prior_high_risk_concept_without_research_or_reselection(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    Base.metadata.create_all(get_engine())
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    create_run(value, f"manual-{value.run_id}")
    await research_run(str(value.run_id))
    with session_scope() as session:
        run = RunRepository(session).get(str(value.run_id))
        run.ip_report = {
            "candidate_reports": [
                {
                    "concept_name": run.research_report["candidates"][0]["concept_name"],
                    "report": {
                        "status": "review",
                        "risk_score": 48,
                        "searched_terms": ["fixture"],
                        "matches": [],
                        "uspto_search_url": "https://tmsearch.uspto.gov/search?query=fixture",
                        "notes": [],
                        "legal_clearance": False,
                    },
                }
            ]
        }

    async def no_reselection(*_: object) -> None:
        raise AssertionError("Astra selection must not repeat after a prior IP screen")

    monkeypatch.setattr("merch.services.openai_service.OpenAIService.select", no_reselection)
    assert await screen_and_select_run(
        str(value.run_id), get_settings().model_copy(update={"ip_check_enabled": True})
    )
    with session_scope() as session:
        run = RunRepository(session).get(str(value.run_id))
        assert run.selected_concept["concept_name"] == "Trail Ritual 2"
        assert len(run.ip_report["candidate_reports"]) == 2


@pytest.mark.asyncio
async def test_ip_check_is_disabled_by_default_and_does_not_screen(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    assert settings.ip_check_enabled is False
    Base.metadata.create_all(get_engine())
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    create_run(value, f"manual-{value.run_id}")
    await research_run(str(value.run_id), settings)

    async def no_ip_screen(*_: object) -> None:
        raise AssertionError("IP screening must be skipped when disabled")

    def no_deterministic_screen(*_: object) -> None:
        raise AssertionError("Deterministic IP screening must be skipped when disabled")

    monkeypatch.setattr("merch.services.openai_service.OpenAIService.ip_screen", no_ip_screen)
    monkeypatch.setattr("merch.pipeline.screen_concept", no_deterministic_screen)
    assert await screen_and_select_run(str(value.run_id), settings)
    with session_scope() as session:
        run = RunRepository(session).get(str(value.run_id), full=True)
        assert run.ip_report is None
        assert run.selected_concept is not None
        assert all(concept.eligible for concept in run.concepts)
        assert not any(call["stage"] == "ip_screen" for call in run.provider_calls)


@pytest.mark.asyncio
async def test_complete_mocked_pipeline_has_automatic_release_and_optional_manual_checks(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    settings = get_settings()
    Base.metadata.create_all(get_engine())
    template = fixture_product_template().model_copy(
        update={
            "print_width": 400,
            "print_height": 500,
            "etsy_production_partner_confirmed": False,
        }
    )
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    await run_fixture_pipeline(value, settings)
    with session_scope() as session:
        run = RunRepository(session).get(str(value.run_id), full=True)
        assert run.status == RunStatus.AWAITING_APPROVAL.value
        assert run.qa_report and run.qa_report["passed"]
        assert run.listings and len(run.listings["listings"]) == 3
        assert run.approvals == []
        assert run.publishes == []
        production = next(item for item in run.artifacts if item.kind == "production-v1")
        assert production.width == 400
        assert production.height == 500
        assert production.metadata_json["featured_color_selection"]["selected_variant_id"] in {1001, 1002}
    automatic = automatic_approval_signal(str(value.run_id), settings)
    assert automatic is not None
    assert automatic.actor == "system"
    assert set(automatic.channels) == set(Channel)
    assert automatic.ip_attested is False
    assert automatic_approval_signal(
        str(value.run_id), settings.model_copy(update={"manual_approval_enabled": True})
    ) is None
    assert automatic_approval_signal(
        str(value.run_id), settings.model_copy(update={"ip_check_enabled": True})
    ) is None
    with pytest.raises(ApprovalInvalid, match="manual review is enabled"):
        record_approval(
            str(value.run_id),
            automatic,
            settings.model_copy(update={"manual_approval_enabled": True}),
        )
    signal = ApprovalSignal(
        channels=[Channel.SHOPIFY, Channel.ETSY],
        expected_version=1,
        ip_attested=False,
        actor="test-operator",
    )
    with pytest.raises(ApprovalInvalid, match="production partner confirmation"):
        record_approval(
            str(value.run_id),
            signal,
            settings.model_copy(update={"etsy_production_partner_check_enabled": True}),
        )
    record_approval(str(value.run_id), signal)
    results = [
        await publish_channel_run(str(value.run_id), channel, settings)
        for channel in signal.channels
    ]
    finish_publishing(str(value.run_id), results)
    with session_scope() as session:
        run = RunRepository(session).get(str(value.run_id), full=True)
        assert run.status == RunStatus.PUBLISHED.value
        assert {item.channel for item in run.publishes} == {"shopify", "etsy"}
        assert all(item.status == "dry_run" for item in run.publishes)


def test_enabled_ip_check_requires_attestation(isolated_app) -> None:
    settings = get_settings().model_copy(update={"ip_check_enabled": True})
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(fixture_product_template())
        value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
        run = RunRepository(session).create(value, f"manual-{value.run_id}")
        run.status = RunStatus.AWAITING_APPROVAL.value
        run.qa_report = {"passed": True}
    signal = ApprovalSignal(
        channels=[Channel.SHOPIFY], expected_version=1, ip_attested=False, actor="test"
    )
    with pytest.raises(ApprovalInvalid, match="IP attestation is required"):
        record_approval(str(value.run_id), signal, settings)


def test_regeneration_invalidates_review_package(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    with session_scope() as session:
        repository = RunRepository(session)
        repository.create(value, f"manual-{value.run_id}")
        record = repository.get(str(value.run_id))
        record.qa_report = {"passed": True}
        record.listings = {"listings": []}
        record.price_quotes = []
        revised = repository.begin_revision(str(value.run_id), True)
        assert revised.version == 2
        assert revised.qa_report is None
        assert revised.listings is None
        assert revised.price_quotes is None


@pytest.mark.parametrize("regenerate", [False, True])
@pytest.mark.asyncio
async def test_package_retry_reuses_saved_paid_stages(
    isolated_app, monkeypatch: pytest.MonkeyPatch, regenerate: bool
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(update={"print_width": 400, "print_height": 500})
        )
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    run_id = str(value.run_id)
    create_run(value, f"manual-{run_id}")
    await research_run(run_id)
    assert await screen_and_select_run(run_id)
    if regenerate:
        with session_scope() as session:
            RunRepository(session).status(run_id, RunStatus.PENDING)
    original_listings = OpenAIService.listings
    calls = 0

    async def fail_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary listing outage")
        return await original_listings(self, *args, **kwargs)

    monkeypatch.setattr(OpenAIService, "listings", fail_once)
    version = 2 if regenerate else 1
    with pytest.raises(RuntimeError, match="temporary listing outage"):
        await generate_package_run(run_id, regenerate=regenerate)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.qa_report and run.qa_report["passed"]
        assert {item.kind for item in run.artifacts} == {
            f"source-v{version}",
            f"production-v{version}",
            f"color-preview-v{version}",
        }
        first_calls = list(run.provider_calls)
    assert await generate_package_run(run_id, regenerate=regenerate)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.version == version
        assert len(run.artifacts) == 3
        assert run.status == RunStatus.AWAITING_APPROVAL.value
        assert [item["stage"] for item in run.provider_calls] == [
            *[item["stage"] for item in first_calls],
            "listings",
            "listing_polish",
        ]
        assert all(
            "variant_id" not in item.get("prompt", "")
            for item in run.provider_calls
            if item["stage"] in {"creative", "listings"}
        )
    assert calls == 2


@pytest.mark.asyncio
async def test_listing_polish_failure_keeps_valid_draft_and_retry_reuses_it(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(update={"print_width": 400, "print_height": 500})
        )
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    run_id = str(value.run_id)
    create_run(value, f"manual-{run_id}")
    await research_run(run_id)
    assert await screen_and_select_run(run_id)
    polish_calls = 0

    async def fail_polish(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal polish_calls
        polish_calls += 1
        raise RuntimeError("temporary polish outage")

    monkeypatch.setattr(OpenAIService, "polish_listings", fail_polish)
    assert await generate_package_run(run_id)
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert run.status == RunStatus.AWAITING_APPROVAL.value
        assert run.listings == run.listing_generation_state["draft"]
        assert "temporary polish outage" in run.listing_generation_state["warning"]
        listing_calls = sum(call["stage"] == "listings" for call in run.provider_calls)
    assert await generate_package_run(run_id)
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert sum(call["stage"] == "listings" for call in run.provider_calls) == listing_calls
    assert polish_calls == 1


@pytest.mark.asyncio
async def test_featured_color_choice_survives_retry_and_regeneration(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    template = fixture_product_template().model_copy(
        update={"print_width": 400, "print_height": 500, "featured_variant_id": 1001}
    )
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    run_id = str(value.run_id)
    create_run(value, f"manual-{run_id}")
    await research_run(run_id)
    assert await screen_and_select_run(run_id)

    ranking_calls = 0
    original_listings = OpenAIService.listings
    listing_calls = 0

    async def rank_colors(self, preview, candidates):  # type: ignore[no-untyped-def]
        nonlocal ranking_calls
        ranking_calls += 1
        assert Image.open(io.BytesIO(preview)).width == 660
        chosen_id = 2 if ranking_calls == 1 else 1
        return ModelResult(
            ShirtColorRanking(scores=[
                ShirtColorScore(
                    candidate_id=item["candidate_id"],
                    score=95 if item["candidate_id"] == chosen_id else 60,
                    reason="Best overall appearance",
                )
                for item in candidates
            ]),
            {"model": "fixture", "estimated_cost_usd": 0.01},
        )

    async def fail_listings_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal listing_calls
        listing_calls += 1
        if listing_calls == 1:
            raise RuntimeError("temporary listing outage")
        return await original_listings(self, *args, **kwargs)

    monkeypatch.setattr(OpenAIService, "rank_shirt_colors", rank_colors)
    monkeypatch.setattr(OpenAIService, "listings", fail_listings_once)
    with pytest.raises(RuntimeError, match="temporary listing outage"):
        await generate_package_run(run_id)
    assert await generate_package_run(run_id)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.publication_template_snapshot["featured_variant_id"] == 1002
        assert run.version == 1
        assert len([item for item in run.artifacts if item.kind == "color-preview-v1"]) == 1
        assert [item["stage"] for item in run.provider_calls].count("featured_color") == 1
        assert ConfigurationRepository(session).get_template().featured_variant_id == 1001
    assert ranking_calls == 1

    assert await generate_package_run(run_id, regenerate=True)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.version == 2
        assert run.publication_template_snapshot["featured_variant_id"] == 1001
        assert len([item for item in run.artifacts if item.kind == "color-preview-v2"]) == 1
        assert automatic_approval_signal(run_id).expected_version == 2
    assert ranking_calls == 2


@pytest.mark.asyncio
async def test_vision_color_ranking_failure_uses_contrast_and_keeps_run_releasable(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(
                update={"print_width": 400, "print_height": 500, "featured_variant_id": 1001}
            )
        )

    async def ranking_unavailable(self, preview, candidates):  # type: ignore[no-untyped-def]
        raise RuntimeError("temporary vision outage")

    monkeypatch.setattr(OpenAIService, "rank_shirt_colors", ranking_unavailable)
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    await run_fixture_pipeline(value, get_settings())
    with session_scope() as session:
        run = RunRepository(session).get(str(value.run_id), full=True)
        assert run.status == RunStatus.AWAITING_APPROVAL.value
        assert run.publication_template_snapshot["featured_variant_id"] in {1001, 1002}
        production = next(item for item in run.artifacts if item.kind == "production-v1")
        choice = production.metadata_json["featured_color_selection"]
        assert choice["method"] == "contrast_fallback"
        assert "temporary vision outage" in choice["fallback_reason"]
        assert automatic_approval_signal(str(value.run_id)) is not None


@pytest.mark.asyncio
async def test_failed_deterministic_qa_skips_paid_visual_call(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(update={"print_width": 400, "print_height": 500})
        )
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    run_id = str(value.run_id)
    create_run(value, f"manual-{run_id}")
    await research_run(run_id)
    assert await screen_and_select_run(run_id)

    def failed_qa(*args, **kwargs):  # type: ignore[no-untyped-def]
        report = actual_deterministic_qa(*args, **kwargs)
        return report.model_copy(
            update={
                "passed": False,
                "issues": [
                    *report.issues,
                    QAIssue(code="fixture_failure", severity="error", message="Fixture failure"),
                ],
            }
        )

    async def no_visual(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("visual QA should be skipped")

    monkeypatch.setattr("merch.pipeline.deterministic_qa", failed_qa)
    monkeypatch.setattr(OpenAIService, "visual_qa", no_visual)
    assert not await generate_package_run(
        run_id, settings=get_settings().model_copy(update={"max_revision_attempts": 1})
    )
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert not any(call["stage"] == "visual_qa" for call in run.provider_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("code,source", [
    ("TYPOGRAPHY_LAYOUT", "prepress"),
    ("TYPOGRAPHY_READABILITY", "visual"),
    ("DISTRESS_PRINTABILITY", "prepress"),
    ("text_equality", "exact_text"),
])
async def test_effect_failure_resumes_brief_rewrite_without_illustration_edits(
    isolated_app, monkeypatch: pytest.MonkeyPatch, code: str, source: str
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(update={"print_width": 400, "print_height": 500})
        )
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    run_id = str(value.run_id)
    create_run(value, f"manual-{run_id}")
    await research_run(run_id)
    assert await screen_and_select_run(run_id)
    issue = QAIssue(code=code, severity="error", message="Effect obscures the lettering")
    original_artwork = OpenAIService.artwork
    original_visual = OpenAIService.visual_qa
    original_typography = OpenAIService.typography

    async def incorrect_slogan(self, slogan, brief):  # type: ignore[no-untyped-def]
        result = await original_typography(self, slogan, brief)
        return ModelResult(
            result.value.model_copy(update={"exact_text": "Wrong words", "line_breaks": ["Wrong words"]}),
            result.metadata,
        )

    def prepare(*args, **kwargs):  # type: ignore[no-untyped-def]
        prepared = actual_prepare_artwork(*args, **kwargs)
        return replace(prepared, issues=[issue] if source == "prepress" else [])

    async def visual(self, image, brief, deterministic, *, effects=None):  # type: ignore[no-untyped-def]
        assert source == "visual"
        assert effects and effects["renderer_version"]
        return ModelResult(
            deterministic.model_copy(update={"passed": False, "issues": [issue]}),
            {"model": "fixture", "estimated_cost_usd": 0.0},
        )

    async def unexpected_async(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("An effects failure must resume without buying an illustration edit")

    def unexpected_prepare(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("A saved production artifact must be reused")

    monkeypatch.setattr("merch.pipeline.prepare_artwork", prepare)
    monkeypatch.setattr(OpenAIService, "visual_qa", visual)
    monkeypatch.setattr(OpenAIService, "revise_artwork", unexpected_async)
    if source == "exact_text":
        monkeypatch.setattr(OpenAIService, "typography", incorrect_slogan)
    assert not await generate_package_run(run_id)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        production = [item for item in run.artifacts if item.kind == "production-v1"]
        assert len(production) == 1
        assert code in {item["code"] for item in production[0].metadata_json["qa"]["issues"]}
        assert production[0].metadata_json["artwork_effects"]["renderer_version"]
        assert production[0].metadata_json["typography_spec"] == run.typography_spec
        assert run.status == RunStatus.AWAITING_BRIEF_REVISION.value
        assert code in run.error
        assert run.qa_report is None and not run.publishes and not run.approvals
        original_research = run.research_report
        original_calls = list(run.provider_calls)
        # Emulate an interruption after the production artifact was saved.
        run.status = RunStatus.QA.value
    monkeypatch.setattr("merch.pipeline.prepare_artwork", unexpected_prepare)
    monkeypatch.setattr(OpenAIService, "visual_qa", unexpected_async)
    monkeypatch.setattr(OpenAIService, "artwork", unexpected_async)
    assert not await generate_package_run(run_id)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.AWAITING_BRIEF_REVISION.value
        assert run.provider_calls == original_calls
        assert len([item for item in run.artifacts if item.kind == "production-v1"]) == 1

    async def rewrite(self, concept, brief, issues, colors, *, recovery_context=None):  # type: ignore[no-untyped-def]
        failed = next(item for item in issues if item.code == code)
        assert "Saved rendering settings" in failed.recommended_fix
        assert "renderer_version" in failed.recommended_fix
        assert "typography_spec" in failed.recommended_fix
        return ModelResult(
            brief.model_copy(update={"typography_style": "Straight, clean lettering without wear"}),
            {"model": "fixture", "estimated_cost_usd": 0.1},
        )

    monkeypatch.setattr(OpenAIService, "revise_brief", rewrite)
    assert await rewrite_failed_brief_run(run_id) == RunStatus.PENDING.value
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.version == 2
        assert run.research_report == original_research
        assert run.typography_spec is None
        assert not run.approvals and not run.publishes
    monkeypatch.setattr("merch.pipeline.prepare_artwork", actual_prepare_artwork)
    monkeypatch.setattr(OpenAIService, "visual_qa", original_visual)
    monkeypatch.setattr(OpenAIService, "artwork", original_artwork)
    monkeypatch.setattr(OpenAIService, "typography", original_typography)
    assert await generate_package_run(run_id)
    signal = automatic_approval_signal(run_id)
    assert signal is not None and signal.expected_version == 2
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.AWAITING_APPROVAL.value
        assert run.research_report == original_research
        assert run.qa_report["passed"]
        assert {item.kind for item in run.artifacts} >= {"production-v1", "production-v2"}
        assert not run.publishes and not run.approvals


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(DesignMode))
async def test_effected_artwork_reaches_approval_and_retries_reuse_it(
    isolated_app, monkeypatch: pytest.MonkeyPatch, mode: DesignMode
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(update={"print_width": 400, "print_height": 500})
        )
    original_creative = OpenAIService.creative
    original_typography = OpenAIService.typography
    original_listings = OpenAIService.listings
    original_polish = OpenAIService.polish_listings

    async def creative(self, concept, context):  # type: ignore[no-untyped-def]
        result = await original_creative(self, concept, context)
        brief = result.value.model_copy(update={
            "design_mode": mode,
            "slogan": None if mode == DesignMode.ILLUSTRATION else result.value.slogan,
            "artwork_distress_level": 0 if mode == DesignMode.TYPOGRAPHY else 3,
        })
        return ModelResult(brief, result.metadata)

    async def typography(self, slogan, brief):  # type: ignore[no-untyped-def]
        result = await original_typography(self, slogan, brief)
        spec = result.value.model_copy(update={
            "text_arc_or_shape": "up", "distress_level": 3,
        })
        return ModelResult(spec, result.metadata)

    effective_settings = []
    copy_settings = []

    async def visual(self, image, brief, deterministic, *, effects=None):  # type: ignore[no-untyped-def]
        assert effects
        effective_settings.append(effects)
        return ModelResult(deterministic, {"model": "fixture", "estimated_cost_usd": 0.0})

    async def listings(self, context, brief, research):  # type: ignore[no-untyped-def]
        copy_settings.append(context["artwork_effects"])
        return await original_listings(self, context, brief, research)

    async def polish(self, context, brief, drafts):  # type: ignore[no-untyped-def]
        copy_settings.append(context["artwork_effects"])
        return await original_polish(self, context, brief, drafts)

    monkeypatch.setattr(OpenAIService, "creative", creative)
    monkeypatch.setattr(OpenAIService, "typography", typography)
    monkeypatch.setattr(OpenAIService, "visual_qa", visual)
    monkeypatch.setattr(OpenAIService, "listings", listings)
    monkeypatch.setattr(OpenAIService, "polish_listings", polish)
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    run_id = str(value.run_id)
    create_run(value, f"manual-{run_id}")
    await research_run(run_id)
    assert await screen_and_select_run(run_id)
    assert await generate_package_run(run_id)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.AWAITING_APPROVAL.value
        production = next(item for item in run.artifacts if item.kind == "production-v1")
        effects = production.metadata_json["artwork_effects"]
        assert effective_settings == [effects]
        assert copy_settings == [effects, effects]
        assert effects["distress"]["scope"] == (
            "text" if mode == DesignMode.TYPOGRAPHY else "design"
        )
        assert effects["distress"]["requested_level"] == 3
        assert production.metadata_json["featured_color_selection"]["artwork_sha256"] == production.sha256
        artifact_ids = [item.id for item in run.artifacts]
        original_calls = list(run.provider_calls)

    async def unexpected(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Retry must reuse the saved artwork and effects")

    monkeypatch.setattr(OpenAIService, "creative", unexpected)
    monkeypatch.setattr(OpenAIService, "typography", unexpected)
    monkeypatch.setattr(OpenAIService, "artwork", unexpected)
    monkeypatch.setattr(OpenAIService, "visual_qa", unexpected)
    monkeypatch.setattr(OpenAIService, "rank_shirt_colors", unexpected)
    assert await generate_package_run(run_id)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.AWAITING_APPROVAL.value
        assert run.version == 1
        assert run.provider_calls == original_calls
        assert [item.id for item in run.artifacts] == artifact_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("defect_code", ["FINE_DETAIL", "ANATOMY", "READABILITY", "CONTRAST"])
async def test_repeated_visual_defect_pauses_for_revised_brief(
    isolated_app, monkeypatch: pytest.MonkeyPatch, defect_code: str
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(update={"print_width": 400, "print_height": 500})
        )
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    run_id = str(value.run_id)
    create_run(value, f"manual-{run_id}")
    await research_run(run_id)
    assert await screen_and_select_run(run_id)
    visual_calls = 0
    edit_calls = 0

    async def repeated_visual(self, image, brief, deterministic, *, effects=None):  # type: ignore[no-untyped-def]
        nonlocal visual_calls
        visual_calls += 1
        report = deterministic.model_copy(
            update={
                "passed": False,
                "issues": [
                    QAIssue(code=defect_code, severity="error", message="Visible production defect")
                ],
            }
        )
        return ModelResult(report, {"model": "fixture", "estimated_cost_usd": 0.0})

    async def same_art(self, image, brief, issues):  # type: ignore[no-untyped-def]
        nonlocal edit_calls
        edit_calls += 1
        return image, {"model": "fixture", "estimated_cost_usd": 0.0}

    monkeypatch.setattr(OpenAIService, "visual_qa", repeated_visual)
    monkeypatch.setattr(OpenAIService, "revise_artwork", same_art)
    assert not await generate_package_run(run_id)
    assert (visual_calls, edit_calls) == (2, 1)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.AWAITING_BRIEF_REVISION.value
        assert defect_code in (run.error or "")
        assert run.qa_report is None
        assert not run.approvals and not run.publishes
        assert len([item for item in run.artifacts if item.kind == "production-v1"]) == 2
        from merch.schemas import CreativeBrief

        original = CreativeBrief.model_validate(run.creative_brief)
    with pytest.raises(ValueError, match="revised creative brief is required"):
        await retry_failed_artwork_run(run_id)
    with pytest.raises(ValueError, match="Revise the creative brief"):
        await retry_failed_artwork_run(run_id, revised_brief=original)

    class FakeTemporal:
        async def start_workflow(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return None

    async def fake_temporal_client(settings):  # type: ignore[no-untyped-def]
        return FakeTemporal()

    monkeypatch.setattr("merch.temporal.temporal_client", fake_temporal_client)
    revised = original.model_copy(
        update={"generation_brief": original.generation_brief + " Use broad detached shapes."}
    )
    restarted = await retry_failed_artwork_run(run_id, revised_brief=revised)
    assert restarted.reuse_brief

    async def passing_visual(self, image, brief, deterministic, *, effects=None):  # type: ignore[no-untyped-def]
        assert brief.generation_brief == revised.generation_brief
        return ModelResult(deterministic, {"model": "fixture", "estimated_cost_usd": 0.0})

    async def no_creative(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Revised brief should be reused without another creative call")

    monkeypatch.setattr(OpenAIService, "visual_qa", passing_visual)
    monkeypatch.setattr(OpenAIService, "creative", no_creative)
    assert await generate_package_run(run_id, regenerate=True, preserve_brief=True)
    with session_scope() as session:
        run = RunRepository(session).get(run_id)
        assert run.version == 2
        assert run.status == RunStatus.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_contrast_excludes_color_from_only_this_runs_publication(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    Base.metadata.create_all(get_engine())
    template = fixture_product_template().model_copy(
        update={"print_width": 400, "print_height": 500, "featured_variant_id": 1001}
    )
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    run_id = str(value.run_id)
    create_run(value, f"manual-{run_id}")
    await research_run(run_id)
    assert await screen_and_select_run(run_id)

    def forest_contrast(*args, **kwargs):  # type: ignore[no-untyped-def]
        report = actual_deterministic_qa(*args, **kwargs)
        return report.model_copy(
            update={
                "passed": False,
                "issues": [
                    *report.issues,
                    QAIssue(
                        code="contrast",
                        severity="error",
                        message="Artwork lacks contrast with #1F3A32",
                        affected_shirt_colors=["#1F3A32"],
                    ),
                ],
            }
        )

    async def visual_qa(self, image, brief, deterministic, *, effects=None):  # type: ignore[no-untyped-def]
        assert "#1F3A32" not in brief.shirt_colors
        assert deterministic.passed
        return ModelResult(deterministic, {"model": "fixture", "estimated_cost_usd": 0.0})

    async def no_edit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("A color-specific contrast finding must not buy an artwork edit")

    monkeypatch.setattr("merch.pipeline.deterministic_qa", forest_contrast)
    monkeypatch.setattr(OpenAIService, "visual_qa", visual_qa)
    monkeypatch.setattr(OpenAIService, "revise_artwork", no_edit)
    assert await generate_package_run(run_id)
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.AWAITING_APPROVAL.value
        assert run.excluded_shirt_colors == ["#1F3A32"]
        assert run.template_snapshot["variants"][1]["enabled"]
        assert [
            item["variant_id"] for item in run.publication_template_snapshot["variants"]
            if item["enabled"]
        ] == [1001]
        assert {(item["channel"], item["variant_id"]) for item in run.price_quotes} == {
            (channel.value, 1001) for channel in Channel
        }
        assert run.qa_report["passed"]
    with session_scope() as session:
        assert ConfigurationRepository(session).get_template().variants[1].enabled

    captured: list[dict[str, object]] = []
    original_payload = PrintifyClient.product_payload

    def product_payload(self, template, listing, quotes, artwork_upload_id):  # type: ignore[no-untyped-def]
        payload = original_payload(self, template, listing, quotes, artwork_upload_id)
        captured.append(payload)
        return payload

    monkeypatch.setattr(PrintifyClient, "product_payload", product_payload)
    record_approval(
        run_id,
        ApprovalSignal(channels=[Channel.ETSY], expected_version=1, ip_attested=False, actor="test"),
    )
    from merch.schemas import PublishStatus

    assert await publish_channel_run(run_id, Channel.ETSY) == PublishStatus.DRY_RUN
    assert [item["id"] for item in captured[0]["variants"]] == [1001]


@pytest.mark.asyncio
async def test_live_etsy_publish_waits_for_storefront_verification_and_retries_without_republishing(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def prepared(*args, **kwargs):  # type: ignore[no-untyped-def]
        return []

    # This test isolates storefront handoff/retry state; content gates have
    # full real-image integration coverage in test_mockup_pipeline.
    monkeypatch.setattr("merch.pipeline.prepare_mockups", prepared)
    monkeypatch.setattr("merch.pipeline.verify_printify_product", lambda *args: "mockup")
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    settings = get_settings().model_copy(
        update={
            "publish_mode": "live", "printify_api_token": SecretStr("fixture-token"),
            "etsy_native_publish_grace_seconds": 0,
        }
    )
    Base.metadata.create_all(get_engine())
    template = fixture_product_template().model_copy(
        update={"print_width": 400, "print_height": 500, "featured_variant_id": 1001}
    )
    with session_scope() as session:
        ConfigurationRepository(session).save_template(template)
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    await run_fixture_pipeline(value, get_settings())
    run_id = str(value.run_id)
    record_approval(
        run_id,
        ApprovalSignal(
            channels=[Channel.ETSY], expected_version=1, ip_attested=False, actor="test"
        ),
    )
    publish_calls = 0

    async def validated(self, template):  # type: ignore[no-untyped-def]
        return template

    async def uploaded(self, filename, data):  # type: ignore[no-untyped-def]
        return {"id": "image-1"}

    async def created(self, shop_id, payload):  # type: ignore[no-untyped-def]
        assert sum(item["is_default"] for item in payload["variants"]) == 1
        assert next(item for item in payload["variants"] if item["is_default"])["id"] == 1001
        return {"id": "product-1", **payload}

    async def published(self, shop_id, product_id):  # type: ignore[no-untyped-def]
        nonlocal publish_calls
        publish_calls += 1
        return {"status": "accepted"}

    async def closed(self):  # type: ignore[no-untyped-def]
        return None

    async def missing_external(self, shop_id, product_id):  # type: ignore[no-untyped-def]
        return {"id": product_id, "is_locked": False, "external": None}

    monkeypatch.setattr(PrintifyClient, "validate_template", validated)
    monkeypatch.setattr(PrintifyClient, "upload_image", uploaded)
    monkeypatch.setattr(PrintifyClient, "create_product", created)
    monkeypatch.setattr(PrintifyClient, "publish", published)
    monkeypatch.setattr(PrintifyClient, "close", closed)
    monkeypatch.setattr(PrintifyClient, "product", missing_external)
    from merch.schemas import PublishStatus

    assert await publish_channel_run(run_id, Channel.ETSY, settings) == PublishStatus.RECONCILIATION_REQUIRED
    finish_publishing(run_id, [PublishStatus.RECONCILIATION_REQUIRED])
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.VERIFICATION_REQUIRED.value
        assert run.publishes[0].printify_product_id == "product-1"
        assert "publish_response" in (run.publishes[0].response_data or {})
    assert publish_calls == 1

    async def verified(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"id": "product-1", "external": {"id": "7"}}, {
            "listing_id": 7,
            "featured_variant_id": 1001,
            "featured_image_id": 9,
            "variant_count": 2,
        }

    monkeypatch.setattr("merch.pipeline._verify_etsy_publish", verified)
    async def linked_external(self, shop_id, product_id):  # type: ignore[no-untyped-def]
        return {"id": product_id, "external": {"id": "7"}}

    monkeypatch.setattr(PrintifyClient, "product", linked_external)
    assert await publish_channel_run(run_id, Channel.ETSY, settings) == PublishStatus.SUCCEEDED
    assert publish_calls == 1
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.publishes[0].status == PublishStatus.SUCCEEDED.value
        assert run.publishes[0].external_product_id == "7"

    assert await repair_published_etsy_listing(run_id, settings) == 7
    assert publish_calls == 1
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.publishes[0].response_data["verification"]["listing_id"] == 7

    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        run.status = RunStatus.VERIFICATION_REQUIRED.value
        run.publishes[0].status = PublishStatus.RECONCILIATION_REQUIRED.value
        run.publishes[0].response_data = {}

    dry_settings = settings.model_copy(update={"publish_mode": "dry_run"})
    assert (
        await publish_channel_run(run_id, Channel.ETSY, dry_settings)
        == PublishStatus.RECONCILIATION_REQUIRED
    )
    assert publish_calls == 1

    async def existing_product(self, shop_id, product_id):  # type: ignore[no-untyped-def]
        return {"id": product_id, "external": {"id": "7"}}

    monkeypatch.setattr(PrintifyClient, "product", existing_product)
    assert await publish_channel_run(run_id, Channel.ETSY, settings) == PublishStatus.SUCCEEDED
    assert publish_calls == 1


@pytest.mark.asyncio
async def test_direct_etsy_fallback_marks_run_published_only_after_verified_link(
    isolated_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def prepared(*args, **kwargs):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr("merch.pipeline.prepare_mockups", prepared)
    monkeypatch.setattr("merch.pipeline.verify_printify_product", lambda *args: "mockup")
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_EDGE", 512)
    monkeypatch.setattr("merch.domain.prepress.MAX_GENERATION_PIXELS", 262_144)
    settings = get_settings().model_copy(update={
        "publish_mode": "live", "printify_api_token": SecretStr("fixture-token"),
        "etsy_native_publish_grace_seconds": 0,
    })
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        ConfigurationRepository(session).save_template(
            fixture_product_template().model_copy(update={
                "print_width": 400, "print_height": 500, "featured_variant_id": 1001,
            })
        )
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=False)
    await run_fixture_pipeline(value, get_settings())
    run_id = str(value.run_id)
    record_approval(run_id, ApprovalSignal(
        channels=[Channel.ETSY], expected_version=1, ip_attested=False, actor="system",
    ))

    async def validated(self, template):  # type: ignore[no-untyped-def]
        return template

    async def uploaded(self, filename, data):  # type: ignore[no-untyped-def]
        return {"id": "art-1"}

    async def created(self, shop_id, payload):  # type: ignore[no-untyped-def]
        return {"id": "product-1", **payload}

    async def published(self, shop_id, product_id):  # type: ignore[no-untyped-def]
        return {}

    async def product(self, shop_id, product_id):  # type: ignore[no-untyped-def]
        return {"id": product_id, "is_locked": False, "external": None}

    async def closed(self):  # type: ignore[no-untyped-def]
        return None

    async def verified_direct(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "id": "product-1", "external": {"id": "99"},
            "variants": [{"sku": "black-m"}, {"sku": "forest-l"}],
        }, {"listing_id": 99, "featured_image_id": 7, "publication_mode": "direct_etsy_fallback"}

    monkeypatch.setattr(PrintifyClient, "validate_template", validated)
    monkeypatch.setattr(PrintifyClient, "upload_image", uploaded)
    monkeypatch.setattr(PrintifyClient, "create_product", created)
    monkeypatch.setattr(PrintifyClient, "publish", published)
    monkeypatch.setattr(PrintifyClient, "product", product)
    monkeypatch.setattr(PrintifyClient, "close", closed)
    monkeypatch.setattr("merch.pipeline._direct_etsy_fallback", verified_direct)
    assert await publish_channel_run(run_id, Channel.ETSY, settings) == PublishStatus.SUCCEEDED
    finish_publishing(run_id, [PublishStatus.SUCCEEDED])
    with session_scope() as session:
        run = RunRepository(session).get(run_id, full=True)
        assert run.status == RunStatus.PUBLISHED.value
        assert run.publishes[0].external_product_id == "99"
        mapping = session.query(ProductMappingRecord).one()
        assert mapping.marketplace_product_id == "99"
        assert mapping.marketplace_listing_id == "99"
