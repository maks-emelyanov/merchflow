from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest
from PIL import Image, ImageDraw

from merch.defaults import fixture_product_template
from merch.schemas import Channel, EtsyListingDefaults, MarketplaceListing, PriceQuote
from merch.services.etsy_publisher import direct_inventory, mockup_plan, publish_direct_etsy
from merch.services.storefront import StorefrontVerificationError, verify_etsy_inventory
from merch.setup_etsy_tee import COLORS, SIZES

CDN_IMAGES: dict[str, tuple[bytes, str]] = {}


def shirt_image(color: str) -> bytes:
    image = Image.new("RGB", (400, 500), "#dedede")
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(105, 60), (165, 40), (235, 40), (295, 60), (375, 170), (290, 220),
         (280, 440), (120, 440), (110, 220), (25, 170)],
        fill=color,
    )
    draw.ellipse((165, 24, 235, 75), fill="#dedede")
    draw.ellipse((155, 165, 245, 255), fill="#ffdd55")
    draw.rectangle((175, 185, 225, 235), fill="#bd6339")
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    return encoded.getvalue()


async def source_mockup(url: str) -> tuple[bytes, str]:
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    if filename == "black.jpg":
        color = "#111827"
    elif filename == "forest.jpg":
        color = "#1F3A32"
    else:
        variant_id = int(filename.removesuffix(".jpg"))
        color = list(COLORS.values())[(variant_id - 1001) // len(SIZES)]
    return shirt_image(color), "image/png"


@pytest.fixture(autouse=True)
def mockup_downloads(monkeypatch):  # type: ignore[no-untyped-def]
    CDN_IMAGES.clear()

    async def etsy_image(url: str) -> tuple[bytes, str]:
        return CDN_IMAGES[url]

    monkeypatch.setattr("merch.services.etsy_publisher.download_mockup", source_mockup)
    monkeypatch.setattr("merch.services.etsy_publisher.download_etsy_image", etsy_image)


def fixtures():  # type: ignore[no-untyped-def]
    template = fixture_product_template().model_copy(update={"featured_variant_id": 1001})
    defaults = EtsyListingDefaults(
        taxonomy_id=482, shipping_profile_id=11, return_policy_id=12,
        readiness_state_id=13, production_partner_ids=[14],
    )
    listing = MarketplaceListing(
        channel=Channel.ETSY, title="Original forest shirt", short_description="A shirt",
        long_description="An original forest shirt made by Printify.",
        tags=["forest", "shirt"], bullet_points=[], alt_text="Forest design",
        target_customer="Hikers", gift_occasions=[], seo_meta_title="Forest shirt",
        seo_meta_description="Forest shirt",
    )
    quotes = [
        PriceQuote(channel=Channel.ETSY, variant_id=variant.variant_id,
                   production_cost_cents=variant.production_cost_cents,
                   retail_price_cents=2599 if variant.variant_id == 1001 else 3099,
                   estimated_fee_cents=300, estimated_margin=0.4)
        for variant in template.variants
    ]
    product = {
        "id": "printify-1", "is_locked": False, "external": None,
        "variants": [
            {"id": 1001, "sku": "black-m", "is_enabled": True, "is_default": True, "price": 2599},
            {"id": 1002, "sku": "forest-l", "is_enabled": True, "is_default": False, "price": 3099},
        ],
        "images": [
            {"mockup_id": "product_1001_front", "position": "front",
             "src": "https://images.printify.com/black.jpg"},
            {"mockup_id": "product_1002_front", "position": "front",
             "src": "https://images.printify.com/forest.jpg"},
        ],
    }
    return template, defaults, listing, quotes, product


@pytest.mark.parametrize("identity_source", ["mockup_id", "url"])
def test_mockup_plan_uses_rendered_color_despite_product_wide_variant_ids(
    identity_source: str,
) -> None:
    template, _, _, _, product = fixtures()
    variants = [
        template.variants[0].model_copy(update={
            "variant_id": index, "color": color, "color_hex": COLORS[color], "size": size,
        })
        for index, (color, size) in enumerate(
            ((color, size) for color in COLORS for size in SIZES), start=1001,
        )
    ]
    template = template.model_copy(update={"variants": variants})
    all_ids = [item.variant_id for item in variants]
    representatives = [item for item in variants if item.size == "M"]
    product["images"] = [
        {
            "position": "front", "variant_ids": all_ids,
            "src": f"https://images.printify.com/mockup/printify-1/{item.variant_id}/92547/front.jpg",
            **({"mockup_id": f"printify-1_{item.variant_id}_92547_front"}
               if identity_source == "mockup_id" else {}),
        }
        for item in reversed(representatives)
    ]

    plan = mockup_plan(product, template)

    assert len(plan) == len({source for _, source in plan}) == 14
    assert plan[0][0] == template.featured_variant().color
    assert dict(plan) == {
        item.color: f"https://images.printify.com/mockup/printify-1/{item.variant_id}/92547/front.jpg"
        for item in representatives
    }


@pytest.mark.parametrize("mockup_id", [None, "product_1002_1001_front", "product_9999_front"])
def test_mockup_plan_rejects_broad_membership_without_matching_rendered_variant(
    mockup_id: str | None,
) -> None:
    template, _, _, _, product = fixtures()
    product["images"] = [{
        "position": "front", "variant_ids": [1001, 1002],
        "mockup_id": mockup_id, "src": "https://images.printify.com/unknown.jpg",
    }]

    with pytest.raises(StorefrontVerificationError, match="unambiguous front mockup"):
        mockup_plan(product, template)


def test_mockup_plan_accepts_membership_only_when_all_variants_have_the_same_color() -> None:
    template, _, _, _, product = fixtures()
    template = template.model_copy(update={"variants": [
        *template.variants,
        template.variants[0].model_copy(update={"variant_id": 1003, "size": "S"}),
    ]})
    product["images"][0].pop("mockup_id")
    product["images"][0]["variant_ids"] = [1001, 1003]

    assert mockup_plan(product, template)[0] == (
        template.featured_variant().color, "https://images.printify.com/black.jpg",
    )

    product["images"][0]["variant_ids"].append(9999)
    with pytest.raises(StorefrontVerificationError, match="unambiguous front mockup"):
        mockup_plan(product, template)


def test_mockup_plan_prefers_encoded_identity_over_color_membership() -> None:
    template, _, _, _, product = fixtures()
    product["images"].insert(0, {
        "position": "front", "variant_ids": [1001],
        "src": "https://images.printify.com/fallback.jpg",
    })

    assert mockup_plan(product, template)[0][1] == "https://images.printify.com/black.jpg"


@pytest.mark.parametrize("origin", ["http://images.printify.com", "https://printify.com.invalid"])
def test_mockup_plan_does_not_infer_identity_from_untrusted_url(origin: str) -> None:
    template, _, _, _, product = fixtures()
    product["images"][0] = {
        "position": "front", "variant_ids": [1001, 1002],
        "src": f"{origin}/mockup/printify-1/1001/92547/front.jpg",
    }

    with pytest.raises(StorefrontVerificationError, match="unambiguous front mockup"):
        mockup_plan(product, template)


@pytest.mark.parametrize("path", ["printify-1/1002", "another-product/1001"])
def test_mockup_plan_rejects_conflicting_source_identity(path: str) -> None:
    template, _, _, _, product = fixtures()
    product["images"][0]["src"] = f"https://images.printify.com/mockup/{path}/92547/front.jpg"

    with pytest.raises(StorefrontVerificationError, match=r"conflicting|another product"):
        mockup_plan(product, template)


def test_mockup_plan_rejects_duplicate_sources_for_different_colors() -> None:
    template, _, _, _, product = fixtures()
    product["images"][1]["src"] = product["images"][0]["src"]

    with pytest.raises(StorefrontVerificationError, match="same mockup for different colors"):
        mockup_plan(product, template)


@pytest.mark.parametrize("price_axis", ["size", "color", "constant"])
def test_direct_inventory_price_dependencies_match_unique_variant_skus(price_axis: str) -> None:
    template, defaults, _, _, _ = fixtures()
    variants = [
        template.variants[0].model_copy(update={
            "variant_id": index,
            "color": color,
            "size": size,
        })
        for index, (color, size) in enumerate(
            ((color, size) for color in ("Black", "Forest") for size in ("M", "L")),
            start=1001,
        )
    ]
    template = template.model_copy(update={"variants": variants})
    quotes = [
        PriceQuote(
            channel=Channel.ETSY, variant_id=variant.variant_id,
            production_cost_cents=variant.production_cost_cents,
            retail_price_cents=2599 + 500 * (
                (price_axis == "size" and variant.size == "L")
                or (price_axis == "color" and variant.color == "Forest")
            ),
            estimated_fee_cents=300, estimated_margin=0.4,
        )
        for variant in variants
    ]
    product = {"variants": [
        {"id": variant.variant_id, "sku": f"sku-{variant.variant_id}"}
        for variant in variants
    ]}

    payload = direct_inventory(product, template, quotes, defaults)

    assert payload["sku_on_property"] == [513, 514]
    assert payload["price_on_property"] == ([] if price_axis == "constant" else [513, 514])
    assert {
        item["sku"]: round(item["offerings"][0]["price"] * 100)
        for item in payload["products"]
    } == {f"sku-{quote.variant_id}": quote.retail_price_cents for quote in quotes}


class FakeEtsy:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(etsy_shop_id=42)
        self.remote: dict[str, object] | None = None
        self.inventory_data: dict[str, object] = {"products": []}
        self.photos: list[dict[str, object]] = []
        self.links: list[dict[str, int]] = []
        self.creates = 0
        self.activations = 0

    async def shop_listings(self, state):  # type: ignore[no-untyped-def]
        return [deepcopy(self.remote)] if self.remote and self.remote["state"] == state else []

    async def listing(self, listing_id):  # type: ignore[no-untyped-def]
        assert self.remote and listing_id == self.remote["listing_id"]
        return deepcopy(self.remote)

    async def create_draft(self, payload):  # type: ignore[no-untyped-def]
        self.creates += 1
        self.remote = {
            "listing_id": 99, "shop_id": 42, "state": "draft", "title": payload["title"],
            "description": payload["description"], "taxonomy_id": int(payload["taxonomy_id"]),
            "tags": [], "url": "https://www.etsy.com/listing/99",
        }
        return deepcopy(self.remote)

    async def update_listing(self, listing_id, payload):  # type: ignore[no-untyped-def]
        assert self.remote and listing_id == 99
        if "tags" in payload:
            self.remote["tags"] = payload["tags"].split(",")
        if "state" in payload:
            self.remote["state"] = payload["state"]
            if payload["state"] == "active":
                self.activations += 1
        return deepcopy(self.remote)

    async def inventory(self, listing_id):  # type: ignore[no-untyped-def]
        assert listing_id == 99
        return deepcopy(self.inventory_data)

    async def update_inventory(self, listing_id, payload):  # type: ignore[no-untyped-def]
        assert listing_id == 99
        products = []
        for item in payload["products"]:
            props = deepcopy(item["property_values"])
            for prop in props:
                prop["value_ids"] = [abs(hash(prop["values"][0])) % 10000 + 1]
            offering = deepcopy(item["offerings"][0])
            offering["price"] = {
                "amount": round(offering["price"] * 100),
                "divisor": 100, "currency_code": "USD",
            }
            products.append({"sku": item["sku"], "property_values": props,
                             "offerings": [offering]})
        self.inventory_data = {"products": products}

    async def images(self, listing_id):  # type: ignore[no-untyped-def]
        assert listing_id == 99
        return deepcopy(self.photos)

    async def upload_mockup(self, listing_id, image, content_type, alt_text, rank):  # type: ignore[no-untyped-def]
        assert listing_id == 99 and content_type == "image/png"
        image_id = 100 + len(self.photos)
        url = f"https://i.etsystatic.com/42/r/il/fixture/il_fullxfull.{image_id}.jpg"
        encoded = BytesIO()
        with Image.open(BytesIO(image)) as source:
            source.resize((320, 400)).save(encoded, format="JPEG", quality=90)
        CDN_IMAGES[url] = encoded.getvalue(), "image/jpeg"
        self.photos.append({
            "listing_image_id": image_id, "rank": rank, "alt_text": alt_text,
            "url_fullxfull": url,
        })
        return image_id

    async def variation_images(self, listing_id):  # type: ignore[no-untyped-def]
        assert listing_id == 99
        return deepcopy(self.links)

    async def update_variation_images(self, listing_id, images):  # type: ignore[no-untyped-def]
        assert listing_id == 99
        self.links = deepcopy(images)


class FakePrintify:
    def __init__(self, product, *, fail_link=False):  # type: ignore[no-untyped-def]
        self.remote = deepcopy(product)
        self.publishes = 0
        self.fail_link = fail_link

    async def product(self, shop_id, product_id):  # type: ignore[no-untyped-def]
        assert shop_id == "fixture-etsy" and product_id == "printify-1"
        return deepcopy(self.remote)

    async def publish(self, shop_id, product_id):  # type: ignore[no-untyped-def]
        self.publishes += 1
        return {}

    async def publishing_succeeded(self, shop_id, product_id, listing_id, handle):  # type: ignore[no-untyped-def]
        if self.fail_link:
            raise RuntimeError("Printify refused link")
        self.remote["external"] = {"id": str(listing_id), "handle": handle}


@pytest.mark.asyncio
async def test_direct_etsy_fallback_is_resumable_and_links_the_full_listing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = FakeEtsy(), FakePrintify(product)
    progress: dict[str, object] = {}

    def checkpoint(**updates):  # type: ignore[no-untyped-def]
        progress.update(deepcopy(updates))

    for _ in range(2):
        linked, featured_id, color_ids = await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, progress, checkpoint,
        )
        assert linked["external"]["id"] == "99"
        assert featured_id == color_ids[template.featured_variant().color]
    assert etsy.creates == 1
    assert len(etsy.photos) == 2
    assert len(etsy.links) == 2
    assert etsy.remote["state"] == "active"
    assert etsy.remote["tags"] == listing.tags
    assert printify.publishes == 1
    assert progress["stage"] == "active_listing_verified"


