"""Bounded, SSRF-safe competitor image acquisition for reference analysis."""

from __future__ import annotations

import io
import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw

from merch.schemas import MarketplaceSource, ProductOpportunity

IMAGE_HOST_SUFFIXES = {
    MarketplaceSource.ETSY: ("etsy.com", "etsystatic.com", "etsycdn.com"),
    MarketplaceSource.AMAZON_US: (
        "amazon.com",
        "media-amazon.com",
        "ssl-images-amazon.com",
    ),
    MarketplaceSource.TIKTOK_SHOP: ("tiktok.com", "tiktokcdn.com", "byteimg.com"),
    MarketplaceSource.WALMART: ("walmart.com", "walmartimages.com"),
    MarketplaceSource.EBAY: ("ebay.com", "ebayimg.com", "ebaystatic.com"),
}


def _public_https_url(value: str, marketplace: MarketplaceSource) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("reference image URL must use HTTPS")
    hostname = parsed.hostname.casefold()
    if not any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in IMAGE_HOST_SUFFIXES[marketplace]
    ):
        raise ValueError("reference image URL is outside the marketplace allowlist")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("reference image host did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("reference image host resolves outside the public internet")
    return value


def _fixture_reference(label: str, index: int) -> bytes:
    colors = ("#274C77", "#A3CEF1", "#E7ECEF")
    image = Image.new("RGB", (640, 640), colors[index % len(colors)])
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 560, 560), outline="white", width=16)
    draw.text((110, 300), label[:35], fill="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


async def acquire_reference_images(
    opportunity: ProductOpportunity,
    *,
    fake: bool,
) -> list[tuple[str, bytes]]:
    selected = opportunity.comparable_listings[:3]
    if fake:
        return [
            (item.external_listing_id, _fixture_reference(item.product_type, index))
            for index, item in enumerate(selected)
        ]
    result: list[tuple[str, bytes]] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20, connect=8), follow_redirects=False
    ) as client:
        for item in selected:
            if not item.image_urls:
                raise RuntimeError(
                    f"listing {item.external_listing_id} has no reference image evidence"
                )
            url = _public_https_url(item.image_urls[0], item.marketplace)
            response = await client.get(url, headers={"User-Agent": "merch-pod/0.1"})
            response.raise_for_status()
            if len(response.content) > 15_000_000:
                raise RuntimeError("reference image exceeds the 15 MB safety limit")
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if content_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise RuntimeError("reference URL did not return a supported image")
            with Image.open(io.BytesIO(response.content)) as image:
                image.verify()
            result.append((item.external_listing_id, response.content))
    return result
