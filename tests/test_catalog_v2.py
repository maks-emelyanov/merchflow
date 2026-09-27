from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from merch.catalog_pipeline import (
    _reusable_ip_report,
    attempt_catalog_opportunity,
    research_catalog_run,
)
from merch.catalog_publisher import publish_catalog_run
from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.domain.catalog import UnsupportedDecorationMethod, fixture_catalog, placement_for
from merch.domain.catalog_prepress import validate_surface_artwork
from merch.domain.etsy_inventory import build_generic_etsy_inventory
from merch.domain.opportunities import evidence_is_fresh, reduce_variation_axes
from merch.domain.originality import evaluate_originality
from merch.domain.pricing import competitive_price
from merch.models import Base
from merch.pipeline import create_run
from merch.repository import RunRepository
from merch.schemas import (
    CatalogVariant,
    MarketplaceListing,
    MarketplaceSource,
    OriginalityVisionAssessment,
    PriceDecision,
    PrintSurface,
    ProductPlanV2,
    RunInput,
    RunStatus,
    SurfaceArtwork,
)
from merch.services.browser_session import (
    designer_product_identity,
    embedded_product_identities,
    extract_designer_variant_costs,
    extract_variant_costs,
    printify_session_expiry,
)
from merch.services.catalog import (
    claim_catalog_refresh,
    discover_catalog_refresh,
    finalize_catalog_refresh,
    refresh_catalog_batch,
)
from merch.services.etsy_catalog_publisher import (
    _variation_value_images,
    publish_direct_catalog_etsy,
)
from merch.services.marketplace_research import (
    contains_access_challenge,
    extract_listing_snapshot,
)
from merch.services.printify import PrintifyClient
from merch.services.reference_assets import select_reference_listings

FIXTURES = Path(__file__).parent / "fixtures"


def _png(color: tuple[int, int, int, int] = (255, 0, 0, 255)) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (64, 64), color).save(output, "PNG")
    return output.getvalue()


def test_catalog_ip_report_reuse_is_scoped_to_opportunity() -> None:
    payload = {
        "status": "pass",
        "risk_score": 0,
        "searched_terms": ["safe phrase"],
        "matches": [],
        "uspto_search_url": "https://tmsearch.uspto.gov/",
        "notes": [],
        "legal_clearance": False,
    }
    calls = [
        {
            "stage": "catalog_ip_screen",
            "opportunity_id": "opportunity-a",
            "model": "test",
        }
    ]

    reusable = _reusable_ip_report(payload, calls, "opportunity-a")

    assert reusable is not None
    assert reusable.status == "pass"
    assert _reusable_ip_report(payload, calls, "opportunity-b") is None
    assert (
        _reusable_ip_report(
            payload,
            [{"stage": "catalog_ip_screen", "model": "legacy"}],
            "opportunity-a",
        )
        is None
    )


def test_reference_selection_prefers_image_backed_comparables() -> None:
    missing = SimpleNamespace(external_listing_id="missing", image_urls=[])
    first = SimpleNamespace(external_listing_id="first", image_urls=["https://example.com/1"])
    second = SimpleNamespace(external_listing_id="second", image_urls=["https://example.com/2"])
    third = SimpleNamespace(external_listing_id="third", image_urls=["https://example.com/3"])
    opportunity = SimpleNamespace(comparable_listings=[missing, first, second, third])

    selected = select_reference_listings(opportunity, fake=False)  # type: ignore[arg-type]

    assert [item.external_listing_id for item in selected] == ["first", "second", "third"]
    assert select_reference_listings(  # type: ignore[arg-type]
        opportunity, fake=True
    ) == [missing, first, second]


def test_reference_selection_requires_three_image_backed_comparables() -> None:
    opportunity = SimpleNamespace(
        comparable_listings=[
            SimpleNamespace(external_listing_id="first", image_urls=["https://example.com/1"]),
            SimpleNamespace(external_listing_id="second", image_urls=["https://example.com/2"]),
            SimpleNamespace(external_listing_id="missing", image_urls=[]),
        ]
    )

    with pytest.raises(RuntimeError, match="fewer than three"):
        select_reference_listings(opportunity, fake=False)  # type: ignore[arg-type]