@pytest.mark.asyncio
async def test_inventory_validation_failure_resumes_same_draft_with_all_98_prices(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    template, defaults, listing, _, product = fixtures()
    variants = [
        template.variants[0].model_copy(update={
            "variant_id": index, "color": color, "color_hex": COLORS[color], "size": size,
        })
        for index, (color, size) in enumerate(
            ((color, size) for color in COLORS for size in SIZES), start=1001,
        )
    ]
    template = template.model_copy(update={"variants": variants})
    quotes = [
        PriceQuote(
            channel=Channel.ETSY, variant_id=variant.variant_id,
            production_cost_cents=variant.production_cost_cents,
            retail_price_cents=3099 if variant.size in {"2XL", "3XL"} else 2599,
            estimated_fee_cents=300, estimated_margin=0.4,
        )
        for variant in variants
    ]
    product["variants"] = [
        {"id": item.variant_id, "sku": f"sku-{item.variant_id}"} for item in variants
    ]
    product["images"] = [
        {"variant_ids": [item.variant_id], "position": "front",
         "src": f"https://images.printify.com/{item.variant_id}.jpg"}
        for item in variants
    ]

    class ValidatingEtsy(FakeEtsy):
        async def update_inventory(self, listing_id, payload):  # type: ignore[no-untyped-def]
            if payload["price_on_property"] == [513]:
                httpx.Response(400, json={"error": (
                    "price_on_property: unsupported number of property IDs. Supports only zero "
                    "or all 2 variation properties, as at least one *_on_property field is "
                    "linked to all 2 properties."
                )}, request=httpx.Request("PUT", "https://openapi.etsy.com/inventory")).raise_for_status()
            assert payload["price_on_property"] == payload["sku_on_property"] == [513, 514]
            await super().update_inventory(listing_id, payload)

    def old_inventory(*args, **kwargs):  # type: ignore[no-untyped-def]
        payload = direct_inventory(*args, **kwargs)
        payload["price_on_property"] = [513]
        return payload

    monkeypatch.setattr("merch.services.etsy_publisher.direct_inventory", old_inventory)
    etsy, printify = ValidatingEtsy(), FakePrintify(product)
    progress: dict[str, object] = {}

    def checkpoint(**updates):  # type: ignore[no-untyped-def]
        progress.update(deepcopy(updates))

    with pytest.raises(httpx.HTTPStatusError):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, progress, checkpoint,
        )
    assert progress["stage"] == "writing_inventory"
    assert progress["etsy_listing_id"] == 99
    assert etsy.creates == 1 and etsy.photos == []

    monkeypatch.setattr("merch.services.etsy_publisher.direct_inventory", direct_inventory)
    linked, _, colors = await publish_direct_etsy(
        etsy, printify, "fixture-etsy", "printify-1", product,
        template, listing, quotes, defaults, progress, checkpoint,
    )
    assert etsy.creates == 1 and linked["external"]["id"] == "99"
    assert len(colors) == len(etsy.photos) == len(etsy.links) == 14
    assert len(etsy.inventory_data["products"]) == 98
    verify_etsy_inventory(etsy.inventory_data, product, template, quotes)
    assert printify.publishes == 1
    assert etsy.remote["state"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [True, False])
