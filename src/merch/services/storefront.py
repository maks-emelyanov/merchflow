"""Read and verify the live Etsy listing created by a Printify publish."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from merch.config import Settings
from merch.schemas import PriceQuote, ProductTemplate
from merch.services.credentials import redact


class StorefrontVerificationError(RuntimeError):
    """The channel is live or may be live, but its approved state is unverified."""


def printify_listing_id(product: dict[str, Any]) -> int:
    external = product.get("external") or {}
    value = external.get("id") if isinstance(external, dict) else None
    if value is None:
        raise StorefrontVerificationError("Printify has not supplied the Etsy listing ID")
    try:
        listing_id = int(value)
    except (TypeError, ValueError) as exc:
        raise StorefrontVerificationError("Printify has not supplied the Etsy listing ID") from exc
    if listing_id < 1:
        raise StorefrontVerificationError("Printify supplied an invalid Etsy listing ID")
    return listing_id


def verify_printify_product(
    product: dict[str, Any], template: ProductTemplate, quotes: list[PriceQuote]
) -> str:
    expected = {quote.variant_id: quote.retail_price_cents for quote in quotes}
    enabled = {
        int(item["id"]): item
        for item in product.get("variants", [])
        if item.get("is_enabled")
    }
    template_enabled = {item.variant_id for item in template.variants if item.enabled}
    if set(enabled) != set(expected) or set(expected) != template_enabled:
        raise StorefrontVerificationError("Printify enabled variants differ from approved variants")
    if any(int(enabled[variant_id].get("price", -1)) != price for variant_id, price in expected.items()):
        raise StorefrontVerificationError("Printify prices differ from approved prices")
    featured_id = template.featured_variant().variant_id
    if {variant_id for variant_id, item in enabled.items() if item.get("is_default")} != {featured_id}:
        raise StorefrontVerificationError("Printify default variant is not the featured variant")
    # Import at call time because selection uses this module's verification error.
    from merch.services.mockup_selection import mockup_plan

    # This verifier also serves Shopify and Amazon. Their product validation only
    # needs the featured variant; Etsy's complete gallery policy belongs to its
    # prepare/readback gates and must not impose its photo limit on other channels.
    featured_template = template.model_copy(update={"variants": [template.featured_variant()]})
    source = mockup_plan(product, featured_template)[0][1]
    parsed = urlparse(source)
    if parsed.scheme != "https" or not parsed.hostname:
        raise StorefrontVerificationError("Printify supplied an invalid featured mockup URL")
    return source


def _money_cents(value: Any) -> int:
    if not isinstance(value, dict) or value.get("currency_code") != "USD":
        raise StorefrontVerificationError("Etsy returned an unsupported price currency")
    try:
        amount = Decimal(str(value["amount"]))
        divisor = Decimal(str(value["divisor"]))
        cents = amount * 100 / divisor
    except (KeyError, ArithmeticError, ValueError) as exc:
        raise StorefrontVerificationError("Etsy returned an invalid variant price") from exc
    if divisor <= 0 or cents != cents.to_integral_value():
        raise StorefrontVerificationError("Etsy returned an invalid variant price")
    return int(cents)


def verify_etsy_inventory(
    inventory: dict[str, Any],
    product: dict[str, Any],
    template: ProductTemplate,
    quotes: list[PriceQuote],
) -> None:
    expected_prices = {quote.variant_id: quote.retail_price_cents for quote in quotes}
    printify_variants = {int(item["id"]): item for item in product.get("variants", [])}
    expected_by_sku = {
        str(printify_variants[variant_id].get("sku") or ""): price
        for variant_id, price in expected_prices.items()
    }
    active_products = [
        item
        for item in inventory.get("products", [])
        if not item.get("is_deleted")
        and any(
            offer.get("is_enabled") and not offer.get("is_deleted")
            for offer in item.get("offerings", [])
        )
    ]
    use_sku = (
        len(expected_by_sku) == len(expected_prices)
        and "" not in expected_by_sku
        and all(str(item.get("sku") or "") for item in active_products)
    )
    expected_by_options = {
        (item.color.casefold(), item.size.casefold()): expected_prices[item.variant_id]
        for item in template.variants
        if item.variant_id in expected_prices
    }
    actual: dict[str | tuple[str, str], int] = {}
    for item in active_products:
        offerings = [
            offer
            for offer in item.get("offerings", [])
            if offer.get("is_enabled") and not offer.get("is_deleted")
        ]
        if not offerings:
            continue
        if len(offerings) != 1:
            raise StorefrontVerificationError("Etsy variant has multiple enabled prices")
        offer = offerings[0]
        if int(offer.get("quantity") or 0) < 1:
            raise StorefrontVerificationError("Etsy has an enabled variant with no inventory")
        if use_sku:
            key: str | tuple[str, str] = str(item.get("sku") or "")
        else:
            properties = {
                str(prop.get("property_name") or "").casefold(): str((prop.get("values") or [""])[0]).casefold()
                for prop in item.get("property_values", [])
            }
            colors = [value for name, value in properties.items() if "color" in name]
            sizes = [value for name, value in properties.items() if "size" in name]
            if len(colors) != 1 or len(sizes) != 1:
                raise StorefrontVerificationError("Etsy inventory lacks SKU or color/size data")
            key = (colors[0], sizes[0])
        if key in actual:
            raise StorefrontVerificationError("Etsy returned duplicate enabled variants")
        actual[key] = _money_cents(offer.get("price"))
    expected: dict[str | tuple[str, str], int] = {}
    for key, price in (expected_by_sku if use_sku else expected_by_options).items():
        expected[key] = price
    if actual != expected:
        missing = len(expected.keys() - actual.keys())
        extra = len(actual.keys() - expected.keys())
        wrong_prices = sum(
            actual[key] != price for key, price in expected.items() if key in actual
        )
        raise StorefrontVerificationError(
            f"Etsy variants or prices differ from approval (missing={missing}, extra={extra}, prices={wrong_prices})"
        )


SELECTOR_IDS = {"Size": 513, "Color": 514}
INVENTORY_DEPENDENCIES = (
    "price_on_property",
    "quantity_on_property",
    "sku_on_property",
    "readiness_state_on_property",
)


def normalize_etsy_inventory_dependencies(payload: dict[str, Any]) -> dict[str, Any]:
    """Derive price dependencies from offerings and keep other dependencies compatible."""
    selector_ids = set(SELECTOR_IDS.values())
    dependencies = {
        key: {int(value) for value in payload.get(key) or []}
        for key in INVENTORY_DEPENDENCIES
    }
    if any(values - selector_ids for values in dependencies.values()):
        raise StorefrontVerificationError("Etsy inventory dependency uses an unknown variation")
    prices = _enabled_inventory_prices(payload)
    if prices is not None:
        dependencies["price_on_property"] = selector_ids if len(prices) > 1 else set()
    use_both = any(values == selector_ids for values in dependencies.values())
    return {
        **payload,
        **{
            key: [value for value in SELECTOR_IDS.values() if use_both or value in values]
            if values else []
            for key, values in dependencies.items()
        },
    }


def _enabled_inventory_prices(payload: dict[str, Any]) -> set[Decimal] | None:
    """Infer only from complete enabled offerings; retain metadata on partial inputs."""
    prices: set[Decimal] = set()
    products = payload.get("products")
    if not isinstance(products, list):
        return None
    for product in products:
        if not isinstance(product, dict):
            return None
        if product.get("is_deleted"):
            continue
        offerings = product.get("offerings")
        if not isinstance(offerings, list) or not offerings:
            return None
        for offering in offerings:
            if not isinstance(offering, dict):
                return None
            if offering.get("is_deleted") or offering.get("is_enabled") is False:
                continue
            if offering.get("is_enabled") is not True or offering.get("price") is None:
                return None
            raw_price = offering["price"]
            try:
                price = (
                    Decimal(_money_cents(raw_price)) / 100 if isinstance(raw_price, dict)
                    else Decimal(str(raw_price))
                )
            except (InvalidOperation, StorefrontVerificationError, ValueError):
                return None
            if not price.is_finite() or price < 0:
                return None
            prices.add(price)
    return prices or None


def _inventory_products(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    products = [item for item in inventory.get("products", []) if not item.get("is_deleted")]
    if not products:
        raise StorefrontVerificationError("Etsy inventory has no products")
    return products


def selector_labels_are_exact(inventory: dict[str, Any]) -> bool:
    """Check every retained product, including disabled ones, for the buyer-facing labels."""
    return all(
        sorted(str(prop.get("property_name") or "") for prop in item.get("property_values", []))
        == ["Color", "Size"]
        for item in _inventory_products(inventory)
    )


def verify_etsy_selector_labels(inventory: dict[str, Any]) -> None:
    if not selector_labels_are_exact(inventory):
        raise StorefrontVerificationError('Etsy selectors must be named exactly "Size" and "Color"')


def _selector_role(prop: dict[str, Any]) -> str:
    name = str(prop.get("property_name") or "").casefold()
    if "size" in name and "color" not in name:
        return "Size"
    if "color" in name and "size" not in name:
        return "Color"
    raise StorefrontVerificationError(f"Cannot identify Etsy variation: {name!r}")


def build_etsy_selector_inventory(
    inventory: dict[str, Any],
) -> tuple[dict[str, Any], dict[int, int]]:
    """Build a complete Etsy inventory update with custom Size and Color variations."""
    products: list[dict[str, Any]] = []
    old_to_new: dict[int, int] = {}
    for item in _inventory_products(inventory):
        properties = item.get("property_values") or []
        if len(properties) != 2:
            raise StorefrontVerificationError("Etsy product must have one size and one color")
        by_role = {_selector_role(prop): prop for prop in properties}
        if set(by_role) != set(SELECTOR_IDS):
            raise StorefrontVerificationError("Etsy product must have one size and one color")
        new_properties = []
        for role, new_id in SELECTOR_IDS.items():
            old = by_role[role]
            old_id = int(old["property_id"])
            if old_id in old_to_new and old_to_new[old_id] != new_id:
                raise StorefrontVerificationError("Etsy variation property IDs are inconsistent")
            old_to_new[old_id] = new_id
            values = old.get("values") or []
            if len(values) != 1 or not str(values[0]):
                raise StorefrontVerificationError("Etsy variation has no unique value")
            new_properties.append(
                {
                    "property_id": new_id,
                    "property_name": role,
                    "value_ids": [],
                    "values": [str(values[0])],
                    "scale_id": None,
                }
            )
        offerings = []
        for offer in item.get("offerings", []):
            if offer.get("is_deleted"):
                continue
            cents = _money_cents(offer.get("price"))
            updated_offer: dict[str, Any] = {
                "price": float(Decimal(cents) / 100),
                "quantity": int(offer["quantity"]),
                "is_enabled": bool(offer["is_enabled"]),
            }
            if offer.get("readiness_state_id") is not None:
                updated_offer["readiness_state_id"] = int(offer["readiness_state_id"])
            offerings.append(updated_offer)
        if not offerings:
            raise StorefrontVerificationError("Etsy product has no offerings")
        products.append(
            {
                "sku": str(item.get("sku") or ""),
                "offerings": offerings,
                "property_values": new_properties,
            }
        )
    if len(set(old_to_new.values())) != 2:
        raise StorefrontVerificationError("Etsy inventory lacks size or color variation IDs")
    payload: dict[str, Any] = {"products": products}
    for key in INVENTORY_DEPENDENCIES:
        old_ids = inventory.get(key) or []
        if any(int(old_id) not in old_to_new for old_id in old_ids):
            raise StorefrontVerificationError(f"Etsy {key} uses an unknown variation")
        mapped_ids = {old_to_new[int(old_id)] for old_id in old_ids}
        payload[key] = [new_id for new_id in SELECTOR_IDS.values() if new_id in mapped_ids]
    return normalize_etsy_inventory_dependencies(payload), old_to_new


def plan_etsy_variation_images(
    original_images: list[dict[str, Any]],
    original_inventory: dict[str, Any],
    old_to_new: dict[int, int],
) -> list[dict[str, Any]]:
    """Save photo links before rewriting the inventory's variation IDs."""
    old_values = {
        (int(prop["property_id"]), str(prop["values"][0]).casefold())
        for item in _inventory_products(original_inventory)
        for prop in item.get("property_values", [])
    }
    result: list[dict[str, Any]] = []
    for image in original_images:
        old_id = int(image["property_id"])
        value = str(image.get("value") or "")
        if (old_id, value.casefold()) not in old_values or old_id not in old_to_new:
            raise StorefrontVerificationError("Etsy variation image has an unknown variation value")
        result.append(
            {"property_id": old_to_new[old_id], "value": value, "image_id": int(image["image_id"])}
        )
    return result