def test_method_capabilities_fail_closed() -> None:
    assert placement_for("embroidery", "front") == "restricted_palette"
    assert placement_for("sublimation", "mug_wrap") == "full_bleed"
    with pytest.raises(UnsupportedDecorationMethod):
        placement_for("future_magic_print", "front")


def test_browser_snapshot_extracts_explicit_sales_and_delivered_price() -> None:
    html = """
    <html><head><meta property="og:image" content="https://images.example/item.png">
    <script type="application/ld+json">{
      "@type":"Product","name":"Ceramic Trail Mug","sku":"MUG-1",
      "offers":{"price":"18.00"},
      "aggregateRating":{"ratingValue":"4.8","reviewCount":"250"},
      "brand":{"name":"A Seller"}
    }</script></head><body>Bestseller — free shipping — 1,200 sold</body></html>
    """
    snapshot = extract_listing_snapshot(
        source=MarketplaceSource.ETSY,
        url="https://www.etsy.com/listing/123456/ceramic-trail-mug",
        html=html,
        product_type="Ceramic Mug",
        collected_at=datetime.now(UTC),
    )
    assert snapshot.external_listing_id == "123456"
    assert snapshot.delivered_price_cents == 1800
    assert snapshot.confidence == 90
    assert any(item.kind == "sold_count" and item.explicit for item in snapshot.sales_signals)


@pytest.mark.parametrize(
    ("source", "fixture", "url", "expected_shipping", "explicit"),
    [
        (
            MarketplaceSource.ETSY,
            "etsy.html",
            "https://www.etsy.com/listing/101/mug",
            0,
            True,
        ),
        (
            MarketplaceSource.AMAZON_US,
            "amazon_us.html",
            "https://www.amazon.com/dp/B0ABCDEF12",
            0,
            True,
        ),
        (
            MarketplaceSource.TIKTOK_SHOP,
            "tiktok_shop.html",
            "https://shop.tiktok.com/us/view/product/778899",
            0,
            True,
        ),
        (
            MarketplaceSource.WALMART,
            "walmart.html",
            "https://www.walmart.com/ip/mug/987654321",
            499,
            False,
        ),
        (
            MarketplaceSource.EBAY,
            "ebay.html",
            "https://www.ebay.com/itm/mug/33557799",
            None,
            True,
        ),
    ],
)
def test_saved_marketplace_page_contracts(
    source: MarketplaceSource,
    fixture: str,
    url: str,
    expected_shipping: int | None,
    explicit: bool,
) -> None:
    snapshot = extract_listing_snapshot(
        source=source,
        url=url,
        html=(FIXTURES / "marketplaces" / fixture).read_text(),
        product_type="Ceramic Mug",
        collected_at=datetime.now(UTC),
    )
    assert snapshot.displayed_price_cents is not None
    assert snapshot.shipping_price_cents == expected_shipping
    assert any(item.explicit for item in snapshot.sales_signals) is explicit


def test_printify_dashboard_contract_identity_costs_and_selector_drift() -> None:
    import json

    payload = json.loads((FIXTURES / "printify" / "dashboard.json").read_text())
    assert embedded_product_identities([payload]) == {(68, 9)}
    assert extract_variant_costs({10001, 10002}, rows=[], embedded_payloads=[payload]) == {
        10001: 525,
        10002: 650,
    }
    # A renamed/missing row selector can fall back to immutable embedded state.
    assert extract_variant_costs(
        {10001},
        rows=[{"variant": "selector-drift", "price": "$1.00"}],
        embedded_payloads=[payload],
    ) == {10001: 525}