async def test_image_interruption_resumes_same_draft_without_duplicate_upload(
    monkeypatch, lost_response: bool,
) -> None:  # type: ignore[no-untyped-def]
    class InterruptedEtsy(FakeEtsy):
        def __init__(self) -> None:
            super().__init__()
            self.upload_calls = 0

        async def upload_mockup(self, *args):  # type: ignore[no-untyped-def]
            self.upload_calls += 1
            image_id = await super().upload_mockup(*args)
            if lost_response and self.upload_calls == 1:
                raise httpx.ReadTimeout("Image upload response lost")
            return image_id

    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = InterruptedEtsy(), FakePrintify(product)
    progress: dict[str, object] = {}
    interrupted = False

    def checkpoint(**updates):  # type: ignore[no-untyped-def]
        nonlocal interrupted
        progress.update(deepcopy(updates))
        if not lost_response and not interrupted and updates.get("etsy_color_image_ids"):
            interrupted = True
            raise RuntimeError("Worker interrupted after image checkpoint")

    with pytest.raises((httpx.ReadTimeout, RuntimeError)):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, progress, checkpoint,
        )
    assert etsy.creates == 1 and len(etsy.photos) == 1
    assert progress["etsy_listing_id"] == 99

    linked, _, color_ids = await publish_direct_etsy(
        etsy, printify, "fixture-etsy", "printify-1", product,
        template, listing, quotes, defaults, progress, checkpoint,
    )
    assert linked["external"]["id"] == "99"
    assert etsy.creates == 1 and etsy.upload_calls == 2
    assert len(color_ids) == len(etsy.photos) == len(etsy.links) == 2
    assert printify.publishes == 1