def remap_etsy_variation_images(
    plan: list[dict[str, Any]], updated_inventory: dict[str, Any]
) -> list[dict[str, int]]:
    """Resolve photo links against Etsy's newly assigned custom value IDs."""
    new_values: dict[tuple[int, str], int] = {}
    for item in _inventory_products(updated_inventory):
        for prop in item.get("property_values", []):
            ids = prop.get("value_ids") or []
            values = prop.get("values") or []
            if len(ids) == 1 and len(values) == 1:
                new_values[(int(prop["property_id"]), str(values[0]).casefold())] = int(ids[0])
    result: list[dict[str, int]] = []
    for image in plan:
        new_id = int(image["property_id"])
        value_id = new_values.get((new_id, str(image["value"]).casefold()))
        if value_id is None:
            raise StorefrontVerificationError("Etsy did not return a value ID for a variation image")
        result.append(
            {"property_id": new_id, "value_id": value_id, "image_id": int(image["image_id"])}
        )
    return result


def verify_etsy_variation_images(
    actual: list[dict[str, Any]], expected: list[dict[str, int]]
) -> None:
    keys = ("property_id", "value_id", "image_id")
    if len(actual) != len(expected) or {tuple(int(image[key]) for key in keys) for image in actual} != {
        tuple(image[key] for key in keys) for image in expected
    }:
        raise StorefrontVerificationError("Etsy variation photos differ after selector update")


