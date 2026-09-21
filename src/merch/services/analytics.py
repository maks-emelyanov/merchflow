from __future__ import annotations

import asyncio
import csv
import io
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from typing import Any, cast

import httpx

from merch.config import Settings
from merch.schemas import Channel, DailyPerformance

ETSY_RECEIPT_PAGE_SIZE = 100
ETSY_MAX_RECEIPT_PAGES = 121  # Etsy's maximum supported offset is 12,000.


def _cents(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return round(float(value) * 100)


def parse_etsy_stats_csv(data: bytes) -> list[DailyPerformance]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    required = {"date", "listing_id"}
    if not reader.fieldnames or not required.issubset(
        {name.strip().lower() for name in reader.fieldnames}
    ):
        raise ValueError("Etsy CSV must contain date and listing_id columns")
    rows: list[DailyPerformance] = []
    for raw in reader:
        row = {key.strip().lower(): value.strip() for key, value in raw.items() if key}
        rows.append(
            DailyPerformance(
                metric_date=date.fromisoformat(row["date"]),
                channel=Channel.ETSY,
                external_product_id=row["listing_id"],
                impressions=int(row["impressions"]) if row.get("impressions") else None,
                visits=int(row["visits"]) if row.get("visits") else None,
                favorites=int(row["favorites"]) if row.get("favorites") else None,
                cart_adds=int(row["cart_adds"]) if row.get("cart_adds") else None,
                orders=int(row["orders"]) if row.get("orders") else None,
                gross_revenue_cents=_cents(row.get("revenue")),
                source="etsy_csv",
                completeness={
                    key: bool(row.get(key))
                    for key in (
                        "impressions",
                        "visits",
                        "favorites",
                        "cart_adds",
                        "orders",
                        "revenue",
                    )
                },
            )
        )
    return rows


class ShopifyAnalyticsClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        base = f"https://{settings.shopify_shop_domain}/admin/api/{settings.shopify_api_version}"
        self.client = client or httpx.AsyncClient(base_url=base, timeout=60)

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.shopify_shop_domain
            and self.settings.shopify_admin_token.get_secret_value()
        )

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            "/graphql.json",
            headers={
                "X-Shopify-Access-Token": self.settings.shopify_admin_token.get_secret_value()
            },
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise RuntimeError(f"Shopify GraphQL error: {body['errors']}")
        return cast(dict[str, Any], body["data"])

    async def health(self) -> str:
        if not self.configured:
            raise RuntimeError("Shopify analytics credentials are not configured")
        await self._graphql("query { shop { id name } }", {})
        return "connected"

    async def sync(self, since: date) -> list[DailyPerformance]:
        if not self.configured:
            return []
        shopifyql = (
            "FROM sales SHOW gross_sales, discounts, returns, net_sales, orders "
            f"GROUP BY product_id TIMESERIES day SINCE {since.isoformat()} UNTIL today"
        )
        query = """
        query Analytics($query: String!) {
          shopifyqlQuery(query: $query) {
            tableData { columns { name dataType displayName } rows }
            parseErrors
          }
        }
        """
        data = await self._graphql(query, {"query": shopifyql})
        result = data["shopifyqlQuery"]
        if result.get("parseErrors"):
            raise RuntimeError(f"ShopifyQL parse error: {result['parseErrors']}")
        columns = [item["name"] for item in result["tableData"]["columns"]]
        output = []
        for values in result["tableData"]["rows"]:
            row = dict(zip(columns, values, strict=True))
            output.append(
                DailyPerformance(
                    metric_date=date.fromisoformat(str(row.get("day"))[:10]),
                    channel=Channel.SHOPIFY,
                    external_product_id=str(row.get("product_id")),
                    orders=int(row.get("orders") or 0),
                    gross_revenue_cents=_cents(row.get("gross_sales")),
                    refunds_cents=abs(_cents(row.get("returns")) or 0),
                    source="shopify_shopifyql",
                    completeness={"sales": True, "traffic": False},
                )
            )
        return output


class EtsyAnalyticsClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(base_url="https://api.etsy.com/v3", timeout=60)

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.etsy_api_key.get_secret_value()
            and self.settings.etsy_shared_secret.get_secret_value()
            and self.settings.etsy_access_token.get_secret_value()
            and self.settings.etsy_shop_id
        )

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": f"{self.settings.etsy_api_key.get_secret_value()}:{self.settings.etsy_shared_secret.get_secret_value()}",
            "Authorization": f"Bearer {self.settings.etsy_access_token.get_secret_value()}",
        }

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        response = await self.client.get(path, headers=self._headers(), params=params)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    async def health(self) -> str:
        if not self.configured:
            raise RuntimeError("Etsy analytics credentials are not configured")
        await self._get(f"/application/shops/{self.settings.etsy_shop_id}/receipts", limit=1)
        return "connected"

    async def _receipts_since(self, since: date) -> list[dict[str, Any]]:
        """Return a complete bounded receipt window, or fail without partial totals."""
        min_created = int(datetime.combine(since, datetime.min.time(), tzinfo=UTC).timestamp())
        max_created = int(datetime.now(UTC).timestamp())
        receipts: list[dict[str, Any]] = []
        receipt_ids: set[int] = set()
        expected_count: int | None = None
        for _ in range(ETSY_MAX_RECEIPT_PAGES):
            data = await self._get(
                f"/application/shops/{self.settings.etsy_shop_id}/receipts",
                min_created=min_created, max_created=max_created,
                limit=ETSY_RECEIPT_PAGE_SIZE, offset=len(receipts),
                sort_on="receipt_id", sort_order="asc",
            )
            count = int(data["count"])
            if count < 0 or count > ETSY_RECEIPT_PAGE_SIZE * ETSY_MAX_RECEIPT_PAGES:
                raise RuntimeError("Etsy receipt window exceeds supported pagination; import CSV instead")
            if expected_count is not None and count != expected_count:
                raise RuntimeError("Etsy receipt count changed during pagination; retry the sync")
            expected_count = count
            page = data.get("results", [])
            if len(page) > ETSY_RECEIPT_PAGE_SIZE or len(receipts) + len(page) > count:
                raise RuntimeError("Etsy returned an inconsistent receipt page; retry the sync")
            for receipt in page:
                receipt_id = int(receipt["receipt_id"])
                if receipt_id in receipt_ids:
                    raise RuntimeError("Etsy repeated a receipt during pagination; retry the sync")
                receipt_ids.add(receipt_id)
                receipts.append(receipt)
            if len(receipts) == count:
                return receipts
            if not page:
                raise RuntimeError("Etsy returned an incomplete receipt window; retry the sync")
        raise RuntimeError("Etsy receipt pagination limit reached; import CSV instead")

    async def sync(self, since: date) -> list[DailyPerformance]:
        if not self.configured:
            return []
        receipts = await self._receipts_since(since)
        output: dict[tuple[date, str], DailyPerformance] = {}
        for receipt in receipts:
            when = datetime.fromtimestamp(receipt["create_timestamp"], UTC).date()
            receipt_listings: set[str] = set()
            for transaction in receipt.get("transactions", []):
                listing_id = str(transaction.get("listing_id"))
                key = (when, listing_id)
                current = output.get(key)
                amount = transaction.get("price", {})
                quantity = int(transaction.get("quantity", 1))
                revenue = round(Fraction(
                    int(amount.get("amount", 0)) * quantity * 100,
                    max(1, int(amount.get("divisor", 100))),
                ))
                if current is None:
                    current = DailyPerformance(
                        metric_date=when,
                        channel=Channel.ETSY,
                        external_product_id=listing_id,
                        orders=0,
                        units=0,
                        gross_revenue_cents=0,
                        source="etsy_api",
                        completeness={"sales": True, "traffic": False, "receipt_window_complete": True},
                    )
                    output[key] = current
                # Different variations of one listing can share a receipt. They
                # contribute units and revenue separately, but only one order.
                if listing_id not in receipt_listings:
                    current.orders = (current.orders or 0) + 1
                    receipt_listings.add(listing_id)
                current.units = (current.units or 0) + quantity
                current.gross_revenue_cents = (current.gross_revenue_cents or 0) + revenue
        return list(output.values())


class AmazonAnalyticsClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(base_url=settings.amazon_sp_api_url, timeout=60)
        self._token: str | None = None

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.amazon_lwa_client_id.get_secret_value()
            and self.settings.amazon_lwa_client_secret.get_secret_value()
            and self.settings.amazon_refresh_token.get_secret_value()
        )

    async def _access_token(self) -> str:
        if self._token:
            return self._token
        async with httpx.AsyncClient(timeout=30) as auth:
            response = await auth.post(
                "https://api.amazon.com/auth/o2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.settings.amazon_refresh_token.get_secret_value(),
                    "client_id": self.settings.amazon_lwa_client_id.get_secret_value(),
                    "client_secret": self.settings.amazon_lwa_client_secret.get_secret_value(),
                },
            )
            response.raise_for_status()
            self._token = response.json()["access_token"]
            return self._token

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        token = await self._access_token()
        headers = kwargs.pop("headers", {})
        headers["x-amz-access-token"] = token
        response = await self.client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    async def health(self) -> str:
        if not self.configured:
            raise RuntimeError("Amazon SP-API credentials are not configured")
        await self._request(
            "GET",
            "/sales/v1/orderMetrics",
            params={
                "marketplaceIds": self.settings.amazon_marketplace_id,
                "interval": f"{(datetime.now(UTC) - timedelta(days=1)).isoformat()}/{datetime.now(UTC).isoformat()}",
                "granularity": "Day",
            },
        )
        return "connected"

    async def sync(self, since: date) -> list[DailyPerformance]:
        if not self.configured:
            return []
        request = await self._request(
            "POST",
            "/reports/2021-06-30/reports",
            json={
                "reportType": "GET_SALES_AND_TRAFFIC_REPORT",
                "dataStartTime": since.isoformat(),
                "dataEndTime": date.today().isoformat(),
                "marketplaceIds": [self.settings.amazon_marketplace_id],
                "reportOptions": {"dateGranularity": "DAY", "asinGranularity": "SKU"},
            },
        )
        report_id = request["reportId"]
        document_id = None
        for _ in range(12):
            report = await self._request("GET", f"/reports/2021-06-30/reports/{report_id}")
            if report.get("processingStatus") == "DONE":
                document_id = report["reportDocumentId"]
                break
            if report.get("processingStatus") in {"CANCELLED", "FATAL"}:
                raise RuntimeError(f"Amazon report failed: {report.get('processingStatus')}")
            await asyncio.sleep(5)
        if document_id is None:
            raise TimeoutError("Amazon report was not ready before the polling deadline")
        document = await self._request("GET", f"/reports/2021-06-30/documents/{document_id}")
        async with httpx.AsyncClient(timeout=60) as download:
            response = await download.get(document["url"])
            response.raise_for_status()
            payload = response.json()
        output = []
        for row in payload.get("salesAndTrafficByAsin", []):
            sales = row.get("salesByAsin", {})
            traffic = row.get("trafficByAsin", {})
            output.append(
                DailyPerformance(
                    metric_date=date.fromisoformat(
                        payload["reportSpecification"]["dataEndTime"][:10]
                    ),
                    channel=Channel.AMAZON_US,
                    external_product_id=row.get("sku")
                    or row.get("childAsin")
                    or row.get("parentAsin"),
                    impressions=traffic.get("pageViews"),
                    visits=traffic.get("sessions"),
                    orders=sales.get("totalOrderItems"),
                    units=sales.get("unitsOrdered"),
                    gross_revenue_cents=_cents(sales.get("orderedProductSales", {}).get("amount")),
                    refunds_cents=_cents(sales.get("refundAmount", {}).get("amount")),
                    source="amazon_sales_and_traffic",
                    period_start=date.fromisoformat(
                        payload["reportSpecification"]["dataStartTime"][:10]
                    ) if payload["reportSpecification"].get("dataStartTime") else since,
                    period_end=date.fromisoformat(
                        payload["reportSpecification"]["dataEndTime"][:10]
                    ),
                    completeness={"sales": True, "traffic": True, "fees": False},
                )
            )
        return output