@pytest.mark.asyncio
async def test_direct_etsy_uses_selected_color_as_first_photo(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    template, defaults, listing, quotes, product = fixtures()
    template = template.model_copy(update={"featured_variant_id": 1002})
    product["variants"][0]["is_default"] = False
    product["variants"][1]["is_default"] = True
    etsy, printify = FakeEtsy(), FakePrintify(product)
    _, featured_id, color_ids = await publish_direct_etsy(
        etsy, printify, "fixture-etsy", "printify-1", product,
        template, listing, quotes, defaults, {}, lambda **updates: None,
    )
    assert featured_id == color_ids["#1F3A32"] == 100
    assert etsy.photos[0]["rank"] == 1


@pytest.mark.asyncio
async def test_direct_etsy_fallback_stops_on_ambiguous_listing() -> None:
    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = FakeEtsy(), FakePrintify(product)
    etsy.remote = {
        "listing_id": 99, "shop_id": 42, "state": "draft", "title": listing.title,
        "description": "Different product", "taxonomy_id": 482, "tags": [],
    }
    with pytest.raises(StorefrontVerificationError, match="not the approved product"):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, {}, lambda **updates: None,
        )
    assert etsy.creates == 0


@pytest.mark.asyncio
async def test_direct_etsy_fallback_adopts_matching_active_listing_without_new_photos() -> None:
    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = FakeEtsy(), FakePrintify(product)
    await etsy.create_draft({
        "title": listing.title, "description": listing.long_description,
        "taxonomy_id": defaults.taxonomy_id,
    })
    await etsy.update_inventory(99, direct_inventory(product, template, quotes, defaults))
    etsy.remote["state"] = "active"
    for rank, (color, source) in enumerate(mockup_plan(product, template), start=1):
        content, mime_type = await source_mockup(source)
        image_id = await etsy.upload_mockup(99, content, mime_type, "Existing mockup", rank)
        color_id = next(
            prop["value_ids"][0]
            for item in etsy.inventory_data["products"]
            for prop in item["property_values"]
            if prop["property_id"] == 514 and prop["values"] == [color]
        )
        etsy.links.append({"property_id": 514, "value_id": color_id, "image_id": image_id})
    progress: dict[str, object] = {}

    def checkpoint(**updates):  # type: ignore[no-untyped-def]
        progress.update(updates)

    linked, featured_id, color_ids = await publish_direct_etsy(
        etsy, printify, "fixture-etsy", "printify-1", product,
        template, listing, quotes, defaults, progress, checkpoint,
    )
    assert linked["external"]["id"] == "99"
    assert featured_id == color_ids[template.featured_variant().color] == 100
    assert etsy.creates == 1 and len(etsy.photos) == 2
    assert etsy.activations == 0
    assert progress["stage"] == "adopted_existing_listing"


