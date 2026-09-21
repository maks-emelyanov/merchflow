from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from merch.config import Settings
from merch.defaults import fixture_product_template
from merch.schemas import Channel, PriceQuote
from merch.services.etsy_publisher import mockup_plan
from merch.services.mockup_verification import prepare_mockups
from merch.services.storefront import (
    EtsyStorefrontClient,
    StorefrontVerificationError,
    build_etsy_selector_inventory,
    normalize_etsy_inventory_dependencies,
    plan_etsy_variation_images,
    printify_listing_id,
    remap_etsy_variation_images,
    selector_labels_are_exact,
    verify_etsy_inventory,
    verify_etsy_listing,
    verify_etsy_selector_labels,
    verify_etsy_variation_images,
    verify_featured_image,
    verify_printify_product,
)


def _fixtures():  # type: ignore[no-untyped-def]
    template = fixture_product_template().model_copy(update={"featured_variant_id": 1001})
    quotes = [
        PriceQuote(
            channel=Channel.ETSY,
            variant_id=variant.variant_id,
            production_cost_cents=variant.production_cost_cents,
            retail_price_cents=2599 if variant.variant_id == 1001 else 3099,
            estimated_fee_cents=300,
            estimated_margin=0.4,
        )
        for variant in template.variants
    ]
    product = {
        "external": {"id": "4577226777"},
        "variants": [
            {"id": 1001, "sku": "black-m", "is_enabled": True, "is_default": True, "price": 2599},
            {"id": 1002, "sku": "forest-l", "is_enabled": True, "is_default": False, "price": 3099},
        ],
        "images": [
            {
                "mockup_id": "product_1002_front",
                "position": "front",
                "src": "https://images.printify.com/forest.jpg",
            },
            {
                "mockup_id": "product_1001_front",
                "position": "front",
                "src": "https://images.printify.com/black.jpg",
            },
        ],
    }
    inventory = {
        "products": [
            {
                "sku": sku,
                "offerings": [
                    {
                        "is_enabled": True,
                        "quantity": 10,
                        "price": {"amount": price, "divisor": 100, "currency_code": "USD"},
                    }
                ],
            }
            for sku, price in [("black-m", 2599), ("forest-l", 3099)]
        ]
    }
    return template, quotes, product, inventory


def test_storefront_requires_exact_featured_mockup_prices_and_variants() -> None:
    template, quotes, product, inventory = _fixtures()
    assert printify_listing_id(product) == 4577226777
    assert verify_printify_product(product, template, quotes).endswith("/black.jpg")
    verify_etsy_inventory(inventory, product, template, quotes)
    verify_etsy_listing(
        {"listing_id": 4577226777, "shop_id": 42, "state": "active", "title": "Approved"},
        4577226777,
        42,
        "Approved",
    )
    verify_featured_image([{"listing_image_id": 9, "rank": 1}], 9)

    product["variants"][0]["is_default"] = False
    with pytest.raises(StorefrontVerificationError, match="default variant"):
        verify_printify_product(product, template, quotes)
    product["variants"][0]["is_default"] = True
    product["variants"][0]["price"] = 2499
    with pytest.raises(StorefrontVerificationError, match="Printify prices"):
        verify_printify_product(product, template, quotes)
    product["variants"][0]["price"] = 2599
    inventory["products"][0]["offerings"][0]["price"]["amount"] = 2499
    with pytest.raises(StorefrontVerificationError, match="prices=1"):
        verify_etsy_inventory(inventory, product, template, quotes)
    with pytest.raises(StorefrontVerificationError, match="approved mockup first"):
        verify_featured_image([{"listing_image_id": 9, "rank": 2}], 9)


def test_selected_variant_controls_printify_default_and_native_featured_photo() -> None:
    template, quotes, product, _ = _fixtures()
    template = template.model_copy(update={"featured_variant_id": 1002})
    product["variants"][0]["is_default"] = False
    product["variants"][1]["is_default"] = True
    assert verify_printify_product(product, template, quotes).endswith("/forest.jpg")
    assert mockup_plan(product, template)[0] == ("#1F3A32", "https://images.printify.com/forest.jpg")
    verify_featured_image([{"listing_image_id": 22, "rank": 1}], 22)
    with pytest.raises(StorefrontVerificationError, match="approved mockup first"):
        verify_featured_image([{"listing_image_id": 22, "rank": 2}], 22)


