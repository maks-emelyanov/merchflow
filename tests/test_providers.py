from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from datetime import date
from itertools import pairwise
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from openai import BadRequestError, RateLimitError
from PIL import Image

from merch.config import Settings
from merch.defaults import fixture_product_template
from merch.schemas import DesignMode, ResearchReport, TypographyProposal, TypographySpec
from merch.services.openai_service import (
    ModelResult,
    OpenAINonRetryableError,
    OpenAIService,
    _catalog_generation_size,
    _resize_catalog_artwork,
)
from merch.services.printify import (
    AmbiguousCreateError,
    PrintifyClient,
    PrintifyHTTPError,
    ProviderConfigurationError,
)


@pytest.mark.asyncio
async def test_openai_responses_contract_uses_structured_output_and_search() -> None:
    fake_report = (await OpenAIService(Settings()).research(date.today(), "none")).value
    captured = {}

    class Responses:
        async def parse(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return SimpleNamespace(
                output_parsed=fake_report,
                usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 10}),
                id="resp_fixture",
                model="gpt-6-astra",
            )

    service = OpenAIService(Settings(provider_mode="live", openai_api_key="test"))
    service.client = SimpleNamespace(responses=Responses())  # type: ignore[assignment]
    result = await service._parse("research", ResearchReport, web_search=True)
    assert result.metadata["response_id"] == "resp_fixture"
    assert captured["text_format"] is ResearchReport
    assert captured["tools"] == [{"type": "web_search"}]
    assert "temperature" not in captured


@pytest.mark.asyncio
async def test_openai_routes_research_and_caps_visual_detail() -> None:
    fake_report = (await OpenAIService(Settings()).research(date.today(), "none")).value
    captured = {}

    class Responses:
        async def parse(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return SimpleNamespace(
                output_parsed=fake_report,
                usage=SimpleNamespace(
                    model_dump=lambda: {"input_tokens": 100, "output_tokens": 20}
                ),
                id="resp_fixture",
                model=kwargs["model"],
                output=[],
            )

    service = OpenAIService(Settings(provider_mode="live", openai_api_key="test"))
    service.client = SimpleNamespace(responses=Responses())  # type: ignore[assignment]
    result = await service.research(date.today(), "none")
    assert captured["model"] == "gpt-5.6-terra"
    assert captured["reasoning"] == {"effort": "medium"}
    assert result.metadata["estimated_cost_usd"] is not None
    assert result.metadata["reasoning_effort"] == "medium"
    await service._parse("visual QA", ResearchReport, image=b"png-bytes")
    assert captured["input"][0]["content"][1]["detail"] == "high"


@pytest.mark.asyncio
async def test_openai_image_quality_defaults_to_medium() -> None:
    brief = (
        await OpenAIService(Settings()).creative(
            (await OpenAIService(Settings()).research(date.today(), "none")).value.candidates[0],
            {},
        )
    ).value
    captured = {}

    class Images:
        async def generate(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"image").decode())],
                quality="medium",
                size="1024x1024",
                usage=SimpleNamespace(
                    model_dump=lambda: {
                        "input_tokens_details": {"text_tokens": 50},
                        "output_tokens_details": {"image_tokens": 500},
                    }
                ),
            )

    service = OpenAIService(Settings(provider_mode="live", openai_api_key="test"))
    service.client = SimpleNamespace(images=Images())  # type: ignore[assignment]
    image, metadata = await service.artwork(brief, 1024, 1024)
    assert image == b"image"
    assert captured["quality"] == "medium"
    assert metadata["estimated_cost_usd"] == 0.01525


@pytest.mark.parametrize(
    ("surface_size"),
    [(4500, 5100), (3909, 4431), (3319, 3761), (898, 898), (750, 750)],
)
def test_catalog_generation_size_satisfies_image_api_contract(
    surface_size: tuple[int, int],
) -> None:
    width, height = _catalog_generation_size(*surface_size)

    assert width % 16 == height % 16 == 0
    assert max(width, height) <= 3840
    assert 655_360 <= width * height <= 8_294_400
    assert max(width / height, height / width) <= 3
    target_ratio = surface_size[0] / surface_size[1]
    assert abs(width / height - target_ratio) / target_ratio < 0.01


