"""Compliant browser-first collection of listing-specific marketplace evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from merch.config import Settings, get_settings
from merch.database import session_scope
from merch.repository import ResearchRepository
from merch.schemas import (
    CompetitorListingSnapshot,
    MarketplaceSource,
    SalesSignal,
)
from merch.services.openai_service import OpenAIService
from merch.services.storage import ArtifactStorage


@dataclass(frozen=True)
class MarketplaceAdapter:
    source: MarketplaceSource
    host: str
    search_url: str
    listing_path: re.Pattern[str]
    allowed_domain_suffixes: tuple[str, ...]


ADAPTERS = {
    MarketplaceSource.ETSY: MarketplaceAdapter(
        MarketplaceSource.ETSY,
        "www.etsy.com",
        "https://www.etsy.com/search?q={query}",
        re.compile(r"/listing/\d+"),
        ("etsy.com", "etsystatic.com", "etsycdn.com"),
    ),
    MarketplaceSource.AMAZON_US: MarketplaceAdapter(
        MarketplaceSource.AMAZON_US,
        "www.amazon.com",
        "https://www.amazon.com/s?k={query}",
        re.compile(r"/(?:dp|gp/product)/[A-Z0-9]{10}"),
        ("amazon.com", "media-amazon.com", "ssl-images-amazon.com"),
    ),
    MarketplaceSource.TIKTOK_SHOP: MarketplaceAdapter(
        MarketplaceSource.TIKTOK_SHOP,
        "shop.tiktok.com",
        "https://shop.tiktok.com/us/search?q={query}",
        re.compile(r"/(?:view/product|pdp)/[^/?#]+"),
        ("tiktok.com", "tiktokcdn.com", "byteimg.com"),
    ),
    MarketplaceSource.WALMART: MarketplaceAdapter(
        MarketplaceSource.WALMART,
        "www.walmart.com",
        "https://www.walmart.com/search?q={query}",
        re.compile(r"/ip/(?:[^/?#]+/)?\d+"),
        ("walmart.com", "walmartimages.com"),
    ),
    MarketplaceSource.EBAY: MarketplaceAdapter(
        MarketplaceSource.EBAY,
        "www.ebay.com",
        "https://www.ebay.com/sch/i.html?_nkw={query}",
        re.compile(r"/itm/(?:[^/?#]+/)?\d+"),
        ("ebay.com", "ebayimg.com", "ebaystatic.com"),
    ),
}

_CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "robot check",
    "unusual traffic",
    "access denied",
    "sign in to continue",
)
_MONEY = re.compile(r"\$\s*(\d[\d,]*)(?:\.(\d{2}))?")


def contains_access_challenge(text: str) -> bool:
    folded = text.casefold()
    return any(marker in folded for marker in _CHALLENGE_MARKERS)


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.json_scripts: list[str] = []
        self._json_script = False
        self._buffer: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "meta":
            key = attributes.get("property") or attributes.get("name")
            if key and attributes.get("content"):
                self.meta[key.casefold()] = attributes["content"]
        if tag.casefold() == "script" and attributes.get("type", "").casefold() in {
            "application/ld+json",
            "application/json",
        }:
            self._json_script = True
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._json_script:
            self.json_scripts.append("".join(self._buffer))
            self._json_script = False
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._json_script:
            self._buffer.append(data)
        elif data.strip():
            self.text.append(data.strip())


def _price_cents(value: object) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value) * 100)
    if not isinstance(value, str):
        return None
    match = _MONEY.search(value)
    if match:
        return int(match.group(1).replace(",", "")) * 100 + int(match.group(2) or 0)
    try:
        return round(float(value.replace(",", "")) * 100)
    except ValueError:
        return None


def _product_json(values: list[Any]) -> dict[str, Any]:
    queue = list(values)
    while queue:
        value = queue.pop(0)
        if isinstance(value, list):
            queue.extend(value)
        elif isinstance(value, dict):
            kind = value.get("@type")
            if kind == "Product" or (isinstance(kind, list) and "Product" in kind):
                return value
            graph = value.get("@graph")
            if isinstance(graph, list):
                queue.extend(graph)
    return {}


def _external_id(source: MarketplaceSource, url: str, product: dict[str, Any]) -> str:
    path = urlparse(url).path
    patterns = {
        MarketplaceSource.ETSY: r"/listing/(\d+)",
        MarketplaceSource.AMAZON_US: r"/(?:dp|gp/product)/([A-Z0-9]{10})",
        MarketplaceSource.TIKTOK_SHOP: r"/(?:view/product|pdp)/([^/?#]+)",
        MarketplaceSource.WALMART: r"/ip/(?:[^/?#]+/)?(\d+)",
        MarketplaceSource.EBAY: r"/itm/(?:[^/?#]+/)?(\d+)",
    }
    match = re.search(patterns[source], path, re.I)
    if match:
        return match.group(1)
    for key in ("productID", "sku", "mpn"):
        if product.get(key):
            return str(product[key])
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def extract_listing_snapshot(
    *,
    source: MarketplaceSource,
    url: str,
    html: str,
    product_type: str,
    collected_at: datetime,
    page_artifact_key: str | None = None,
    screenshot_artifact_key: str | None = None,
) -> CompetitorListingSnapshot:
    parser = _MetadataParser()
    parser.feed(html)
    payloads: list[Any] = []
    for value in parser.json_scripts:
        try:
            payloads.append(json.loads(value))
        except json.JSONDecodeError:
            continue
    product = _product_json(payloads)
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    aggregate = product.get("aggregateRating") or {}
    title = str(
        product.get("name")
        or parser.meta.get("og:title")
        or parser.meta.get("twitter:title")
        or product_type
    ).strip()
    displayed = _price_cents(
        offers.get("price") or offers.get("lowPrice") or parser.meta.get("product:price:amount")
    )
    body = " ".join(parser.text)
    body_folded = body.casefold()
    shipping: int | None = None
    free_shipping = re.search(r"\bfree shipping\b|\bfree delivery\b", body_folded)
    shipping_match = re.search(r"(?:shipping|delivery)[^$]{0,30}(\$\s*[\d,.]+)", body, re.I)
    if free_shipping:
        shipping = 0
    elif shipping_match:
        shipping = _price_cents(shipping_match.group(1))
    signals: list[SalesSignal] = []
    if re.search(r"\bbest\s*seller\b|\bbestseller\b", body_folded):
        signals.append(
            SalesSignal(
                kind="bestseller_badge",
                value=1,
                label="Visible bestseller badge",
                explicit=True,
                observed_at=collected_at,
            )
        )
    sold = re.search(r"([\d,.]+)\+?\s+(?:sold|purchased)", body_folded)
    if sold:
        signals.append(
            SalesSignal(
                kind="sold_count",
                value=float(sold.group(1).replace(",", "")),
                label=f"Visible sold count: {sold.group(0)}",
                explicit=True,
                observed_at=collected_at,
            )
        )
    rank = re.search(r"best sellers rank[^#]{0,40}#?([\d,]+)", body_folded)
    if rank:
        signals.append(
            SalesSignal(
                kind="sales_rank",
                value=float(rank.group(1).replace(",", "")),
                label=f"Visible sales rank #{rank.group(1)}",
                explicit=True,
                observed_at=collected_at,
            )
        )
    review_count_raw = aggregate.get("reviewCount") or aggregate.get("ratingCount")
    try:
        review_count = int(str(review_count_raw).replace(",", ""))
    except TypeError, ValueError:
        review_count = None
    if review_count is not None:
        signals.append(
            SalesSignal(
                kind="review_count",
                value=review_count,
                label=f"{review_count} visible reviews",
                explicit=False,
                observed_at=collected_at,
            )
        )
    raw_rating = aggregate.get("ratingValue")
    try:
        rating = float(raw_rating) if raw_rating is not None else None
    except TypeError, ValueError:
        rating = None
    images = product.get("image") or parser.meta.get("og:image") or []
    if isinstance(images, str):
        images = [images]
    seller = product.get("brand") or offers.get("seller")
    if isinstance(seller, dict):
        seller = seller.get("name")
    confidence = 90 if any(item.explicit for item in signals) else 65 if signals else 35
    limitations = []
    if not any(item.explicit for item in signals):
        limitations.append("No explicit unit-sales or bestseller signal was visible")
    if shipping is None:
        limitations.append("Delivered price is unavailable because shipping was not visible")
    return CompetitorListingSnapshot(
        marketplace=source,
        external_listing_id=_external_id(source, url, product),
        url=url,
        title=title,
        seller=str(seller) if seller else None,
        product_type=product_type,
        attributes={},
        displayed_price_cents=displayed,
        shipping_price_cents=shipping,
        delivered_price_cents=(
            displayed + shipping if displayed is not None and shipping is not None else None
        ),
        rating=rating,
        review_count=review_count,
        sales_signals=signals,
        image_urls=[str(item) for item in images if str(item).startswith(("http://", "https://"))],
        collected_at=collected_at,
        source_method="browser",
        confidence=confidence,
        limitations=limitations,
        page_artifact_key=page_artifact_key,
        screenshot_artifact_key=screenshot_artifact_key,
    )


async def _robots_allowed(url: str, settings: Settings) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(robots_url, headers={"User-Agent": "merch-pod/0.1"})
        if response.status_code >= 400:
            return False
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        return parser.can_fetch("merch-pod/0.1", url)
    except httpx.HTTPError:
        return False


def _safe_listing_url(adapter: MarketplaceAdapter, value: str) -> str | None:
    url = urljoin(f"https://{adapter.host}", value)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        adapter.host,
        adapter.host.removeprefix("www."),
    }:
        return None
    if not adapter.listing_path.search(parsed.path):
        return None
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


async def _browser_collect(
    adapter: MarketplaceAdapter,
    query: str,
    settings: Settings,
) -> list[CompetitorListingSnapshot]:
    from playwright.async_api import async_playwright

    search_url = adapter.search_url.format(query=quote_plus(query))
    if not await _robots_allowed(search_url, settings):
        raise RuntimeError(
            f"robots policy does not allow browser collection for {adapter.source.value}"
        )
    storage = ArtifactStorage(settings)
    storage.ensure_bucket()
    results: list[CompetitorListingSnapshot] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.browser_headless)
        context = await browser.new_context(
            user_agent="merch-pod/0.1 marketplace research; contact operator",
            locale="en-US",
            service_workers="block",
        )

        async def allowlisted_route(route: Any) -> None:
            hostname = (urlparse(route.request.url).hostname or "").casefold()
            allowed = any(
                hostname == suffix or hostname.endswith(f".{suffix}")
                for suffix in adapter.allowed_domain_suffixes
            )
            if allowed:
                await route.continue_()
            else:
                await route.abort()

        await context.route("**/*", allowlisted_route)
        page = await context.new_page()
        page.set_default_timeout(settings.browser_navigation_timeout_seconds * 1000)
        await page.goto(search_url, wait_until="domcontentloaded")
        body = (await page.locator("body").inner_text()).casefold()
        if contains_access_challenge(body):
            raise RuntimeError(f"{adapter.source.value} presented an access challenge")
        hrefs = await page.locator("a[href]").evaluate_all(
            "elements => elements.map(element => element.href)"
        )
        listing_urls = list(
            dict.fromkeys(safe for href in hrefs if (safe := _safe_listing_url(adapter, str(href))))
        )[: settings.research_listings_per_source]
        for listing_url in listing_urls:
            if not await _robots_allowed(listing_url, settings):
                continue
            await page.goto(listing_url, wait_until="domcontentloaded")
            html = await page.content()
            body = (await page.locator("body").inner_text()).casefold()
            if contains_access_challenge(body):
                continue
            screenshot = await page.screenshot(full_page=False)
            page_key, _ = storage.put(
                html.encode(), suffix="html", content_type="text/html; charset=utf-8"
            )
            screenshot_key, _ = storage.put(screenshot, suffix="png", content_type="image/png")
            results.append(
                extract_listing_snapshot(
                    source=adapter.source,
                    url=listing_url,
                    html=html,
                    product_type=query,
                    collected_at=datetime.now(UTC),
                    page_artifact_key=page_key,
                    screenshot_artifact_key=screenshot_key,
                )
            )
        await browser.close()
    return results


async def _fallback_collect(
    source: MarketplaceSource, query: str, settings: Settings
) -> list[CompetitorListingSnapshot]:
    now = datetime.now(UTC)
    result = await OpenAIService(settings).marketplace_fallback(
        source, query, current_time=now.isoformat()
    )
    snapshots = []
    for item in result.value.listings:
        if settings.provider_mode != "fake":
            safe_url = _safe_listing_url(ADAPTERS[source], item.url)
            if safe_url is None:
                continue
        else:
            safe_url = item.url
        shipping = item.shipping_price_cents
        displayed = item.displayed_price_cents
        snapshots.append(
            CompetitorListingSnapshot(
                marketplace=source,
                external_listing_id=item.external_listing_id,
                url=safe_url,
                title=item.title,
                seller=item.seller,
                product_type=query,
                displayed_price_cents=displayed,
                shipping_price_cents=shipping,
                delivered_price_cents=(
                    displayed + shipping if displayed is not None and shipping is not None else None
                ),
                rating=item.rating,
                review_count=item.review_count,
                sales_signals=item.sales_signals,
                image_urls=item.image_urls,
                collected_at=now,
                source_method="search_fallback",
                confidence=35,
                limitations=[
                    "Search fallback; direct marketplace page was unavailable",
                    *item.limitations,
                ],
            )
        )
    return snapshots


async def collect_marketplace_evidence(
    query: str,
    settings: Settings | None = None,
) -> list[CompetitorListingSnapshot]:
    settings = settings or get_settings()
    results: list[CompetitorListingSnapshot] = []
    for source, adapter in ADAPTERS.items():
        try:
            direct = (
                []
                if settings.provider_mode == "fake"
                else await _browser_collect(adapter, query, settings)
            )
            if not direct:
                raise RuntimeError("no direct listing evidence was collected")
            results.extend(direct)
        except Exception:
            if settings.search_fallback_enabled:
                results.extend(await _fallback_collect(source, query, settings))
    unique: dict[tuple[MarketplaceSource, str], CompetitorListingSnapshot] = {}
    for item in results:
        unique[(item.marketplace, item.external_listing_id)] = item
    snapshots = list(unique.values())
    with session_scope() as session:
        repository = ResearchRepository(session)
        for snapshot in snapshots:
            repository.save_snapshot(snapshot)
    return snapshots


async def source_health(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    settings = settings or get_settings()
    output: dict[str, dict[str, Any]] = {}
    for source, adapter in ADAPTERS.items():
        search_url = adapter.search_url.format(query="test")
        allowed = (
            True
            if settings.provider_mode == "fake"
            else await _robots_allowed(search_url, settings)
        )
        output[source.value] = {
            "healthy": allowed or settings.search_fallback_enabled,
            "direct_collection_allowed": allowed,
            "search_fallback_enabled": settings.search_fallback_enabled,
        }
    return output