@pytest.mark.asyncio
@pytest.mark.parametrize("gallery", ["over_etsy_limit", "missing_nonfeatured"])
async def test_general_product_verification_does_not_require_an_etsy_gallery(gallery: str) -> None:
    template, quotes, product, _ = _fixtures()
    if gallery == "over_etsy_limit":
        template = template.model_copy(update={
            "variants": [
                template.variants[0].model_copy(update={
                    "variant_id": 1001 + index, "color": f"Color {index}",
                })
                for index in range(21)
            ],
        })
        quotes = [
            quotes[0].model_copy(update={"channel": Channel.SHOPIFY, "variant_id": variant.variant_id})
            for variant in template.variants
        ]
        product["variants"] = [
            {"id": variant.variant_id, "is_enabled": True,
             "is_default": variant.variant_id == 1001, "price": 2599}
            for variant in template.variants
        ]
        product["images"] = [
            {"mockup_id": f"product_{variant.variant_id}_front", "position": "front",
             "src": f"https://images.printify.com/{variant.variant_id}.jpg"}
            for variant in template.variants
        ]
        expected_error = "at most 20"
    else:
        quotes = [quote.model_copy(update={"channel": Channel.SHOPIFY}) for quote in quotes]
        product["images"] = [image for image in product["images"]
                             if image["mockup_id"] == "product_1001_front"]
        expected_error = "no unambiguous front mockup"
    template = template.model_copy(update={"channels": [template.channels[0]]})
    assert verify_printify_product(product, template, quotes) == next(
        image["src"] for image in product["images"] if image["mockup_id"] == "product_1001_front"
    )

    async def unexpected_download(url: str) -> tuple[bytes, str]:
        pytest.fail(f"Etsy gallery selection must reject before downloading {url}")

    with pytest.raises(StorefrontVerificationError, match=expected_error):
        await prepare_mockups(product, template, downloader=unexpected_download)


def test_general_product_verification_requires_the_rendered_featured_variant() -> None:
    template, quotes, product, _ = _fixtures()
    product["images"] = [{
        **product["images"][0], "variant_ids": [variant.variant_id for variant in template.variants],
    }]
    with pytest.raises(StorefrontVerificationError, match="no unambiguous front mockup"):
        verify_printify_product(product, template, quotes)


def test_storefront_falls_back_to_color_and_size_when_skus_are_absent() -> None:
    template, quotes, product, inventory = _fixtures()
    for variant in product["variants"]:
        variant["sku"] = ""
    for item, (color, size) in zip(
        inventory["products"], [("#111827", "M"), ("#1F3A32", "L")], strict=True
    ):
        item["sku"] = ""
        item["property_values"] = [
            {"property_name": "Primary color", "values": [color]},
            {"property_name": "Size", "values": [size]},
        ]
    verify_etsy_inventory(inventory, product, template, quotes)
    inventory["products"].pop()
    with pytest.raises(StorefrontVerificationError, match="missing=1"):
        verify_etsy_inventory(inventory, product, template, quotes)


def _inventory_with_printify_labels():  # type: ignore[no-untyped-def]
    template, quotes, product, inventory = _fixtures()
    for item, (color, size, color_id, size_id) in zip(
        inventory["products"],
        [("Black", "M", 11, 21), ("Forest", "L", 12, 22)],
        strict=True,
    ):
        item["product_id"] = 900
        item["property_values"] = [
            {
                "property_id": 300,
                "property_name": "Bella+ Canvas Colors",
                "values": [color],
                "value_ids": [color_id],
                "scale_name": None,
            },
            {
                "property_id": 400,
                "property_name": "Clothing sizes",
                "values": [size],
                "value_ids": [size_id],
                "scale_name": None,
            },
        ]
        item["offerings"][0].update({"offering_id": 800, "readiness_state_id": 7})
    inventory.update(
        price_on_property=[300, 400],
        quantity_on_property=[300, 400],
        sku_on_property=[300, 400],
        readiness_state_on_property=[],
    )
    return template, quotes, product, inventory