@pytest.mark.asyncio
async def test_direct_etsy_fallback_deactivates_run_owned_listing_if_link_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = FakeEtsy(), FakePrintify(product, fail_link=True)
    progress: dict[str, object] = {}

    def checkpoint(**updates):  # type: ignore[no-untyped-def]
        progress.update(deepcopy(updates))

    with pytest.raises(StorefrontVerificationError, match="link could not be confirmed"):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, progress, checkpoint,
        )
    assert etsy.remote["state"] == "inactive"
    assert progress["stage"] == "link_failed_listing_inactive"


@pytest.mark.asyncio
async def test_direct_etsy_fallback_deactivates_if_printify_links_another_listing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = FakeEtsy(), FakePrintify(product)
    printify.remote["external"] = {"id": "different"}
    progress: dict[str, object] = {}

    def checkpoint(**updates):  # type: ignore[no-untyped-def]
        progress.update(updates)

    with pytest.raises(StorefrontVerificationError, match="different Etsy listing"):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, progress, checkpoint,
        )
    assert etsy.remote["state"] == "inactive"
    assert progress["stage"] == "link_mismatch_listing_inactive"


@pytest.mark.asyncio
async def test_direct_etsy_fallback_does_not_repeat_unknown_image_upload() -> None:
    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = FakeEtsy(), FakePrintify(product)
    await etsy.create_draft({
        "title": listing.title, "description": listing.long_description,
        "taxonomy_id": defaults.taxonomy_id,
    })
    await etsy.update_inventory(99, direct_inventory(product, template, quotes, defaults))
    featured_color = template.featured_variant().color
    progress = {
        "etsy_listing_id": 99, "etsy_listing_owned": True,
        "image_upload_started_color": featured_color,
    }
    with pytest.raises(StorefrontVerificationError, match="outcome is unknown"):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, progress, lambda **updates: None,
        )
    assert etsy.photos == []