class EtsyStorefrontClient:
    def __init__(
        self,
        settings: Settings,
        access_token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.access_token = access_token or settings.etsy_access_token.get_secret_value()
        self.client = client or httpx.AsyncClient(
            base_url="https://openapi.etsy.com/v3", timeout=httpx.Timeout(30, connect=10)
        )
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.etsy_api_key.get_secret_value()
            and self.settings.etsy_shared_secret.get_secret_value()
            and self.access_token
            and self.settings.etsy_shop_id
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.configured:
            raise StorefrontVerificationError(
                "Etsy API key, shared secret, access token, and shop ID are required for storefront verification"
            )
        return {
            "x-api-key": (
                f"{self.settings.etsy_api_key.get_secret_value()}:"
                f"{self.settings.etsy_shared_secret.get_secret_value()}"
            ),
            "Authorization": f"Bearer {self.access_token}",
        }

    def _raise_for_status(self, response: httpx.Response, operation: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            # Capture only structured API error messages, never the response body,
            # request payload, URL query, or authentication headers.
            messages: list[str] = []
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                for key in ("error", "error_description", "message"):
                    value = body.get(key)
                    if isinstance(value, str):
                        messages.append(value)
                errors = body.get("errors")
                if isinstance(errors, list):
                    for item in errors[:5]:
                        if isinstance(item, dict) and isinstance(item.get("message"), str):
                            messages.append(item["message"])
            detail = redact("; ".join(messages), sorted([
                self.settings.etsy_api_key.get_secret_value(),
                self.settings.etsy_shared_secret.get_secret_value(),
                self.access_token,
                self.settings.etsy_access_token.get_secret_value(),
                self.settings.etsy_refresh_token.get_secret_value(),
            ], key=len, reverse=True))
            detail = re.sub(
                r'''(?ix)(\b(?:authorization|x-api-key|access[_-]?token|refresh[_-]?token|client[_-]?secret|api[_-]?key)["']?\s*[:=]\s*)(?:(?:Bearer|Basic)\s+)?(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
                r"\1[REDACTED]", detail,
            )
            detail = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", detail)
            detail = re.sub(r"https?://\S+", "[URL REDACTED]", detail)
            detail = " ".join("".join(char if char.isprintable() else " " for char in detail).split())
            if len(detail) > 1000:
                detail = detail[:997] + "..."
            message = f"Etsy {operation} failed (HTTP {response.status_code})"
            if detail:
                message += f": {detail}"
            raise httpx.HTTPStatusError(
                message, request=response.request, response=response
            ) from None

    async def _get(self, path: str, operation: str) -> dict[str, Any]:
        response = await self.client.get(path, headers=self._headers())
        self._raise_for_status(response, operation)
        return cast(dict[str, Any], response.json())

    async def listing(self, listing_id: int) -> dict[str, Any]:
        return await self._get(f"/application/listings/{listing_id}", "read listing")

    async def shop_listings(self, state: str) -> list[dict[str, Any]]:
        if state not in {"active", "draft", "inactive", "sold_out", "expired"}:
            raise ValueError("unsupported Etsy listing state")
        found: list[dict[str, Any]] = []
        offset = 0
        while True:
            result = await self._get(
                f"/application/shops/{self.settings.etsy_shop_id}/listings"
                f"?state={state}&limit=100&offset={offset}", "list shop listings",
            )
            page = result.get("results") or []
            found.extend(page)
            if len(page) < 100:
                return found
            offset += 100

    async def create_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            f"/application/shops/{self.settings.etsy_shop_id}/listings",
            headers=self._headers(), data=payload,
        )
        self._raise_for_status(response, "create draft")
        return cast(dict[str, Any], response.json())

    async def update_listing(self, listing_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.patch(
            f"/application/shops/{self.settings.etsy_shop_id}/listings/{listing_id}",
            headers=self._headers(), data=payload,
        )
        self._raise_for_status(response, "update listing")
        return cast(dict[str, Any], response.json())

    async def inventory(self, listing_id: int) -> dict[str, Any]:
        return await self._get(f"/application/listings/{listing_id}/inventory", "read inventory")

    async def update_inventory(
        self,
        listing_id: int,
        payload: dict[str, Any],
        *,
        max_variations_supported: int = 2,
    ) -> None:
        if max_variations_supported not in {2, 3}:
            raise ValueError("Etsy max_variations_supported must be 2 or 3")
        path = f"/application/listings/{listing_id}/inventory"
        if max_variations_supported == 3:
            path += "?max_variations_supported=3"
        response = await self.client.put(
            path,
            headers=self._headers(),
            json=payload,
        )
        self._raise_for_status(response, "update inventory")

    async def seller_taxonomy_nodes(self) -> list[dict[str, Any]]:
        result = await self._get(
            "/application/seller-taxonomy/nodes", "read seller taxonomy"
        )
        return cast(list[dict[str, Any]], result.get("results", []))

    async def taxonomy_properties(self, taxonomy_id: int) -> list[dict[str, Any]]:
        result = await self._get(
            f"/application/seller-taxonomy/nodes/{taxonomy_id}/properties",
            "read taxonomy properties",
        )
        return cast(list[dict[str, Any]], result.get("results", []))

    async def shipping_profile(self, shipping_profile_id: int) -> dict[str, Any]:
        return await self._get(
            f"/application/shops/{self.settings.etsy_shop_id}/shipping-profiles/"
            f"{shipping_profile_id}",
            "read shipping profile",
        )

    async def return_policy(self, return_policy_id: int) -> dict[str, Any]:
        return await self._get(
            f"/application/shops/{self.settings.etsy_shop_id}/policies/return/"
            f"{return_policy_id}",
            "read return policy",
        )

    async def readiness_states(self) -> list[dict[str, Any]]:
        result = await self._get(
            f"/application/shops/{self.settings.etsy_shop_id}/readiness-state-definitions",
            "read readiness states",
        )
        return cast(list[dict[str, Any]], result.get("results", []))

    async def create_readiness_state(
        self,
        *,
        minimum_days: int = 2,
        maximum_days: int = 5,
    ) -> dict[str, Any]:
        response = await self.client.post(
            f"/application/shops/{self.settings.etsy_shop_id}/readiness-state-definitions",
            headers=self._headers(),
            data={
                "readiness_state": "made_to_order",
                "min_processing_time": minimum_days,
                "max_processing_time": maximum_days,
                "processing_time_unit": "days",
            },
        )
        self._raise_for_status(response, "create readiness state")
        return cast(dict[str, Any], response.json())

    async def variation_images(self, listing_id: int) -> list[dict[str, Any]]:
        result = await self._get(
            f"/application/shops/{self.settings.etsy_shop_id}/listings/{listing_id}/variation-images",
            "read variation images",
        )
        return cast(list[dict[str, Any]], result.get("results", []))

    async def listing_transactions(self, listing_id: int) -> list[dict[str, Any]]:
        """Read a bounded first page; one row is enough to block artwork replacement."""
        result = await self._get(
            f"/application/shops/{self.settings.etsy_shop_id}/listings/"
            f"{listing_id}/transactions?limit=1&offset=0",
            "read listing transactions",
        )
        rows = result.get("results")
        count = result.get("count")
        if not isinstance(rows, list) or not isinstance(count, int) or count < len(rows):
            raise StorefrontVerificationError(
                "Etsy listing transactions response is incomplete"
            )
        if count and not rows:
            raise StorefrontVerificationError(
                "Etsy listing transactions response omitted a matching order"
            )
        return cast(list[dict[str, Any]], rows)

    async def update_variation_images(
        self, listing_id: int, images: list[dict[str, int]]
    ) -> None:
        response = await self.client.post(
            f"/application/shops/{self.settings.etsy_shop_id}/listings/{listing_id}/variation-images",
            headers=self._headers(),
            json={"variation_images": images},
        )
        self._raise_for_status(response, "update variation images")

    async def images(self, listing_id: int) -> list[dict[str, Any]]:
        result = await self._get(f"/application/listings/{listing_id}/images", "read listing images")
        return cast(list[dict[str, Any]], result.get("results", []))

    async def delete_image(self, listing_id: int, image_id: int) -> None:
        """Delete one image from an existing listing without changing the listing itself."""
        response = await self.client.delete(
            f"/application/shops/{self.settings.etsy_shop_id}/listings/"
            f"{listing_id}/images/{image_id}",
            headers=self._headers(),
        )
        self._raise_for_status(response, "delete listing image")

    async def upload_featured(
        self, listing_id: int, image: bytes, content_type: str, alt_text: str
    ) -> int:
        extension = "png" if content_type == "image/png" else "jpg"
        response = await self.client.post(
            f"/application/shops/{self.settings.etsy_shop_id}/listings/{listing_id}/images",
            headers=self._headers(),
            data={"rank": "1", "overwrite": "false", "alt_text": alt_text[:500]},
            files={"image": (f"featured-mockup.{extension}", image, content_type)},
        )
        self._raise_for_status(response, "upload featured image")
        return int(response.json()["listing_image_id"])

    async def upload_mockup(
        self, listing_id: int, image: bytes, content_type: str, alt_text: str, rank: int
    ) -> int:
        extension = "png" if content_type == "image/png" else "jpg"
        response = await self.client.post(
            f"/application/shops/{self.settings.etsy_shop_id}/listings/{listing_id}/images",
            headers=self._headers(),
            data={"rank": str(rank), "overwrite": "false", "alt_text": alt_text[:500]},
            files={"image": (f"mockup-{rank}.{extension}", image, content_type)},
        )
        self._raise_for_status(response, "upload mockup")
        return int(response.json()["listing_image_id"])


async def download_mockup(url: str, client: httpx.AsyncClient | None = None) -> tuple[bytes, str]:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        hostname.endswith(".printify.com")
        or hostname == "printify.com"
        or hostname.endswith(".printify.me")
        or hostname == "printify.me"
    ):
        raise StorefrontVerificationError("Featured mockup URL is outside Printify")
    owned = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
    try:
        response = await http.get(url)
        response.raise_for_status()
        image = response.content
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type not in {"image/jpeg", "image/png"} or not 0 < len(image) <= 15_000_000:
            raise StorefrontVerificationError("Printify featured mockup is not a supported image")
        return image, content_type
    finally:
        if owned:
            await http.aclose()


def verify_etsy_listing(
    listing: dict[str, Any],
    listing_id: int,
    shop_id: int,
    expected_title: str,
) -> None:
    if (
        int(listing.get("listing_id") or 0) != listing_id
        or int(listing.get("shop_id") or 0) != shop_id
        or listing.get("state") != "active"
        or listing.get("title") != expected_title
    ):
        raise StorefrontVerificationError("Etsy listing identity, title, shop, or active state differs")


def verify_featured_image(images: list[dict[str, Any]], image_id: int) -> None:
    if not any(
        int(item.get("listing_image_id") or 0) == image_id and int(item.get("rank") or 0) == 1
        for item in images
    ):
        raise StorefrontVerificationError("Etsy has not placed the approved mockup first")
