"""Printify catalog synchronization with fail-closed capability discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from merch.config import Settings, get_settings
from merch.database import session_scope
from merch.domain.catalog import fixture_catalog, normalize_catalog_product
from merch.repository import CatalogRepository
from merch.schemas import CatalogProduct
from merch.services.printify import PrintifyClient


async def fetch_catalog(settings: Settings | None = None) -> list[CatalogProduct]:
    settings = settings or get_settings()
    synced_at = datetime.now(UTC)
    if settings.provider_mode == "fake":
        return fixture_catalog(synced_at)
    client = PrintifyClient(settings)
    products: list[CatalogProduct] = []
    try:
        for blueprint_summary in await client.blueprints():
            blueprint_id = blueprint_summary.get("id")
            if type(blueprint_id) is not int:
                continue
            blueprint = await client.blueprint(blueprint_id)
            for provider in await client.print_providers(blueprint_id):
                provider_id = provider.get("id")
                if type(provider_id) is not int:
                    continue
                variants = await client.variants(blueprint_id, provider_id)
                shipping = await client.shipping(blueprint_id, provider_id)
                try:
                    products.append(
                        normalize_catalog_product(
                            blueprint, provider, variants, shipping, synced_at=synced_at
                        )
                    )
                except ValueError:
                    # Unsupported/empty products remain discoverable upstream but are not
                    # eligible until an explicit production capability is implemented.
                    continue
    finally:
        await client.close()
    return products


async def sync_catalog(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    products = await fetch_catalog(settings)
    if not products:
        raise RuntimeError("Printify catalog sync returned no supported products")
    with session_scope() as session:
        repository = CatalogRepository(session)
        for product in products:
            repository.upsert_product(product)
        removed = repository.prune_products(
            {
                repository.product_key(product.blueprint_id, product.print_provider_id)
                for product in products
            }
        )
    return {
        "products": len(products),
        "removed_products": removed,
        "variants": sum(len(item.variants) for item in products),
        "decoration_methods": sorted(
            {
                surface.decoration_method
                for product in products
                for variant in product.variants
                for surface in variant.surfaces
            }
        ),
        "synced_at": max((item.synced_at for item in products), default=datetime.now(UTC)),
    }
