from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from merch.config import Settings
from merch.domain.performance import summarize_performance
from merch.services.analytics import EtsyAnalyticsClient, parse_etsy_stats_csv


def test_etsy_csv_normalizes_nullable_funnel_metrics() -> None:
    rows = parse_etsy_stats_csv(
        b"date,listing_id,impressions,visits,favorites,cart_adds,orders,revenue\n"
        b"2026-09-10,123,100,12,3,,2,44.50\n"
    )
    assert len(rows) == 1
    assert rows[0].impressions == 100
    assert rows[0].cart_adds is None
    assert rows[0].gross_revenue_cents == 4450
    assert rows[0].completeness["cart_adds"] is False


def test_etsy_csv_rejects_unknown_shape() -> None:
    with pytest.raises(ValueError, match="date and listing_id"):
        parse_etsy_stats_csv(b"day,item\n2026-09-10,1\n")


def _etsy_settings() -> Settings:
    return Settings(
        _env_file=None, provider_mode="fake", etsy_api_key="fixture-key",
        etsy_shared_secret="fixture-secret", etsy_access_token="fixture-token", etsy_shop_id=1,
    )


def _receipt(receipt_id: int) -> dict:
    return {
        "receipt_id": receipt_id,
        "create_timestamp": int(datetime(2026, 9, 20, tzinfo=UTC).timestamp()),
        "transactions": [{
            "listing_id": 123, "quantity": 1,
            "price": {"amount": 2000, "divisor": 100},
        }],
    }


@pytest.mark.asyncio
async def test_etsy_sync_paginates_complete_window_before_replacing_csv_sales() -> None:
    requests: list[httpx.Request] = []
    receipts = [_receipt(index) for index in range(1, 102)]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"count": 101, "results": receipts[offset:offset + 100]})

    async with httpx.AsyncClient(base_url="https://etsy.invalid/v3", transport=httpx.MockTransport(respond)) as client:
        rows = await EtsyAnalyticsClient(_etsy_settings(), client).sync(date(2026, 9, 1))
    assert [request.url.params["offset"] for request in requests] == ["0", "100"]
    assert len({request.url.params["max_created"] for request in requests}) == 1
    assert all(request.url.params["sort_on"] == "receipt_id" for request in requests)
    assert rows[0].orders == rows[0].units == 101
    assert rows[0].gross_revenue_cents == 202000
    assert rows[0].completeness["receipt_window_complete"]
    csv_row = {
        **rows[0].model_dump(mode="json"), "source": "etsy_csv", "completeness": {},
    }
    summary = summarize_performance([rows[0].model_dump(mode="json"), csv_row], {})
    assert len(summary["performance"]) == 1
    assert summary["performance"][0]["source"] == "etsy_api"
    assert summary["performance"][0]["metrics"]["orders"] == 101


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "duplicate", "changed_count", "http_error"])
async def test_etsy_sync_does_not_return_partial_totals(failure: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["offset"] == "0":
            return httpx.Response(200, json={
                "count": 101, "results": [_receipt(index) for index in range(1, 101)],
            })
        if failure == "http_error":
            return httpx.Response(503)
        return httpx.Response(200, json={
            "count": 102 if failure == "changed_count" else 101,
            "results": [] if failure == "empty" else [_receipt(1)],
        })

    async with httpx.AsyncClient(base_url="https://etsy.invalid/v3", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
            await EtsyAnalyticsClient(_etsy_settings(), client).sync(date(2026, 9, 1))


@pytest.mark.asyncio
async def test_etsy_sync_bounds_pagination_and_accepts_empty_windows() -> None:
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"count": 12101 if requests == 1 else 0, "results": []})

    async with httpx.AsyncClient(base_url="https://etsy.invalid/v3", transport=httpx.MockTransport(respond)) as client:
        analytics = EtsyAnalyticsClient(_etsy_settings(), client)
        with pytest.raises(RuntimeError, match="exceeds supported pagination"):
            await analytics.sync(date(2026, 9, 1))
        assert requests == 1
        assert await analytics.sync(date(2026, 9, 1)) == []


@pytest.mark.asyncio
async def test_etsy_sync_counts_receipts_and_multiplies_each_variation_price_by_quantity() -> None:
    first = _receipt(1)
    first["transactions"] = [
        {"listing_id": 123, "sku": "small", "quantity": 2, "price": {"amount": 1999, "divisor": 100}},
        {"listing_id": 123, "sku": "large", "quantity": 3, "price": {"amount": 2500, "divisor": 100}},
        {"listing_id": 456, "sku": "medium", "quantity": 2, "price": {"amount": 30, "divisor": 1}},
    ]
    second = _receipt(2)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 2, "results": [first, second]})

    async with httpx.AsyncClient(base_url="https://etsy.invalid/v3", transport=httpx.MockTransport(respond)) as client:
        rows = await EtsyAnalyticsClient(_etsy_settings(), client).sync(date(2026, 9, 1))
    by_listing = {row.external_product_id: row for row in rows}
    assert by_listing["123"].orders == 2
    assert by_listing["123"].units == 6
    assert by_listing["123"].gross_revenue_cents == 1999 * 2 + 2500 * 3 + 2000
    assert by_listing["456"].orders == 1
    assert by_listing["456"].units == 2
    assert by_listing["456"].gross_revenue_cents == 6000