def test_catalog_artwork_resampling_preserves_alpha_and_exact_dimensions() -> None:
    source = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    for x in range(8, 16):
        for y in range(16):
            source.putpixel((x, y), (255, 0, 0, 255))
    encoded = io.BytesIO()
    source.save(encoded, "PNG")

    resized = _resize_catalog_artwork(encoded.getvalue(), 32, 48)

    with Image.open(io.BytesIO(resized)) as output:
        assert output.size == (32, 48)
        assert output.convert("RGBA").getchannel("A").getextrema() == (0, 255)


@pytest.mark.asyncio
async def test_openai_image_edit_preserves_requested_source_size() -> None:
    fixture = OpenAIService(Settings())
    concept = (await fixture.research(date.today(), "none")).value.candidates[0]
    brief = (await fixture.creative(concept, {})).value
    source = io.BytesIO()
    Image.new("RGBA", (32, 48), (0, 0, 0, 0)).save(source, "PNG")
    captured = {}

    class Images:
        async def edit(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(source.getvalue()).decode())],
                quality="high",
                size="32x48",
                usage=None,
            )

    service = OpenAIService(Settings(provider_mode="live", openai_api_key="test"))
    service.client = SimpleNamespace(images=Images())  # type: ignore[assignment]
    await service.revise_artwork(source.getvalue(), brief, [])
    assert captured["size"] == "32x48"


@pytest.mark.asyncio
async def test_typography_binds_exact_multiline_slogan_and_renderable_colors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = OpenAIService(Settings())
    concept = (await fixture.research(date.today(), "none")).value.candidates[0]
    slogan = "KILN WEATHER BUREAU\nHEAT ADVISORY IN EFFECT"
    brief = (await fixture.creative(concept, {})).value.model_copy(
        update={
            "slogan": slogan,
            "palette": ["Cream (#F4E6CC)", "Midnight blue (#1A1F35)"],
        }
    )
    parsed = TypographyProposal(
        exact_text="KILN WEATHER BUREAU HEAT ADVISORY IN EFFECT",
        line_breaks=["KILN WEATHER BUREAU", "HEAT ADVISORY IN EFFECT"],
        letter_spacing=0.03,
        line_spacing=1.1,
        text_alignment="center",
        text_arc_or_shape="none",
        outline="#1A1F35",
        shadow=None,
        distress_level=0,
        primary_color="#F4E6CC on dark garments; #1A1F35 on light garments",
        secondary_color=None,
        interaction_with_illustration="Centered typography only",
        relative_width=0.88,
        relative_height=0.28,
    )
    service = OpenAIService(Settings(provider_mode="live", openai_api_key="test"))

    async def parse(*args, **kwargs):  # type: ignore[no-untyped-def]
        return ModelResult(parsed, {"model": "fixture"})

    monkeypatch.setattr(service, "_parse", parse)
    result = await service.typography(slogan, brief)
    assert result.value.exact_text == slogan
    assert result.value.line_breaks == ["KILN WEATHER BUREAU", "HEAT ADVISORY IN EFFECT"]
    assert result.value.primary_color == "#F4E6CC"
    assert result.value.outline == "#1A1F35"
    assert result.value.vertical_placement == "bottom"


@pytest.mark.asyncio
async def test_typography_canonicalization_repairs_malformed_transport_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = OpenAIService(Settings())
    concept = (await fixture.research(date.today(), "none")).value.candidates[0]
    slogan = " KILN WEATHER BUREAU\r\n\r\n HEAT ADVISORY IN EFFECT "
    brief = (await fixture.creative(concept, {})).value.model_copy(
        update={
            "slogan": slogan,
            "palette": ["Cream (#F4E6CC)", "Midnight blue (#1A1F35)"],
        }
    )
    parsed = TypographyProposal(
        exact_text="WRONG WORDS",
        line_breaks=[""],
        letter_spacing=0.03,
        line_spacing=1.1,
        text_alignment="center",
        text_arc_or_shape="none",
        outline="not a color",
        shadow="#12345680",
        distress_level=0,
        primary_color="cream on dark shirts and navy on light shirts",
        secondary_color="transparent",
        interaction_with_illustration="below",
        relative_width=0.88,
        relative_height=0.28,
    )
    service = OpenAIService(Settings(provider_mode="live", openai_api_key="test"))

    async def parse(*args, **kwargs):  # type: ignore[no-untyped-def]
        return ModelResult(parsed, {"model": "fixture"})

    monkeypatch.setattr(service, "_parse", parse)
    result = await service.typography(slogan, brief)

    assert result.value.exact_text == "KILN WEATHER BUREAU\nHEAT ADVISORY IN EFFECT"
    assert result.value.line_breaks == ["KILN WEATHER BUREAU", "HEAT ADVISORY IN EFFECT"]
    assert result.value.primary_color == "#F4E6CC"
    assert result.value.outline is None
    assert result.value.shadow is None
    assert result.value.secondary_color is None


