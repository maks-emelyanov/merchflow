from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast
from weakref import WeakKeyDictionary

import httpx

from merch.config import Settings
from merch.domain.print_areas import largest_compatible_print_area, variant_print_dimensions
from merch.schemas import (
    Channel,
    MarketplaceListing,
    PriceDecision,
    PriceQuote,
    ProductPlanV2,
    ProductTemplate,
)

PRINTIFY_MAX_ENABLED_VARIANTS = 100
PRINTIFY_RANKED_BLUEPRINTS_PATH = "/product-catalog-service/api/v1/blueprints/search"


class ProviderConfigurationError(RuntimeError):
    pass


class PrintifyHTTPError(ProviderConfigurationError):
    def __init__(self, method: str, path: str, status_code: int, detail: str | None = None):
        self.method = method.upper()
        self.path = path
        self.status_code = status_code
        self.status = status_code
        self.detail = (detail or "").strip()[:1000] or None
        message = f"Printify rejected {self.method} {self.path} with status {self.status_code}"
        if self.detail:
            message = f"{message}: {self.detail}"
        super().__init__(message)


class AmbiguousCreateError(RuntimeError):
    """The remote server may have created a product before the request timed out."""


class _RequestPacer:
    def __init__(self, interval: float, concurrency: int):
        self.interval = interval
        self._semaphore = asyncio.Semaphore(concurrency)
        self._start_lock = asyncio.Lock()
        self._last_started_at = 0.0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._semaphore:
            async with self._start_lock:
                loop = asyncio.get_running_loop()
                delay = self.interval - (loop.time() - self._last_started_at)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_started_at = loop.time()
            yield


_CATALOG_PACERS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[float, _RequestPacer]] = (
    WeakKeyDictionary()
)


def _catalog_pacer(interval: float) -> _RequestPacer:
    loop = asyncio.get_running_loop()
    by_interval = _CATALOG_PACERS.setdefault(loop, {})
    return by_interval.setdefault(interval, _RequestPacer(interval, concurrency=4))


class PrintifyClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        public_client: httpx.AsyncClient | None = None,
    ):
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
        self.public_client = public_client
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def close(self) -> None:
        await self.client.aclose()
        if self.public_client is not None and self.public_client is not self.client:
            await self.public_client.aclose()

    async def _paced_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if path.startswith("/catalog/"):
            async with _catalog_pacer(
                self.settings.printify_catalog_request_interval_seconds
            ).slot():
                return await self.client.request(method, path, **kwargs)
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last_request_at
            delay = self.settings.printify_request_interval_seconds - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await self.client.request(method, path, **kwargs)
            finally:
                self._last_request_at = loop.time()

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        fallback = float(min(30, 2**attempt))
        if response is None:
            return fallback
        value = response.headers.get("Retry-After")
        if value is None:
            return fallback
        try:
            delay = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = (retry_at - datetime.now(UTC)).total_seconds()
            except TypeError, ValueError, OverflowError:
                return fallback
        return min(300.0, max(0.1, delay))

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.settings.printify_api_token.get_secret_value():
            raise ProviderConfigurationError("Printify API token is not configured")
        attempts = 8 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            response: httpx.Response | None = None
            try:
                response = await self._paced_request(method, path, **kwargs)
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return {}
                return response.json()
            except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    status_code = exc.response.status_code
                    detail = exc.response.text
                    transient_scope_error = (
                        status_code == 403 and "Invalid scope(s) provided." in detail
                    )
                    retryable_status = (
                        status_code == 429
                        or status_code >= 500
                        or transient_scope_error
                    )
                    if not retryable_status or attempt + 1 >= attempts:
                        raise PrintifyHTTPError(
                            method,
                            path,
                            status_code,
                            detail,
                        ) from exc
                elif attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(self._retry_delay(response, attempt))
        raise RuntimeError("unreachable retry state")

    async def shops(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self._request("GET", "/shops.json"))

    async def blueprints(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self._request("GET", "/catalog/blueprints.json"))

    async def ranked_blueprints(self) -> list[dict[str, Any]]:
        """Return Printify's public Bestsellers in their current displayed order.

        This request deliberately uses a separate, unauthenticated client so the
        private API token can never be forwarded to Printify's public catalog host.
        """
        if self.public_client is None:
            self.public_client = httpx.AsyncClient(
                base_url=self.settings.printify_dashboard_base_url.rstrip("/"),
                headers={"User-Agent": self.settings.printify_user_agent},
                timeout=httpx.Timeout(60, connect=10),
            )

        ranked: list[dict[str, Any]] = []
        seen: set[int] = set()
        page = 1
        total: int | None = None
        while total is None or len(ranked) < total:
            params: dict[str, str | list[str]] = {
                "limit": "100",
                "tags[]": ["Bestsellers"],
            }
            if page > 1:
                params["page"] = str(page)
            response: httpx.Response | None = None
            for attempt in range(8):
                try:
                    response = await self.public_client.get(
                        PRINTIFY_RANKED_BLUEPRINTS_PATH,
                        params=params,
                    )
                    response.raise_for_status()
                    break
                except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError):
                        status_code = exc.response.status_code
                        if status_code != 429 and status_code < 500:
                            raise PrintifyHTTPError(
                                "GET",
                                PRINTIFY_RANKED_BLUEPRINTS_PATH,
                                status_code,
                                exc.response.text,
                            ) from exc
                    if attempt == 7:
                        if isinstance(exc, httpx.HTTPStatusError):
                            raise PrintifyHTTPError(
                                "GET",
                                PRINTIFY_RANKED_BLUEPRINTS_PATH,
                                exc.response.status_code,
                                exc.response.text,
                            ) from exc
                        raise
                    await asyncio.sleep(self._retry_delay(response, attempt))

            if response is None:
                raise RuntimeError("Printify ranked catalog returned no response")
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ProviderConfigurationError(
                    "Printify ranked catalog returned an invalid response"
                )
            if total is None:
                raw_total = payload.get("total")
                total = int(raw_total) if type(raw_total) is int else len(payload["data"])
            added = 0
            for value in payload["data"]:
                if not isinstance(value, dict):
                    continue
                blueprint_id = value.get("blueprintId")
                if type(blueprint_id) is not int or blueprint_id in seen:
                    continue
                seen.add(blueprint_id)
                ranked.append(value)
                added += 1
            if not payload["data"] or added == 0 or len(payload["data"]) < 100:
                break
            page += 1
        return ranked

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

    async def catalog_print_providers(self) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            await self._request("GET", "/catalog/print_providers.json"),
        )

    async def catalog_print_provider(self, print_provider_id: int) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._request("GET", f"/catalog/print_providers/{print_provider_id}.json"),
        )

    async def variants(self, blueprint_id: int, provider_id: int) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._request(
                "GET",
                f"/catalog/blueprints/{blueprint_id}/print_providers/{provider_id}/variants.json",
            ),
        )

    async def shipping(self, blueprint_id: int, provider_id: int) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._request(
                "GET",
                f"/catalog/blueprints/{blueprint_id}/print_providers/{provider_id}/shipping.json",
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

    @staticmethod
    def catalog_product_fingerprint(
        plan: ProductPlanV2,
        listing: MarketplaceListing,
        prices: list[PriceDecision],
        artwork_upload_ids: dict[str, str],
    ) -> str:
        canonical = {
            "schema_version": 2,
            "blueprint_id": plan.blueprint_id,
            "provider_id": plan.print_provider_id,
            "title": listing.title,
            "description": listing.long_description,
            "variants": sorted((item.variant_id, item.item_price_cents) for item in prices),
            "featured_variant_id": plan.featured_variant_id,
            "artwork": sorted(artwork_upload_ids.items()),
        }
        return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()

    def catalog_product_payload(
        self,
        plan: ProductPlanV2,
        listing: MarketplaceListing,
        prices: list[PriceDecision],
        artwork_upload_ids: dict[str, str],
    ) -> dict[str, Any]:
        if len(plan.variants) > PRINTIFY_MAX_ENABLED_VARIANTS:
            raise ValueError(
                "Printify products support at most "
                f"{PRINTIFY_MAX_ENABLED_VARIANTS} enabled variants"
            )
        price_by_variant = {item.variant_id: item.item_price_cents for item in prices}
        if set(price_by_variant) != {item.variant_id for item in plan.variants}:
            raise ValueError("catalog plan variants and price decisions differ")
        artwork_by_signature = {
            item.surface_signature: artwork_upload_ids.get(item.surface_signature)
            for item in plan.surface_artworks
        }
        if not all(artwork_by_signature.values()):
            raise ValueError("catalog plan is missing an uploaded surface artwork")
        grouped: dict[tuple[str, ...], list[int]] = {}
        surfaces_by_group: dict[tuple[str, ...], list[Any]] = {}
        for variant in plan.variants:
            signatures = tuple(sorted(surface.signature for surface in variant.surfaces))
            grouped.setdefault(signatures, []).append(variant.variant_id)
            surfaces_by_group.setdefault(signatures, variant.surfaces)
        print_areas = []
        for signatures, variant_ids in grouped.items():
            surface_by_signature = {
                surface.signature: surface for surface in surfaces_by_group[signatures]
            }
            print_areas.append(
                {
                    "variant_ids": sorted(variant_ids),
                    "placeholders": [
                        {
                            "position": surface_by_signature[signature].position,
                            "decoration_method": surface_by_signature[signature].decoration_method,
                            "images": [
                                {
                                    "id": artwork_by_signature[signature],
                                    "x": 0.5,
                                    "y": 0.5,
                                    "scale": 1.0,
                                    "angle": 0,
                                }
                            ],
                        }
                        for signature in signatures
                    ],
                }
            )
        return {
            "title": listing.title,
            "description": listing.long_description,
            "tags": listing.tags,
            "blueprint_id": plan.blueprint_id,
            "print_provider_id": plan.print_provider_id,
            "variants": [
                {
                    "id": variant.variant_id,
                    "price": price_by_variant[variant.variant_id],
                    "is_enabled": True,
                    "is_default": variant.variant_id == plan.featured_variant_id,
                }
                for variant in plan.variants
            ],
            "print_areas": print_areas,
        }

    def catalog_product_update_payload(
        self,
        plan: ProductPlanV2,
        listing: MarketplaceListing,
        prices: list[PriceDecision],
        artwork_upload_ids: dict[str, str],
        remote_product: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self.catalog_product_payload(plan, listing, prices, artwork_upload_ids)
        selected_variants = {int(item["id"]): item for item in payload["variants"]}
        remote_variants = {
            int(item["id"]): item
            for item in remote_product.get("variants", [])
            if item.get("id") is not None
        }
        if not selected_variants.keys() <= remote_variants.keys():
            raise ValueError("Printify product lacks one or more approved variants")
        payload["variants"] = [
            selected_variants.get(
                variant_id,
                {
                    "id": variant_id,
                    "price": int(remote.get("price") or 0),
                    "is_enabled": False,
                    "is_default": False,
                },
            )
            for variant_id, remote in remote_variants.items()
        ]
        selected_ids = set(selected_variants)
        writable_image_ids = set(artwork_upload_ids.values())
        retained_areas = []
        for area in remote_product.get("print_areas", []):
            variant_ids = [
                int(value)
                for value in area.get("variant_ids", [])
                if int(value) not in selected_ids
            ]
            if variant_ids:
                placeholders = []
                for placeholder in area.get("placeholders", []):
                    images = [
                        {
                            key: image[key]
                            for key in ("id", "x", "y", "scale", "angle", "pattern")
                            if key in image
                        }
                        for image in placeholder.get("images", [])
                        if str(image.get("id") or "") in writable_image_ids
                    ]
                    if not images:
                        continue
                    writable_placeholder = {
                        "position": placeholder["position"],
                        "images": images,
                    }
                    if placeholder.get("decoration_method"):
                        writable_placeholder["decoration_method"] = placeholder["decoration_method"]
                    placeholders.append(writable_placeholder)
                if not placeholders:
                    raise ValueError("Printify retained variant has no reusable uploaded artwork")
                retained_areas.append(
                    {
                        "variant_ids": variant_ids,
                        "placeholders": placeholders,
                    }
                )
        payload["print_areas"] = [*payload["print_areas"], *retained_areas]
        covered_ids = {
            int(value) for area in payload["print_areas"] for value in area.get("variant_ids", [])
        }
        if covered_ids != set(remote_variants):
            raise ValueError("Printify update print areas do not cover every remote variant")
        return payload

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
        except PrintifyHTTPError as exc:
            if exc.status_code >= 500:
                raise AmbiguousCreateError("Printify product creation outcome is unknown") from exc
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise AmbiguousCreateError("Printify product creation outcome is unknown") from exc
            raise

    async def reconcile_product(
        self, shop_id: str, artwork_upload_id: str, listing_title: str
    ) -> list[dict[str, Any]]:
        if self.settings.publish_mode == "dry_run":
            return []
        matches: list[dict[str, Any]] = []
        matched_ids: set[str] = set()
        page_number = 1
        while True:
            page = await self._request("GET", f"/shops/{shop_id}/products.json?page={page_number}")
            for product in page.get("data", []):
                image_ids = {
                    image.get("id")
                    for area in product.get("print_areas", [])
                    for placeholder in area.get("placeholders", [])
                    for image in placeholder.get("images", [])
                }
                product_id = str(product.get("id") or "")
                if (
                    product.get("title") == listing_title
                    and artwork_upload_id in image_ids
                    and product_id not in matched_ids
                ):
                    matches.append(product)
                    matched_ids.add(product_id)
            current_page = int(page.get("current_page") or page_number)
            last_page = int(page.get("last_page") or current_page)
            if last_page < current_page or last_page > 1000:
                raise RuntimeError("Printify product pagination is invalid")
            if current_page >= last_page:
                break
            page_number = current_page + 1
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

    async def update_catalog_product(
        self, shop_id: str, product_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Idempotently replace mutable catalog product fields."""
        mutable = {
            key: payload[key]
            for key in ("title", "description", "tags", "variants", "print_areas")
            if key in payload
        }
        return cast(
            dict[str, Any],
            await self._request(
                "PUT",
                f"/shops/{shop_id}/products/{product_id}.json",
                json=mutable,
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