def test_printify_designer_contract_identity_and_component_costs() -> None:
    payload = {
        "blueprint_id": 5,
        "print_provider": {
            "id": 3,
            "available": True,
            "variants": [
                {
                    "id": 17390,
                    "available": True,
                    "costs": [
                        {"blank": 530, "printing": 383, "fee": 0, "result": 1499},
                        {"blank": 530, "printing": 450, "fee": 25, "result": 1699},
                    ],
                },
                {
                    "id": 17391,
                    "available": False,
                    "costs": [{"blank": 530, "printing": 383, "fee": 0}],
                },
            ],
        },
    }
    assert designer_product_identity(payload) == (5, 3)
    assert extract_designer_variant_costs(payload, {17390, 17391}) == {17390: 913}


def test_printify_session_expiry_prefers_authentication_cookie() -> None:
    state = {
        "cookies": [
            {"name": "__cf_bm", "expires": 1000},
            {"name": "connect.sid", "expires": 5000},
            {"name": "fyul_sess", "expires": 9000},
        ]
    }
    assert printify_session_expiry(state) == datetime.fromtimestamp(5000, UTC)


def test_challenge_and_stale_evidence_are_hard_failures() -> None:
    challenge = (FIXTURES / "marketplaces" / "challenge.html").read_text()
    assert contains_access_challenge(challenge)
    snapshot = extract_listing_snapshot(
        source=MarketplaceSource.ETSY,
        url="https://www.etsy.com/listing/101/mug",
        html=(FIXTURES / "marketplaces" / "etsy.html").read_text(),
        product_type="Ceramic Mug",
        collected_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert not evidence_is_fresh(
        snapshot, now=datetime.now(UTC), direct_hours=24, fallback_hours=72
    )


def test_competitive_price_uses_highest_profitable_99_undercut() -> None:
    decision = competitive_price(
        variant_id=1,
        production_cost_cents=300,
        fulfillment_shipping_cents=100,
        customer_shipping_cents=0,
        percent_fee=0.10,
        fixed_fee_cents=20,
        benchmark_median_delivered_cents=1999,
    )
    assert decision.item_price_cents == 1899
    assert decision.undercut_status == "true"
    assert decision.contribution_margin >= 0.40


def test_variant_reduction_keeps_a_complete_option_matrix() -> None:
    surface = PrintSurface(
        position="front",
        decoration_method="dtg",
        width=64,
        height=64,
        placement="placed",
    )
    variants = [
        CatalogVariant(
            variant_id=index + 1,
            title=f"{color} / {size}",
            options={"Color": color, "Size": size},
            surfaces=[surface],
            production_cost_cents=500 + index,
            shipping_cost_cents=300,
        )
        for index, (color, size) in enumerate(
            [("A", "S"), ("A", "M"), ("B", "S"), ("B", "M"), ("C", "S")]
        )
    ]

    selected = reduce_variation_axes(variants, max_axes=2, maximum_products=5)

    assert len(selected) == 4
    assert {(item.options["Color"], item.options["Size"]) for item in selected} == {
        ("A", "S"),
        ("A", "M"),
        ("B", "S"),
        ("B", "M"),
    }


def test_variation_images_cover_values_from_multi_variant_mockups() -> None:
    surface = PrintSurface(
        position="front",
        decoration_method="dtg",
        width=64,
        height=64,
        placement="placed",
    )
    variants = {
        index: CatalogVariant(
            variant_id=index,
            title=color,
            options={"color": color},
            surfaces=[surface],
        )
        for index, color in enumerate(("Black", "Blue", "Red"), start=1)
    }
    gallery = [
        {"variant_ids": [1, 2, 3]},
        {"variant_ids": [1, 2, 3]},
    ]

    mapped = _variation_value_images(gallery, [101, 102], variants, "color")

    assert set(mapped) == {"Black", "Blue", "Red"}
    assert set(mapped.values()) == {101, 102}


def test_generic_three_axis_inventory_and_multi_surface_printify_payload() -> None:
    surfaces = [
        PrintSurface(
            position="front",
            decoration_method="dtg",
            width=64,
            height=64,
            placement="placed",
        ),
        PrintSurface(
            position="back",
            decoration_method="dtg",
            width=64,
            height=64,
            placement="placed",
        ),
    ]
    variants = [
        CatalogVariant(
            variant_id=1,
            title="Red / S / Matte",
            options={"Color": "Red", "Size": "S", "Finish": "Matte"},
            surfaces=surfaces,
            production_cost_cents=500,
            shipping_cost_cents=300,
        )
    ]
    prices = [
        PriceDecision(
            variant_id=1,
            item_price_cents=1999,
            customer_shipping_cents=0,
            production_cost_cents=500,
            fulfillment_shipping_cents=300,
            estimated_fee_cents=200,
            contribution_margin=0.49,
            undercut_status="true",
            benchmark_median_delivered_cents=2100,
            reason="test",
        )
    ]
    inventory = build_generic_etsy_inventory(
        variants,
        prices,
        {"Color": 513, "Finish": 514, "Size": 516},
        quantity=10,
        readiness_state_id=2,
    )
    assert len(inventory["products"][0]["property_values"]) == 3

    plan = ProductPlanV2(
        blueprint_id=1,
        print_provider_id=2,
        product_title="Two-sided item",
        variants=variants,
        surface_artworks=[
            SurfaceArtwork(
                surface_signature=surface.signature,
                artifact_id=str(index + 1),
                placement="placed",
            )
            for index, surface in enumerate(surfaces)
        ],
        featured_variant_id=1,
        gallery_variant_ids=[1],
        etsy_profile={
            "taxonomy_id": 1,
            "shipping_profile_id": 1,
            "return_policy_id": 1,
            "readiness_state_id": 2,
            "production_partner_ids": [1],
            "max_variations_supported": 3,
            "variation_property_ids": {"Color": 513, "Finish": 514, "Size": 516},
        },
        generated_at=datetime.now(UTC),
    )
    listing = MarketplaceListing(
        channel="etsy",
        title="Original Two-Sided Item",
        short_description="Original item",
        long_description="Original item. Printify is the production partner.",
        tags=["original item"],
        bullet_points=[],
        alt_text="Original item",
        target_customer="gift buyer",
        gift_occasions=[],
        seo_meta_title="Original item",
        seo_meta_description="Original item",
    )
    settings = get_settings()
    payload = PrintifyClient(settings).catalog_product_payload(
        plan,
        listing,
        prices,
        {surface.signature: f"upload-{index}" for index, surface in enumerate(surfaces)},
    )
    assert len(payload["print_areas"]) == 1
    assert {item["position"] for item in payload["print_areas"][0]["placeholders"]} == {
        "front",
        "back",
    }
    assert {item["decoration_method"] for item in payload["print_areas"][0]["placeholders"]} == {
        "dtg"
    }
    with pytest.raises(ValueError, match="at most 100 enabled variants"):
        PrintifyClient(settings).catalog_product_payload(
            plan.model_copy(update={"variants": plan.variants * 101}),
            listing,
            prices,
            {surface.signature: f"upload-{index}" for index, surface in enumerate(surfaces)},
        )
    remote = {
        "variants": [
            {"id": 1, "price": 2200, "is_enabled": True},
            {"id": 2, "price": 2300, "is_enabled": True},
        ],
        "print_areas": [
            {
                "variant_ids": [1, 2],
                "placeholders": [
                    {
                        "position": "front",
                        "decoration_method": "dtg",
                        "images": [
                            {"id": "upload-0", "x": 0.5, "name": "read-only.png"},
                            {"id": "auto-text", "x": 0.5, "name": "text_layer.svg"},
                        ],
                    }
                ],
            }
        ],
    }
    update = PrintifyClient(settings).catalog_product_update_payload(
        plan,
        listing,
        prices,
        {surface.signature: f"upload-{index}" for index, surface in enumerate(surfaces)},
        remote,
    )
    variants_by_id = {item["id"]: item for item in update["variants"]}
    assert variants_by_id[1]["is_enabled"] is True
    assert variants_by_id[2]["is_enabled"] is False
    assert {value for area in update["print_areas"] for value in area["variant_ids"]} == {1, 2}
    retained_images = update["print_areas"][-1]["placeholders"][0]["images"]
    assert retained_images == [{"id": "upload-0", "x": 0.5}]
    assert all(
        "name" not in image
        for area in update["print_areas"]
        for placeholder in area["placeholders"]
        for image in placeholder["images"]
    )


def test_prepress_and_originality_gates_block_bad_outputs() -> None:
    placed = PrintSurface(
        position="front",
        decoration_method="dtg",
        width=64,
        height=64,
        placement="placed",
    )
    assert "transparent background" in " ".join(validate_surface_artwork(_png(), placed))
    identical = _png((20, 40, 60, 255))
    report = evaluate_originality(
        generated_image=identical,
        generated_wording="same exact competitor slogan",
        references=[("listing-1", identical, "same exact competitor slogan")],
        vision=OriginalityVisionAssessment(originality_score=95, copying_risk=5, reasons=[]),
    )
    assert report.passed is False
    assert report.findings[0].perceptual_hash_distance == 0


@pytest.mark.asyncio
async def test_direct_etsy_catalog_verifies_served_gallery_before_activation() -> None:
    surface = PrintSurface(
        position="front",
        decoration_method="dtg",
        width=64,
        height=64,
        placement="placed",
    )
    variant = CatalogVariant(
        variant_id=1,
        title="One size",
        options={},
        surfaces=[surface],
        production_cost_cents=500,
        shipping_cost_cents=300,
    )
    plan = ProductPlanV2(
        blueprint_id=1,
        print_provider_id=2,
        product_title="Original item",
        variants=[variant],
        surface_artworks=[
            SurfaceArtwork(
                surface_signature=surface.signature,
                artifact_id=str(uuid4()),
                placement="placed",
            )
        ],
        featured_variant_id=1,
        gallery_variant_ids=[1],
        etsy_profile={
            "taxonomy_id": 1,
            "shipping_profile_id": 1,
            "return_policy_id": 1,
            "readiness_state_id": 2,
            "production_partner_ids": [1],
            "variation_property_ids": {},
        },
        generated_at=datetime.now(UTC),
    )
    price = PriceDecision(
        variant_id=1,
        item_price_cents=1999,
        customer_shipping_cents=0,
        production_cost_cents=500,
        fulfillment_shipping_cents=300,
        estimated_fee_cents=200,
        contribution_margin=0.49,
        undercut_status="true",
        benchmark_median_delivered_cents=2100,
        reason="test",
    )
    listing = MarketplaceListing(
        channel="etsy",
        title="Original item",
        short_description="Original item",
        long_description="Original item. AI assisted; Printify is the production partner.",
        tags=["original item"],
        bullet_points=[],
        alt_text="Original item",
        target_customer="gift buyer",
        gift_occasions=[],
        seo_meta_title="Original item",
        seo_meta_description="Original item",
    )

    class FakeEtsy:
        settings = SimpleNamespace(etsy_shop_id=42)
        state = "draft"

        async def listing(self, listing_id: int) -> dict[str, object]:
            return {
                "listing_id": listing_id,
                "shop_id": 42,
                "title": listing.title,
                "description": listing.long_description,
                "taxonomy_id": 1,
                "tags": listing.tags,
                "state": self.state,
            }

        async def inventory(self, listing_id: int) -> dict[str, object]:
            return {
                "products": [
                    {
                        "sku": "sku-1",
                        "property_values": [],
                        "offerings": [{"price": "19.99", "quantity": 10, "is_enabled": True}],
                    }
                ]
            }

        async def images(self, listing_id: int) -> list[dict[str, object]]:
            return [{"listing_image_id": 9, "rank": 1, "alt_text": "catalog mockup"}]

        async def update_listing(self, listing_id: int, payload: dict[str, object]) -> None:
            if payload.get("state") == "active":
                self.state = "active"

    class FakePrintify:
        linked = False

        async def publishing_succeeded(
            self, shop_id: str, product_id: str, listing_id: int, handle: str
        ) -> None:
            self.linked = True

        async def product(self, shop_id: str, product_id: str) -> dict[str, object]:
            return {
                "external": {"id": 100},
                "variants": [{"id": 1, "sku": "sku-1"}],
            }

    etsy = FakeEtsy()
    printify = FakePrintify()
    checkpoints: list[str] = []

    def checkpoint(**updates: object) -> None:
        checkpoints.append(str(updates["stage"]))

    async def served_image_gate(
        gallery: list[dict[str, object]], images: list[dict[str, object]]
    ) -> dict[str, object]:
        assert etsy.state == "draft"
        assert len(gallery) == len(images) == 1
        return {"count": 1}

    _, listing_id, verification = await publish_direct_catalog_etsy(
        etsy=etsy,  # type: ignore[arg-type]
        printify=printify,  # type: ignore[arg-type]
        shop_id="shop",
        product_id="product",
        product={
            "variants": [{"id": 1, "sku": "sku-1"}],
            "images": [
                {
                    "src": "https://images.example/mockup.png",
                    "variant_ids": [1],
                    "position": "front",
                }
            ],
        },
        plan=plan,
        listing=listing,
        prices=[price],
        progress={
            "etsy_listing_id": 100,
            "etsy_listing_owned": True,
            "etsy_image_ids": [9],
        },
        checkpoint=checkpoint,
        served_image_gate=served_image_gate,  # type: ignore[arg-type]
    )
    assert listing_id == 100
    assert verification["image"] == {"count": 1}
    assert checkpoints.index("etsy_draft_gallery_verified") < checkpoints.index(
        "activating_etsy_listing"
    )
    assert etsy.state == "active" and printify.linked


@pytest.mark.asyncio
@pytest.mark.parametrize("checks_enabled", [False, True])
async def test_catalog_v2_checks_are_opt_in_and_fake_run_publishes_only_etsy(
    isolated_app: object,
    monkeypatch: pytest.MonkeyPatch,
    checks_enabled: bool,
) -> None:
    settings = get_settings().model_copy(
        update={
            "ip_check_enabled": checks_enabled,
            "originality_check_enabled": checks_enabled,
        }
    )
    if not checks_enabled:
        async def no_model_check(*_: object) -> None:
            raise AssertionError("optional IP/originality model checks must be skipped")

        def no_deterministic_check(*_: object, **__: object) -> None:
            raise AssertionError("optional IP/originality deterministic checks must be skipped")

        monkeypatch.setattr("merch.catalog_pipeline.OpenAIService.ip_screen", no_model_check)
        monkeypatch.setattr(
            "merch.catalog_pipeline.OpenAIService.originality_assessment", no_model_check
        )
        monkeypatch.setattr("merch.catalog_pipeline.evaluate_originality", no_deterministic_check)
        monkeypatch.setattr("merch.catalog_pipeline.copied_listing_wording", no_deterministic_check)
    Base.metadata.create_all(get_engine())
    sync_id = str(claim_catalog_refresh("fake-catalog-bootstrap")["sync_id"])
    await discover_catalog_refresh(sync_id)
    await refresh_catalog_batch(sync_id)
    finalize_catalog_refresh(sync_id)
    value = RunInput(
        run_id=uuid4(),
        scheduled_for=datetime.now(UTC),
        manual=True,
        pipeline_version=2,
    )
    create_run(value, f"catalog-test-{value.run_id}")
    assert await research_catalog_run(str(value.run_id), settings) == 25
    assert await attempt_catalog_opportunity(str(value.run_id), 1, settings)
    assert await publish_catalog_run(str(value.run_id), settings) == "dry_run"
    # An activity replay after the completion checkpoint must not create again.
    assert await publish_catalog_run(str(value.run_id), settings) == "dry_run"
    with session_scope() as session:
        run = RunRepository(session).get(str(value.run_id), full=True)
        assert run.status == "published"
        assert len(run.opportunities) == 25
        assert run.product_plan is not None
        assert (run.ip_report is not None) is checks_enabled
        assert (run.originality_report is not None) is checks_enabled
        assert run.price_decisions
        assert [item.channel for item in run.publishes] == ["etsy"]
        optional_gates = {"ip", "originality_flat", "originality_mockup"}
        assert optional_gates.intersection((run.qa_report or {})["gates"]) == (
            optional_gates if checks_enabled else set()
        )
        check_stages = {
            "catalog_ip_screen",
            "surface_originality",
            "representative_mockup_originality",
        }
        recorded_stages = {item["stage"] for item in run.provider_calls}
        assert check_stages.issubset(recorded_stages) is checks_enabled
        assert {item.data["product_type"] for item in run.opportunities}.issubset(
            {item.title for item in fixture_catalog()}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("drift_kind", ["catalog", "cost"])
async def test_catalog_publish_drift_rejects_before_any_upload(
    isolated_app: object,
    monkeypatch: pytest.MonkeyPatch,
    drift_kind: str,
) -> None:
    Base.metadata.create_all(get_engine())
    sync_id = str(claim_catalog_refresh("drift-test-bootstrap")["sync_id"])
    await discover_catalog_refresh(sync_id)
    await refresh_catalog_batch(sync_id)
    finalize_catalog_refresh(sync_id)
    value = RunInput(
        run_id=uuid4(),
        scheduled_for=datetime.now(UTC),
        manual=True,
        pipeline_version=2,
    )
    create_run(value, f"catalog-drift-test-{value.run_id}")
    assert await research_catalog_run(str(value.run_id)) == 25
    assert await attempt_catalog_opportunity(str(value.run_id), 1)
    with session_scope() as session:
        run = RunRepository(session).get(str(value.run_id), full=True)
        plan = ProductPlanV2.model_validate(run.product_plan)
        prices = [PriceDecision.model_validate(item) for item in run.price_decisions or []]
        opportunity_id = str(run.selected_opportunity["opportunity_id"])

    async def changed_product(blueprint_id, provider_id, settings=None):  # type: ignore[no-untyped-def]
        assert (blueprint_id, provider_id) == (
            plan.blueprint_id,
            plan.print_provider_id,
        )
        product = next(
            item
            for item in fixture_catalog()
            if item.blueprint_id == blueprint_id and item.print_provider_id == provider_id
        )
        return product.model_copy(update={"variants": []}) if drift_kind == "catalog" else product

    async def changed_costs(product, settings=None):  # type: ignore[no-untyped-def]
        return {item.variant_id: item.production_cost_cents + 1 for item in prices}

    async def forbidden_upload(self, filename, data):  # type: ignore[no-untyped-def]
        raise AssertionError("catalog drift must be rejected before artwork upload")

    monkeypatch.setattr("merch.catalog_publisher.refresh_catalog_product", changed_product)
    monkeypatch.setattr("merch.catalog_publisher.collect_printify_costs", changed_costs)
    monkeypatch.setattr(PrintifyClient, "upload_image", forbidden_upload)

    assert await publish_catalog_run(str(value.run_id)) == "hard_gate_failed"
    with session_scope() as session:
        run = RunRepository(session).get(str(value.run_id), full=True)
        rejected = next(
            item for item in run.opportunities if item.data["opportunity_id"] == opportunity_id
        )
        assert run.status == RunStatus.RANKING.value
        assert run.selected_opportunity is None
        assert not rejected.eligible
        assert not run.publishes