@pytest.mark.asyncio
async def test_typography_preserves_soft_wraps_and_enforces_hard_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = OpenAIService(Settings())
    concept = (await fixture.research(date.today(), "none")).value.candidates[0]
    brief = (await fixture.creative(concept, {})).value
    service = OpenAIService(Settings(provider_mode="live", openai_api_key="test"))

    def typography(exact_text: str, line_breaks: list[str]) -> TypographySpec:
        return TypographySpec(
            exact_text=exact_text,
            line_breaks=line_breaks,
            letter_spacing=0.03,
            line_spacing=1.1,
            text_alignment="center",
            text_arc_or_shape="none",
            outline=None,
            shadow=None,
            distress_level=0,
            primary_color="#F4E6CC",
            secondary_color=None,
            interaction_with_illustration="below",
            relative_width=0.88,
            relative_height=0.28,
        )

    soft_slogan = "KILN WEATHER BUREAU HEAT ADVISORY IN EFFECT"
    parsed = typography(soft_slogan, ["KILN WEATHER BUREAU", "HEAT ADVISORY IN EFFECT"])

    async def parse(*args, **kwargs):  # type: ignore[no-untyped-def]
        return ModelResult(parsed, {"model": "fixture"})

    monkeypatch.setattr(service, "_parse", parse)
    result = await service.typography(soft_slogan, brief)
    assert result.value.line_breaks == ["KILN WEATHER BUREAU", "HEAT ADVISORY IN EFFECT"]
    assert result.value.vertical_placement == "bottom"

    hard_slogan = "KILN WEATHER BUREAU\nHEAT ADVISORY IN EFFECT"
    parsed = typography(
        hard_slogan.replace("\n", " "),
        ["KILN WEATHER", "BUREAU HEAT ADVISORY", "IN EFFECT"],
    )
    result = await service.typography(
        hard_slogan, brief.model_copy(update={"design_mode": DesignMode.TYPOGRAPHY})
    )
    assert result.value.exact_text == hard_slogan
    assert result.value.line_breaks == ["KILN WEATHER BUREAU", "HEAT ADVISORY IN EFFECT"]
    assert result.value.vertical_placement == "center"


def test_openai_exhausted_credit_is_not_retried_as_transient_429() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx2.Response(429, request=request)
    exhausted = RateLimitError(
        "no credits",
        response=response,
        body={"type": "insufficient_quota", "code": "credit_balance_exhausted"},
    )
    with pytest.raises(OpenAINonRetryableError, match="credits, spend limit, or quota"):
        OpenAIService._handle_api_error(exhausted)
    project_limit = RateLimitError(
        "project limit", response=response, body={"code": "project_spend_limit_exceeded"}
    )
    with pytest.raises(OpenAINonRetryableError, match="spend limit"):
        OpenAIService._handle_api_error(project_limit)
    transient = RateLimitError(
        "too many requests", response=response, body={"code": "rate_limit_exceeded"}
    )
    assert OpenAIService._handle_api_error(transient) is None
    bad_request = BadRequestError(
        "invalid schema", response=httpx2.Response(400, request=request), body={}
    )
    with pytest.raises(OpenAINonRetryableError, match="HTTP 400"):
        OpenAIService._handle_api_error(bad_request)


@pytest.mark.asyncio
async def test_printify_ambiguous_create_is_not_blindly_retried() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="https://api.printify.com/v1")
    client = PrintifyClient(
        Settings(provider_mode="live", publish_mode="live", printify_api_token="token"),
        client=http,
    )
    with pytest.raises(AmbiguousCreateError):
        await client.create_product("shop", {"title": "fixture"})
    assert calls == 1
    await client.close()


