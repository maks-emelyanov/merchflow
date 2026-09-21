from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any, cast

import httpx

from merch.config import Settings
from merch.domain.print_areas import largest_compatible_print_area, variant_print_dimensions
from merch.schemas import Channel, MarketplaceListing, PriceQuote, ProductTemplate


class ProviderConfigurationError(RuntimeError):
    pass


class AmbiguousCreateError(RuntimeError):
    """The remote server may have created a product before the request timed out."""


class PrintifyClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            base_url=settings.printify_base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {settings.printify_api_token.get_secret_value()}",
                "User-Agent": settings.printify_user_agent,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(60, connect=10),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.settings.printify_api_token.get_secret_value():
            raise ProviderConfigurationError("Printify API token is not configured")
        attempts = 4 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            try:
                response = await self.client.request(method, path, **kwargs)
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return {}
                return response.json()
            except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                retryable_status = isinstance(exc, httpx.HTTPStatusError) and (
                    exc.response.status_code == 429 or exc.response.status_code >= 500
                )
                if isinstance(exc, httpx.HTTPStatusError) and not retryable_status:
                    raise ProviderConfigurationError(
                        f"Printify rejected {method} {path} with status {exc.response.status_code}"
                    ) from exc
                if attempt + 1 >= attempts or (
                    isinstance(exc, httpx.HTTPStatusError) and not retryable_status
                ):
                    raise
                await asyncio.sleep(min(8, 2**attempt))
        raise RuntimeError("unreachable retry state")

    async def shops(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self._request("GET", "/shops.json"))

    async def blueprints(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self._request("GET", "/catalog/blueprints.json"))

    async def blueprint(self, blueprint_id: int) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._request("GET", f"/catalog/blueprints/{blueprint_id}.json"),
        )

    async def print_providers(self, blueprint_id: int) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            await self._request("GET", f"/catalog/blueprints/{blueprint_id}/print_providers.json"),
        )

    async def variants(self, blueprint_id: int, provider_id: int) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._request(
                "GET",
                f"/catalog/blueprints/{blueprint_id}/print_providers/{provider_id}/variants.json",
            ),
        )

    async def validate_template(self, template: ProductTemplate) -> ProductTemplate:
        if self.settings.provider_mode == "fake":
            return template
        catalog = await self.variants(template.blueprint_id, template.print_provider_id)
        current = {int(item["id"]): item for item in catalog.get("variants", [])}
        updated = []
        dimensions: list[tuple[int, int]] = []
        for variant in template.variants:
            remote = current.get(variant.variant_id)
            if remote is None or not remote.get("is_available", True):
                raise ProviderConfigurationError(
                    f"Printify variant {variant.variant_id} is unavailable"
                )
            try:
                dimensions.append(
                    variant_print_dimensions(
                        remote,
                        position=template.position,
                        decoration_method=template.decoration_method,
                    )
                )
            except ValueError as exc:
                raise ProviderConfigurationError(
                    f"Printify variant {variant.variant_id} has an invalid print area: {exc}"
                ) from exc
            data = variant.model_dump()
            data["production_cost_cents"] = int(remote.get("cost", variant.production_cost_cents))
            updated.append(type(variant).model_validate(data))
        try:
            print_width, print_height = largest_compatible_print_area(dimensions)
        except ValueError as exc:
            raise ProviderConfigurationError(
                f"Printify variants have incompatible print areas: {exc}"
            ) from exc
        data = template.model_dump()
        data["variants"] = [item.model_dump() for item in updated]
        data["print_width"] = print_width
        data["print_height"] = print_height
        return ProductTemplate.model_validate(data)

    async def upload_image(self, filename: str, data: bytes) -> dict[str, Any]:
        if self.settings.publish_mode == "dry_run":
            return {
                "id": f"dry-upload-{hashlib.sha256(data).hexdigest()[:16]}",
                "file_name": filename,
            }
        return cast(
            dict[str, Any],
            await self._request(
                "POST",
                "/uploads/images.json",
                json={"file_name": filename, "contents": base64.b64encode(data).decode()},
            ),
        )

    @staticmethod
    def product_fingerprint(
        template: ProductTemplate,
        listing: MarketplaceListing,
        quotes: list[PriceQuote],
        artwork_upload_id: str,
    ) -> str:
        canonical = {
            "blueprint_id": template.blueprint_id,
            "provider_id": template.print_provider_id,
            "title": listing.title,
            "description": listing.long_description,
            "quotes": sorted([(quote.variant_id, quote.retail_price_cents) for quote in quotes]),
            "featured_variant_id": template.featured_variant().variant_id,
            "artwork": artwork_upload_id,
        }
        return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()

    def product_payload(
        self,
        template: ProductTemplate,
        listing: MarketplaceListing,
        quotes: list[PriceQuote],
        artwork_upload_id: str,
    ) -> dict[str, Any]:
        prices = {quote.variant_id: quote.retail_price_cents for quote in quotes}
        enabled = [item for item in template.variants if item.enabled and item.variant_id in prices]
        featured_id = template.featured_variant().variant_id
        if featured_id not in prices:
            raise ValueError("featured variant is missing an approved price")
        return {
            "title": listing.title,
            "description": listing.long_description,
            "tags": listing.tags,
            "blueprint_id": template.blueprint_id,
            "print_provider_id": template.print_provider_id,
            "variants": [
                {
                    "id": variant.variant_id,
                    "price": prices[variant.variant_id],
                    "is_enabled": True,
                    "is_default": variant.variant_id == featured_id,
                }
                for variant in enabled
            ],
            "print_areas": [
                {
                    "variant_ids": [variant.variant_id for variant in enabled],
                    "placeholders": [
                        {
                            "position": template.position,
                            "images": [
                                {
                                    "id": artwork_upload_id,
                                    "x": 0.5,
                                    "y": 0.5,
                                    "scale": 1.0,
                                    "angle": 0,
                                }
                            ],
                        }
                    ],
                }
            ],
        }

    async def create_product(self, shop_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.settings.publish_mode == "dry_run":
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
            return {"id": f"dry-product-{digest}", **payload}
        try:
            return cast(
                dict[str, Any],
                await self._request("POST", f"/shops/{shop_id}/products.json", json=payload),
            )
        except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectError) as exc:
            raise AmbiguousCreateError("Printify product creation outcome is unknown") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise AmbiguousCreateError("Printify product creation outcome is unknown") from exc
            raise

    async def reconcile_product(
        self, shop_id: str, artwork_upload_id: str, listing_title: str
    ) -> list[dict[str, Any]]:
        if self.settings.publish_mode == "dry_run":
            return []
        page = await self._request("GET", f"/shops/{shop_id}/products.json?limit=100")
        matches = []
        for product in page.get("data", []):
            image_ids = {
                image.get("id")
                for area in product.get("print_areas", [])
                for placeholder in area.get("placeholders", [])
                for image in placeholder.get("images", [])
            }
            if product.get("title") == listing_title and artwork_upload_id in image_ids:
                matches.append(product)
        return matches

    async def publish(self, shop_id: str, product_id: str) -> dict[str, Any]:
        if self.settings.publish_mode == "dry_run":
            return {"status": "dry_run", "product_id": product_id}
        payload = {
            "title": True,
            "description": True,
            "images": True,
            "variants": True,
            "tags": True,
            "keyFeatures": True,
            "shipping_template": True,
        }
        for attempt in range(4):
            try:
                return cast(
                    dict[str, Any],
                    await self._request(
                        "POST",
                        f"/shops/{shop_id}/products/{product_id}/publish.json",
                        json=payload,
                    ),
                )
            except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code == 429 or exc.response.status_code >= 500
                )
                if not retryable or attempt == 3:
                    raise
                await asyncio.sleep(min(8, 2**attempt))
        raise RuntimeError("unreachable retry state")

    async def product(self, shop_id: str, product_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._request("GET", f"/shops/{shop_id}/products/{product_id}.json"),
        )

    async def update_product_copy(
        self, shop_id: str, product_id: str, title: str, description: str, tags: list[str]
    ) -> dict[str, Any]:
        """Update text on an existing product without touching its variants or artwork."""
        return cast(
            dict[str, Any],
            await self._request(
                "PUT",
                f"/shops/{shop_id}/products/{product_id}.json",
                json={"title": title, "description": description, "tags": tags},
            ),
        )

    async def update_product_print_areas(
        self, shop_id: str, product_id: str, print_areas: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Replace artwork placement on an existing product without changing its variants."""
        if not print_areas:
            raise ValueError("Printify product artwork requires at least one print area")
        return cast(
            dict[str, Any],
            await self._request(
                "PUT",
                f"/shops/{shop_id}/products/{product_id}.json",
                json={"print_areas": print_areas},
            ),
        )

    async def publishing_succeeded(
        self, shop_id: str, product_id: str, listing_id: int, handle: str
    ) -> None:
        await self._request(
            "POST",
            f"/shops/{shop_id}/products/{product_id}/publishing_succeeded.json",
            json={"external": {"id": str(listing_id), "handle": handle}},
        )

    async def orders(self, shop_id: str, page: int = 1) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._request("GET", f"/shops/{shop_id}/orders.json?page={page}"),
        )

    async def webhooks(self, shop_id: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            await self._request("GET", f"/shops/{shop_id}/webhooks.json"),
        )

    async def create_webhook(
        self, shop_id: str, topic: str, url: str, secret: str
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._request(
                "POST",
                f"/shops/{shop_id}/webhooks.json",
                json={"topic": topic, "url": url, "secret": secret},
            ),
        )

    async def delete_webhook(self, shop_id: str, webhook_id: str) -> None:
        await self._request("DELETE", f"/shops/{shop_id}/webhooks/{webhook_id}.json")


def channel_shop(template: ProductTemplate, channel: Channel) -> str:
    match = next(
        (item for item in template.channels if item.channel == channel and item.enabled), None
    )
    if match is None or not match.printify_shop_id:
        raise ProviderConfigurationError(f"Printify shop is not configured for {channel.value}")
    return match.printify_shop_id