def test_etsy_selector_update_preserves_inventory_and_variation_photos() -> None:
    template, quotes, product, inventory = _inventory_with_printify_labels()
    assert not selector_labels_are_exact(inventory)
    payload, old_to_new = build_etsy_selector_inventory(inventory)
    assert old_to_new == {300: 514, 400: 513}
    assert payload["price_on_property"] == [513, 514]
    assert payload["quantity_on_property"] == [513, 514]
    assert payload["sku_on_property"] == [513, 514]
    assert payload["products"][0] == {
        "sku": "black-m",
        "property_values": [
            {
                "property_id": 513, "property_name": "Size", "value_ids": [],
                "values": ["M"], "scale_id": None,
            },
            {
                "property_id": 514, "property_name": "Color", "value_ids": [],
                "values": ["Black"], "scale_id": None,
            },
        ],
        "offerings": [
            {"price": 25.99, "quantity": 10, "is_enabled": True, "readiness_state_id": 7}
        ],
    }
    original_photos = [{"property_id": 300, "value": "Black", "image_id": 71}]
    photo_plan = plan_etsy_variation_images(original_photos, inventory, old_to_new)
    assert photo_plan == [{"property_id": 514, "value": "Black", "image_id": 71}]

    updated = deepcopy(inventory)
    for item, converted in zip(updated["products"], payload["products"], strict=True):
        item["property_values"] = converted["property_values"]
        for prop in item["property_values"]:
            prop["value_ids"] = [31 if prop["property_name"] == "Color" else 41]
    verify_etsy_selector_labels(updated)
    verify_etsy_inventory(updated, product, template, quotes)
    expected_photos = remap_etsy_variation_images(photo_plan, updated)
    assert expected_photos == [{"property_id": 514, "value_id": 31, "image_id": 71}]
    verify_etsy_variation_images(expected_photos, expected_photos)
    with pytest.raises(StorefrontVerificationError, match="variation photos differ"):
        verify_etsy_variation_images([], expected_photos)


def test_etsy_selector_update_rejects_unknown_variations() -> None:
    _, _, _, inventory = _inventory_with_printify_labels()
    inventory["products"][0]["property_values"][1]["property_name"] = "Material"
    with pytest.raises(StorefrontVerificationError, match="Cannot identify"):
        build_etsy_selector_inventory(inventory)
    with pytest.raises(StorefrontVerificationError, match="selectors must be named"):
        verify_etsy_selector_labels(inventory)


@pytest.mark.parametrize("price_properties", [[400], []])
def test_selector_normalization_expands_dependencies_without_changing_offerings(
    price_properties: list[int],
) -> None:
    _, _, _, inventory = _inventory_with_printify_labels()
    inventory.update(
        price_on_property=price_properties,
        quantity_on_property=[300],
        readiness_state_on_property=[],
    )
    if not price_properties:
        for product in inventory["products"]:
            product["offerings"][0]["price"]["amount"] = 2599
    before = deepcopy(inventory)

    payload, mapping = build_etsy_selector_inventory(inventory)

    assert mapping == {300: 514, 400: 513}
    assert payload["price_on_property"] == ([513, 514] if price_properties else [])
    assert payload["quantity_on_property"] == payload["sku_on_property"] == [513, 514]
    assert payload["readiness_state_on_property"] == []
    assert inventory == before
    for original, normalized in zip(before["products"], payload["products"], strict=True):
        assert normalized["sku"] == original["sku"]
        old_offer = original["offerings"][0]
        assert normalized["offerings"] == [{
            "price": old_offer["price"]["amount"] / 100,
            "quantity": old_offer["quantity"],
            "is_enabled": old_offer["is_enabled"],
            "readiness_state_id": old_offer["readiness_state_id"],
        }]


