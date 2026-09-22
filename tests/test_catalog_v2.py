from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from merch.catalog_pipeline import attempt_catalog_opportunity, research_catalog_run
from merch.catalog_publisher import publish_catalog_run
from merch.config import get_settings
from merch.database import get_engine, session_scope
from merch.domain.catalog import UnsupportedDecorationMethod, fixture_catalog, placement_for
from merch.domain.catalog_prepress import validate_surface_artwork
from merch.domain.etsy_inventory import build_generic_etsy_inventory
from merch.domain.opportunities import evidence_is_fresh
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
    SurfaceArtwork,
)
from merch.services.browser_session import embedded_product_identities, extract_variant_costs
from merch.services.etsy_catalog_publisher import publish_direct_catalog_etsy
from merch.services.marketplace_research import (
    contains_access_challenge,
    extract_listing_snapshot,
)
from merch.services.printify import PrintifyClient

FIXTURES = Path(__file__).parent / "fixtures"


def _png(color: tuple[int, int, int, int] = (255, 0, 0, 255)) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (64, 64), color).save(output, "PNG")
    return output.getvalue()


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
async def test_catalog_v2_fake_run_researches_25_and_publishes_only_etsy(
    isolated_app: object,
) -> None:
    Base.metadata.create_all(get_engine())
    value = RunInput(
        run_id=uuid4(),
        scheduled_for=datetime.now(UTC),
        manual=True,
        pipeline_version=2,
    )
    create_run(value, f"catalog-test-{value.run_id}")
    assert await research_catalog_run(str(value.run_id)) == 25
    assert await attempt_catalog_opportunity(str(value.run_id), 1)
    assert await publish_catalog_run(str(value.run_id)) == "dry_run"
    # An activity replay after the completion checkpoint must not create again.
    assert await publish_catalog_run(str(value.run_id)) == "dry_run"
    with session_scope() as session:
        run = RunRepository(session).get(str(value.run_id), full=True)
        assert run.status == "published"
        assert len(run.opportunities) == 25
        assert run.product_plan is not None
        assert run.originality_report is not None
        assert run.price_decisions
        assert [item.channel for item in run.publishes] == ["etsy"]
        assert {item.data["product_type"] for item in run.opportunities}.issubset(
            {item.title for item in fixture_catalog()}
        )