@pytest.mark.asyncio
async def test_direct_etsy_fallback_deactivates_when_activation_readback_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class UnconfirmedEtsy(FakeEtsy):
        async def listing(self, listing_id):  # type: ignore[no-untyped-def]
            if self.remote and self.remote["state"] == "active":
                raise RuntimeError("Etsy read failed")
            return await super().listing(listing_id)

    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = UnconfirmedEtsy(), FakePrintify(product)
    progress: dict[str, object] = {}

    def checkpoint(**updates):  # type: ignore[no-untyped-def]
        progress.update(updates)

    with pytest.raises(StorefrontVerificationError, match="activation could not be confirmed"):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, progress, checkpoint,
        )
    assert etsy.remote["state"] == "inactive"
    assert progress["stage"] == "activation_unverified_listing_inactive"


@pytest.mark.asyncio
async def test_duplicate_image_content_at_different_urls_blocks_before_draft(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def all_white(_url):  # type: ignore[no-untyped-def]
        return shirt_image("white"), "image/png"

    monkeypatch.setattr("merch.services.etsy_publisher.download_mockup", all_white)
    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = FakeEtsy(), FakePrintify(product)
    evidence: list[bytes] = []

    def retain(image: bytes, _mime: str) -> str:
        evidence.append(image)
        return f"evidence/{len(evidence)}"

    with pytest.raises(StorefrontVerificationError):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, {}, lambda **updates: None,
            evidence_writer=retain,
        )

    assert etsy.creates == etsy.activations == printify.publishes == 0
    assert len(evidence) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_source", ["checkpoint", "alt_text"])
async def test_resumed_image_candidates_require_content_verification(candidate_source: str) -> None:
    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = FakeEtsy(), FakePrintify(product)
    await etsy.create_draft({
        "title": listing.title, "description": listing.long_description,
        "taxonomy_id": defaults.taxonomy_id,
    })
    await etsy.update_inventory(99, direct_inventory(product, template, quotes, defaults))
    plan = mockup_plan(product, template)
    saved_ids = {}
    for rank, (color, _) in enumerate(plan, start=1):
        wrong_source = plan[len(plan) - rank][1]
        image, mime = await source_mockup(wrong_source)
        saved_ids[color] = await etsy.upload_mockup(
            99, image, mime, f"{listing.alt_text} on {color} shirt", rank,
        )
    progress = {"etsy_listing_id": 99, "etsy_listing_owned": True}
    if candidate_source == "checkpoint":
        progress["etsy_color_image_ids"] = saved_ids

    with pytest.raises(StorefrontVerificationError):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, progress, lambda **updates: None,
        )

    assert etsy.remote["state"] == "draft"
    assert len(etsy.photos) == 2
    assert etsy.activations == printify.publishes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["draft", "active"])