@pytest.mark.asyncio
async def test_printify_get_honors_retry_after_on_rate_limit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "3"},
                request=request,
            )
        return httpx.Response(200, json=[], request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("merch.services.printify.asyncio.sleep", sleep)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.printify.com/v1"
    )
    client = PrintifyClient(
        Settings(
            printify_api_token="token",
            printify_request_interval_seconds=0,
            printify_catalog_request_interval_seconds=0,
        ),
        client=http,
    )
    try:
        assert await client.blueprints() == []
        assert calls == 2
        assert delays == [3.0]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_printify_ranked_blueprints_use_public_bestsellers_without_auth() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/product-catalog-service/api/v1/blueprints/search"
        assert request.url.params.get("tags[]") == "Bestsellers"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "total": 2,
                "data": [
                    {"blueprintId": 706, "name": "Garment-Dyed T-shirt"},
                    {"blueprintId": 440, "name": "Staple Tee"},
                ],
            },
            request=request,
        )

    public_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://printify.com",
    )
    api_http = httpx.AsyncClient(base_url="https://api.printify.com/v1")
    client = PrintifyClient(
        Settings(printify_api_token="private-token"),
        client=api_http,
        public_client=public_http,
    )
    try:
        assert [item["blueprintId"] for item in await client.ranked_blueprints()] == [
            706,
            440,
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_printify_get_honors_retry_after_on_server_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                503,
                headers={"Retry-After": "4"},
                request=request,
            )
        return httpx.Response(200, json={}, request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("merch.services.printify.asyncio.sleep", sleep)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.printify.com/v1"
    )
    client = PrintifyClient(
        Settings(
            printify_api_token="token",
            printify_catalog_request_interval_seconds=0,
        ),
        client=http,
    )
    try:
        assert await client.blueprint(6) == {}
        assert calls == 2
        assert delays == [4.0]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_printify_get_retries_transient_invalid_scope_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                403,
                json={"error": "Invalid scope(s) provided."},
                request=request,
            )
        return httpx.Response(200, json=[], request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("merch.services.printify.asyncio.sleep", sleep)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.printify.com/v1"
    )
    client = PrintifyClient(
        Settings(
            printify_api_token="token",
            printify_catalog_request_interval_seconds=0,
        ),
        client=http,
    )
    try:
        assert await client.catalog_print_providers() == []
        assert calls == 2
        assert delays == [1.0]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_printify_get_does_not_retry_other_forbidden_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"error": "Forbidden"}, request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("merch.services.printify.asyncio.sleep", sleep)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.printify.com/v1"
    )
    client = PrintifyClient(
        Settings(
            printify_api_token="token",
            printify_catalog_request_interval_seconds=0,
        ),
        client=http,
    )
    try:
        with pytest.raises(PrintifyHTTPError, match="status 403"):
            await client.catalog_print_providers()
        assert calls == 1
        assert delays == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_printify_catalog_failure_is_typed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, request=request)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.printify.com/v1"
    )
    client = PrintifyClient(
        Settings(
            printify_api_token="token",
            printify_catalog_request_interval_seconds=0,
        ),
        client=http,
    )
    try:
        with pytest.raises(PrintifyHTTPError) as captured:
            await client.blueprint(5811)
        assert captured.value.method == "GET"
        assert captured.value.path == "/catalog/blueprints/5811.json"
        assert captured.value.status == captured.value.status_code == 410
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_printify_catalog_pacer_is_shared_and_bounds_concurrency() -> None:
    active = 0
    maximum_active = 0
    starts: list[float] = []
    first_four_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        starts.append(time.monotonic())
        if len(starts) == 4:
            first_four_started.set()
        await first_four_started.wait()
        await asyncio.sleep(0.015)
        active -= 1
        return httpx.Response(200, json=[], request=request)

    settings = Settings(
        printify_api_token="token",
        printify_catalog_request_interval_seconds=0.01,
    )
    clients = [
        PrintifyClient(
            settings,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="https://api.printify.com/v1",
            ),
        )
        for _ in range(2)
    ]
    try:
        await asyncio.gather(*(client.blueprints() for client in clients for _ in range(4)))
        assert maximum_active == 4
        assert all(later - earlier >= 0.007 for earlier, later in pairwise(starts))
    finally:
        await asyncio.gather(*(client.close() for client in clients))


@pytest.mark.asyncio
async def test_printify_orders_uses_supported_page_parameter_only() -> None:
    paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        return httpx.Response(200, json={"data": []}, request=request)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.printify.com/v1"
    )
    client = PrintifyClient(Settings(printify_api_token="token"), client=http)
    assert (await client.orders("123", page=2))["data"] == []
    assert paths == ["https://api.printify.com/v1/shops/123/orders.json?page=2"]
    await client.close()