@pytest.mark.parametrize("variable", [True, False])
def test_selector_normalization_derives_price_dependency_from_enabled_offerings(variable: bool) -> None:
    _, _, _, inventory = _inventory_with_printify_labels()
    # Deliberately supply stale metadata opposite to the actual enabled prices.
    inventory["price_on_property"] = [] if variable else [300, 400]
    if not variable:
        for product in inventory["products"]:
            product["offerings"][0]["price"]["amount"] = 2599
    disabled = deepcopy(inventory["products"][0])
    disabled["sku"] = "disabled-variant"
    disabled["offerings"][0].update({"is_enabled": False, "price": {
        "amount": 9999, "divisor": 100, "currency_code": "USD",
    }})
    inventory["products"].append(disabled)
    before = deepcopy(inventory)

    payload, _ = build_etsy_selector_inventory(inventory)

    assert payload["price_on_property"] == ([513, 514] if variable else [])
    assert inventory == before
    assert {
        item["sku"]: round(item["offerings"][0]["price"] * 100)
        for item in payload["products"]
    } == {
        item["sku"]: item["offerings"][0]["price"]["amount"]
        for item in before["products"]
    }


@pytest.mark.parametrize("products", [
    None, [], [{}], [{"offerings": []}],
    [{"offerings": [{"is_enabled": True}]}],
    [{"offerings": [{"is_enabled": True, "price": "unavailable"}]}],
])
def test_partial_inventory_retains_declared_dependencies(products) -> None:  # type: ignore[no-untyped-def]
    payload = {"products": products, "price_on_property": [513], "sku_on_property": [513, 514]}
    normalized = normalize_etsy_inventory_dependencies(payload)
    assert normalized["price_on_property"] == [513, 514]
    assert normalized["products"] == products


@pytest.mark.asyncio
async def test_etsy_inventory_error_preserves_validation_detail_and_http_compatibility() -> None:
    api_error = (
        "price_on_property: unsupported number of property IDs. Supports only zero or all 2 "
        "variation properties, as at least one *_on_property field is linked to all 2 properties."
    )
    settings = Settings(
        etsy_api_key="private-key", etsy_shared_secret="private-secret",
        etsy_access_token="private-token", etsy_shop_id=42,
    )
    response: httpx.Response | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response
        response = httpx.Response(400, json={
            "error": api_error,
            "request_body": "PRIVATE INVENTORY BODY",
            "authorization": request.headers["Authorization"],
        }, request=request)
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.etsy.com/v3"
    ) as http:
        client = EtsyStorefrontClient(settings, client=http)
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await client.update_inventory(7, {"products": []})
    assert raised.value.response is response
    assert raised.value.request.method == "PUT"
    assert str(raised.value) == f"Etsy update inventory failed (HTTP 400): {api_error}"
    assert "PRIVATE INVENTORY BODY" not in str(raised.value)
    assert "private-token" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [True, False])