async def test_completed_checkpoint_does_not_bypass_gallery_readback_on_retry(state: str) -> None:
    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = FakeEtsy(), FakePrintify(product)
    progress: dict[str, object] = {}

    def checkpoint(**updates):  # type: ignore[no-untyped-def]
        progress.update(deepcopy(updates))

    await publish_direct_etsy(
        etsy, printify, "fixture-etsy", "printify-1", product,
        template, listing, quotes, defaults, progress, checkpoint,
    )
    etsy.remote["state"] = state
    forest_url = etsy.photos[1]["url_fullxfull"]
    CDN_IMAGES[forest_url] = CDN_IMAGES[etsy.photos[0]["url_fullxfull"]]
    activations, publishes = etsy.activations, printify.publishes

    with pytest.raises(StorefrontVerificationError):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, progress, checkpoint,
        )

    assert etsy.activations == activations and printify.publishes == publishes
    assert etsy.remote["state"] == state


@pytest.mark.asyncio
async def test_adopted_active_listing_with_incomplete_gallery_stops_before_linking() -> None:
    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = FakeEtsy(), FakePrintify(product)
    await etsy.create_draft({
        "title": listing.title, "description": listing.long_description,
        "taxonomy_id": defaults.taxonomy_id,
    })
    await etsy.update_inventory(99, direct_inventory(product, template, quotes, defaults))
    etsy.remote["state"] = "active"
    image, mime = await source_mockup(product["images"][0]["src"])
    await etsy.upload_mockup(99, image, mime, listing.alt_text, 1)

    with pytest.raises(StorefrontVerificationError):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, {}, lambda **updates: None,
        )

    assert etsy.remote["state"] == "active"
    assert printify.publishes == etsy.activations == 0
    assert len(etsy.photos) == 1


@pytest.mark.asyncio
async def test_direct_upload_reuses_the_validated_preflight_bytes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from merch.services.mockup_verification import prepare_mockups

    template, defaults, listing, quotes, product = fixtures()
    expected = await prepare_mockups(product, template, downloader=source_mockup)

    async def download_changed(_url):  # type: ignore[no-untyped-def]
        pytest.fail("The preflight mockup bytes must be reused during upload")

    monkeypatch.setattr("merch.services.etsy_publisher.download_mockup", download_changed)
    etsy, printify = FakeEtsy(), FakePrintify(product)
    linked, _, colors = await publish_direct_etsy(
        etsy, printify, "fixture-etsy", "printify-1", product,
        template, listing, quotes, defaults, {}, lambda **updates: None,
        prepared_mockups=expected,
    )

    assert linked["external"]["id"] == "99"
    assert len(colors) == 2 and etsy.activations == 1


@pytest.mark.asyncio
async def test_wrong_featured_rank_blocks_activation_and_retains_gallery_evidence() -> None:
    class WrongRankEtsy(FakeEtsy):
        async def upload_mockup(self, listing_id, image, content_type, alt_text, rank):  # type: ignore[no-untyped-def]
            return await super().upload_mockup(listing_id, image, content_type, alt_text, 3 - rank)

    template, defaults, listing, quotes, product = fixtures()
    etsy, printify = WrongRankEtsy(), FakePrintify(product)
    progress: dict[str, object] = {}

    def checkpoint(**updates):  # type: ignore[no-untyped-def]
        progress.update(deepcopy(updates))

    with pytest.raises(StorefrontVerificationError, match="featured color first"):
        await publish_direct_etsy(
            etsy, printify, "fixture-etsy", "printify-1", product,
            template, listing, quotes, defaults, progress, checkpoint,
        )

    assert etsy.remote["state"] == "draft"
    assert printify.publishes == etsy.activations == 0
    assert progress["mockup_verification"]["status"] == "failed"
    assert len(progress["mockup_verification"]["gallery"]) == 2