@pytest.mark.asyncio
async def test_printify_product_reconciliation_uses_page_pagination() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        page = int(request.url.params["page"])
        product = {
            "id": "product-7",
            "title": "Approved listing title",
            "print_areas": [
                {
                    "placeholders": [
                        {
                            "images": [
                                {"id": "upload-9"},
                            ]
                        }
                    ]
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                "data": [] if page == 1 else [product],
                "current_page": page,
                "last_page": 2,
                "per_page": 50,
            },
            request=request,
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.printify.com/v1",
    )
    client = PrintifyClient(
        Settings(
            provider_mode="live",
            publish_mode="live",
            printify_api_token="token",
        ),
        client=http,
    )
    try:
        matches = await client.reconcile_product("shop-3", "upload-9", "Approved listing title")
    finally:
        await client.close()

    assert [item["id"] for item in matches] == ["product-7"]
    assert paths == [
        "https://api.printify.com/v1/shops/shop-3/products.json?page=1",
        "https://api.printify.com/v1/shops/shop-3/products.json?page=2",
    ]


@pytest.mark.asyncio
async def test_printify_template_validation_refreshes_and_checks_print_area() -> None:
    incompatible = False

    async def handler(request: httpx.Request) -> httpx.Response:
        second_width = 4000 if incompatible else 4494
        return httpx.Response(
            200,
            json={
                "variants": [
                    {
                        "id": 1001,
                        "cost": 975,
                        "placeholders": [
                            {
                                "position": "front",
                                "decoration_method": "dtg",
                                "width": 3703,
                                "height": 4200,
                            }
                        ],
                    },
                    {
                        "id": 1002,
                        "cost": 1075,
                        "placeholders": [
                            {
                                "position": "front",
                                "decoration_method": "dtg",
                                "width": second_width,
                                "height": 5097,
                            }
                        ],
                    },
                ]
            },
            request=request,
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.printify.com/v1"
    )
    client = PrintifyClient(Settings(provider_mode="live", printify_api_token="token"), client=http)
    try:
        current = await client.validate_template(fixture_product_template())
        assert (current.print_width, current.print_height) == (4494, 5097)
        assert [item.production_cost_cents for item in current.variants] == [975, 1075]
        incompatible = True
        with pytest.raises(ProviderConfigurationError, match="incompatible print areas"):
            await client.validate_template(fixture_product_template())
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_printify_updates_only_print_areas_on_existing_product() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "product-7"}, request=request)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.printify.com/v1"
    )
    client = PrintifyClient(Settings(printify_api_token="token"), client=http)
    print_areas = [
        {
            "variant_ids": [101, 102],
            "placeholders": [
                {
                    "position": "front",
                    "images": [{"id": "new-upload", "x": 0.5, "y": 0.5, "scale": 1.0, "angle": 0}],
                }
            ],
        }
    ]
    try:
        assert await client.update_product_print_areas("shop-3", "product-7", print_areas) == {
            "id": "product-7"
        }
        assert len(requests) == 1
        assert requests[0].method == "PUT"
        assert requests[0].url.path == "/v1/shops/shop-3/products/product-7.json"
        assert json.loads(requests[0].content) == {"print_areas": print_areas}
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_printify_updates_mutable_catalog_product_fields() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "product-7"}, request=request)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.printify.com/v1"
    )
    client = PrintifyClient(Settings(printify_api_token="token"), client=http)
    payload = {
        "title": "Updated",
        "description": "Updated description",
        "tags": ["updated"],
        "blueprint_id": 5,
        "print_provider_id": 99,
        "variants": [{"id": 101, "price": 1999, "is_enabled": True}],
        "print_areas": [{"variant_ids": [101], "placeholders": []}],
    }
    try:
        assert await client.update_catalog_product("shop-3", "product-7", payload) == {
            "id": "product-7"
        }
        sent = json.loads(requests[0].content)
        assert set(sent) == {"title", "description", "tags", "variants", "print_areas"}
        assert requests[0].method == "PUT"
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_printify_refuses_to_remove_every_print_area() -> None:
    async def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"empty print areas must not call Printify: {request.url}")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected), base_url="https://api.printify.com/v1"
    )
    client = PrintifyClient(Settings(printify_api_token="token"), client=http)
    try:
        with pytest.raises(ValueError, match="at least one print area"):
            await client.update_product_print_areas("shop-3", "product-7", [])
    finally:
        await http.aclose()