async def test_etsy_api_errors_are_bounded_and_redact_secrets(structured: bool) -> None:
    settings = Settings(
        etsy_api_key="private-key", etsy_shared_secret="private-secret",
        etsy_access_token="private-token", etsy_refresh_token="private-refresh",
        etsy_shop_id=42,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if not structured:
            return httpx.Response(502, text="<html>PRIVATE PROXY BODY</html>", request=request)
        return httpx.Response(400, json={
            "error": "Invalid request\nprivate-key private-secret private-token private-refresh",
            "error_description": "Bearer unrecognized-secret https://example.invalid/?key=other-secret",
            "errors": [
                {"message": "access_token=rotated-example refresh_token='other-example' "
                 '"client_secret": "unconfigured-secret" Authorization: Basic encoded-example '
                 "X-API-Key=unconfigured-key"},
                {"message": "Invalid variation " + "x" * 2000},
            ],
            "debug": "PRIVATE DEBUG BODY",
        }, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.etsy.com/v3"
    ) as http:
        client = EtsyStorefrontClient(settings, client=http)
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await client.create_draft({"title": "PRIVATE REQUEST TITLE"})
    message = str(raised.value)
    assert "Etsy create draft failed (HTTP " in message
    assert len(message) <= 1100
    assert "\n" not in message
    for secret in (
        "private-key", "private-secret", "private-token", "private-refresh",
        "unrecognized-secret", "other-secret", "PRIVATE", "<html>",
        "rotated-example", "other-example", "unconfigured-secret", "encoded-example",
        "unconfigured-key",
    ):
        assert secret not in message
    if structured:
        assert "Invalid request" in message
        assert "Invalid variation" in message
        assert "[REDACTED]" in message
        assert message.endswith("...")
    else:
        assert message == "Etsy create draft failed (HTTP 502)"


@pytest.mark.asyncio
async def test_etsy_client_uses_rank_one_upload_and_reads_back_image() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"listing_image_id": 9}, request=request)
        return httpx.Response(
            200, json={"results": [{"listing_image_id": 9, "rank": 1}]}, request=request
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.etsy.com/v3"
    )
    settings = Settings(
        etsy_api_key="key", etsy_shared_secret="secret", etsy_access_token="token", etsy_shop_id=42
    )
    client = EtsyStorefrontClient(settings, client=http)
    try:
        assert await client.upload_featured(7, b"image", "image/jpeg", "Black shirt") == 9
        verify_featured_image(await client.images(7), 9)
        assert str(requests[0].url).endswith("/shops/42/listings/7/images")
        assert b'name="rank"\r\n\r\n1' in requests[0].content
        assert requests[0].headers["x-api-key"] == "key:secret"
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_etsy_client_deletes_only_the_selected_listing_image() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.etsy.com/v3"
    )
    settings = Settings(
        etsy_api_key="key", etsy_shared_secret="secret", etsy_access_token="token", etsy_shop_id=42
    )
    client = EtsyStorefrontClient(settings, client=http)
    try:
        await client.delete_image(7, 91)
        assert len(requests) == 1
        assert requests[0].method == "DELETE"
        assert requests[0].url.path == "/v3/application/shops/42/listings/7/images/91"
        assert requests[0].headers["x-api-key"] == "key:secret"
        assert requests[0].headers["Authorization"] == "Bearer token"
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_etsy_client_reads_one_listing_transaction_as_a_sale_gate() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={"count": 3, "results": [{"transaction_id": 91}]},
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.etsy.com/v3"
    )
    settings = Settings(
        etsy_api_key="key",
        etsy_shared_secret="secret",
        etsy_access_token="token",
        etsy_shop_id=42,
    )
    client = EtsyStorefrontClient(settings, client=http)
    try:
        assert await client.listing_transactions(7) == [{"transaction_id": 91}]
        assert len(requests) == 1
        assert requests[0].method == "GET"
        assert requests[0].url.path == (
            "/v3/application/shops/42/listings/7/transactions"
        )
        assert dict(requests[0].url.params) == {"limit": "1", "offset": "0"}
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_etsy_client_rejects_incomplete_listing_transaction_page() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"count": 1, "results": []},
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.etsy.com/v3"
    )
    settings = Settings(
        etsy_api_key="key",
        etsy_shared_secret="secret",
        etsy_access_token="token",
        etsy_shop_id=42,
    )
    client = EtsyStorefrontClient(settings, client=http)
    try:
        with pytest.raises(
            StorefrontVerificationError,
            match="omitted a matching order",
        ):
            await client.listing_transactions(7)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_etsy_client_updates_inventory_and_variation_images() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []}, request=request)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.etsy.com/v3"
    )
    settings = Settings(
        etsy_api_key="key", etsy_shared_secret="secret", etsy_access_token="token", etsy_shop_id=42
    )
    client = EtsyStorefrontClient(settings, client=http)
    try:
        await client.update_inventory(7, {"products": []})
        await client.update_variation_images(
            7, [{"property_id": 514, "value_id": 31, "image_id": 71}]
        )
        assert await client.variation_images(7) == []
        assert requests[0].method == "PUT"
        assert requests[0].url.path == "/v3/application/listings/7/inventory"
        assert requests[1].method == "POST"
        assert requests[1].url.path == "/v3/application/shops/42/listings/7/variation-images"
        assert requests[2].method == "GET"
        assert requests[1].headers["Authorization"] == "Bearer token"
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_publish_verification_repairs_selector_labels_and_preserves_photos(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from merch.pipeline import _verify_etsy_publish

    template, quotes, product, initial_inventory = _inventory_with_printify_labels()
    live_inventory = deepcopy(initial_inventory)
    publish_record = SimpleNamespace(response_data={"publish_response": {"status": "accepted"}})
    photo_links = [{"property_id": 300, "value_id": 11, "value": "Black", "image_id": 71}]
    updates: list[dict] = []

    class FakePrintify:
        async def product(self, shop_id, product_id):  # type: ignore[no-untyped-def]
            return product

    class FakeEtsy:
        configured = True

        async def listing(self, listing_id):  # type: ignore[no-untyped-def]
            return {"listing_id": listing_id, "shop_id": 42, "state": "active", "title": "Approved"}

        async def inventory(self, listing_id):  # type: ignore[no-untyped-def]
            return deepcopy(live_inventory)

        async def variation_images(self, listing_id):  # type: ignore[no-untyped-def]
            return deepcopy(photo_links)

        async def update_inventory(self, listing_id, payload):  # type: ignore[no-untyped-def]
            updates.append(payload)
            for item, new in zip(live_inventory["products"], payload["products"], strict=True):
                item["property_values"] = deepcopy(new["property_values"])
                for prop in item["property_values"]:
                    prop["value_ids"] = [
                        31 if prop["property_name"] == "Color" else 41
                    ]
            photo_links.clear()

        async def update_variation_images(self, listing_id, images):  # type: ignore[no-untyped-def]
            photo_links[:] = [{**image, "value": "Black"} for image in images]

        async def images(self, listing_id):  # type: ignore[no-untyped-def]
            return [{"listing_image_id": 9, "rank": 1}]

        async def close(self):
            return None

    class FakeRepository:
        def __init__(self, session):  # type: ignore[no-untyped-def]
            pass

        def publish_record(self, run_id, channel, fingerprint):  # type: ignore[no-untyped-def]
            return publish_record

    @contextmanager
    def fake_session_scope():  # type: ignore[no-untyped-def]
        yield object()

    fake_etsy = FakeEtsy()
    from merch.services.mockup_verification import PreparedMockup

    async def prepared(*args, **kwargs):  # type: ignore[no-untyped-def]
        return [PreparedMockup(color, source, None, b"unused", "image/jpeg")
                for color, source in mockup_plan(product, template)]

    async def checked(*args, **kwargs):  # type: ignore[no-untyped-def]
        # This test isolates inventory-label migration; real pixels are checked in
        # test_mockup_pipeline for both native publication and resumed saved IDs.
        return {"featured_image_id": 9, "image_ids": {"Black": 71},
                "photo_count": 1, "color_photo_links": 1}

    def checkpoint(run_id, **updates):  # type: ignore[no-untyped-def]
        updates.pop("allow_completed", None)
        publish_record.response_data.update(updates)

    monkeypatch.setattr("merch.pipeline.prepare_mockups", prepared)
    monkeypatch.setattr("merch.pipeline.verify_etsy_mockups", checked)
    monkeypatch.setattr("merch.pipeline._checkpoint_etsy_publish", checkpoint)
    monkeypatch.setattr("merch.pipeline.EtsyStorefrontClient", lambda *args, **kwargs: fake_etsy)
    monkeypatch.setattr("merch.pipeline.RunRepository", FakeRepository)
    monkeypatch.setattr("merch.pipeline.session_scope", fake_session_scope)
    settings = Settings(
        etsy_api_key="key", etsy_shared_secret="secret", etsy_access_token="token", etsy_shop_id=42,
        credential_encryption_key="",
    )
    result, verification = await _verify_etsy_publish(
        "run", FakePrintify(), settings, "shop", "product", template,
        SimpleNamespace(title="Approved", alt_text="Shirt"), quotes, 9,
    )
    assert result is product
    assert len(updates) == 1
    assert verification["selector_labels"] == ["Size", "Color"]
    assert photo_links == [
        {"property_id": 514, "value_id": 31, "image_id": 71, "value": "Black"}
    ]
    assert publish_record.response_data["selector_image_plan"] == [
        {"property_id": 514, "value": "Black", "image_id": 71}
    ]

    # A retry after the inventory update repeats only the idempotent photo repair.
    await _verify_etsy_publish(
        "run", FakePrintify(), settings, "shop", "product", template,
        SimpleNamespace(title="Approved", alt_text="Shirt"), quotes, 9,
    )
    assert len(updates) == 1
