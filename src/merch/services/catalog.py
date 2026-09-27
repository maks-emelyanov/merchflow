"""Incremental and resumable Printify catalog synchronization."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from merch.config import Settings, get_settings
from merch.database import session_scope
from merch.domain.catalog import fixture_catalog, normalize_catalog_product
from merch.repository import CatalogRefreshRepository, CatalogRepository
from merch.schemas import CatalogProduct
from merch.services.printify import PrintifyClient, PrintifyHTTPError

CATALOG_REFRESH_BATCH_SIZE = 50
CATALOG_PROVIDER_DISCOVERY_ATTEMPTS = 2
_UNAVAILABLE_STATUSES = {404, 410}


class CatalogProductUnavailable(RuntimeError):
    pass


def _blueprint_summary(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in ("id", "title", "description", "brand", "model")
        if value.get(key) is not None
    }


def _ranked_blueprint_map(values: list[dict[str, Any]]) -> dict[int, tuple[int, dict[str, Any]]]:
    ranked: dict[int, tuple[int, dict[str, Any]]] = {}
    for position, value in enumerate(values, start=1):
        blueprint_id = value.get("blueprintId")
        if type(blueprint_id) is int and blueprint_id not in ranked:
            ranked[blueprint_id] = (position, value)
    if not ranked:
        raise RuntimeError("Printify Bestsellers ranking returned no blueprints")
    return ranked


def _provider_summary(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in ("id", "title") if value.get(key) is not None}


def _warning(
    stage: str,
    error: Exception | str,
    *,
    blueprint_id: int | None = None,
    print_provider_id: int | None = None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "blueprint_id": blueprint_id,
        "print_provider_id": print_provider_id,
        "error": str(error)[:1000],
    }


def _unavailable_error(exc: BaseException) -> bool:
    return isinstance(exc, PrintifyHTTPError) and exc.status_code in _UNAVAILABLE_STATUSES


def _viability_error(product: CatalogProduct) -> str | None:
    available = [variant for variant in product.variants if variant.available]
    if not available:
        return "no available Printify variants"
    supported = [
        variant
        for variant in available
        if all(surface.placement != "unsupported" for surface in variant.surfaces)
    ]
    if not supported:
        return "no available variants use a supported Printify decoration method"
    if not any(variant.shipping_cost_cents is not None for variant in supported):
        return "no supported variant has US shipping pricing"
    return None


def claim_catalog_refresh(workflow_id: str) -> dict[str, Any]:
    with session_scope() as session:
        record, resumed, owner = CatalogRefreshRepository(session).claim(workflow_id)
        payload = CatalogRefreshRepository.payload(record)
        payload.update({"resumed": resumed, "owner": owner})
        return payload


def get_catalog_refresh(refresh_id: str) -> dict[str, Any]:
    with session_scope() as session:
        record = CatalogRefreshRepository(session).get(refresh_id)
        return CatalogRefreshRepository.payload(record)


def list_catalog_refreshes(limit: int = 20) -> list[dict[str, Any]]:
    with session_scope() as session:
        repository = CatalogRefreshRepository(session)
        return [repository.payload(item) for item in repository.list_refreshes(limit)]


def unresolved_catalog_provider_ids(refresh_id: str | None = None) -> set[int]:
    with session_scope() as session:
        repository = CatalogRefreshRepository(session)
        if refresh_id is not None:
            records = [repository.get(refresh_id)]
        else:
            records = repository.list_refreshes(limit=1)
        if not records:
            raise ValueError("no catalog refresh has been started")
        record = records[0]
        if record.manifest is not None:
            return {int(value) for value in record.manifest.get("preserved_provider_ids", [])}
        return {
            int(item["print_provider_id"])
            for item in record.warning_samples or []
            if item.get("stage") == "provider_discovery"
            and type(item.get("print_provider_id")) is int
        }


async def discover_catalog_refresh(
    refresh_id: str, settings: Settings | None = None
) -> dict[str, Any]:
    settings = settings or get_settings()
    with session_scope() as session:
        repository = CatalogRefreshRepository(session)
        record = repository.get(refresh_id)
        if record.manifest is not None:
            record.status = "running"
            record.error = None
            record.updated_at = datetime.now(UTC)
            return repository.payload(record)

    warnings: list[dict[str, Any]] = []
    if settings.provider_mode == "fake":
        products = fixture_catalog()
        manifest = {
            "version": 2,
            "fake": True,
            "blueprints": {
                str(item.blueprint_id): {
                    "id": item.blueprint_id,
                    "title": item.title,
                    "description": item.description,
                    "brand": item.brand,
                    "model": item.model,
                    "tags": item.tags,
                }
                for item in products
            },
            "providers": {
                str(item.print_provider_id): {"id": item.print_provider_id} for item in products
            },
            "pairs": sorted([[item.blueprint_id, item.print_provider_id] for item in products]),
            "ranked_blueprints": {
                str(item.blueprint_id): rank for rank, item in enumerate(products, start=1)
            },
        }
    else:
        client = PrintifyClient(settings)
        try:
            blueprint_values, ranked_values = await asyncio.gather(
                client.blueprints(),
                client.ranked_blueprints(),
            )
            ranked = _ranked_blueprint_map(ranked_values)
            ranked_blueprint_ids = set(ranked)
            blueprints = {
                str(item["id"]): _blueprint_summary(item)
                for item in blueprint_values
                if type(item.get("id")) is int and item["id"] in ranked_blueprint_ids
            }
            ranked_blueprint_ids.intersection_update(int(value) for value in blueprints)
            ranked = {
                blueprint_id: value
                for blueprint_id, value in ranked.items()
                if blueprint_id in ranked_blueprint_ids
            }
            providers: dict[str, dict[str, Any]] = {}
            pairs: set[tuple[int, int]] = set()
            preserved_provider_ids: set[int] = set()
            preserved_blueprint_ids: set[int] = set()
            try:
                provider_values = await client.catalog_print_providers()
            except PrintifyHTTPError as exc:
                if exc.status_code != 403:
                    raise
                warnings.append(_warning("provider_index_fallback", exc))

                async def blueprint_providers(
                    blueprint_id: int,
                ) -> list[dict[str, Any]] | Exception:
                    try:
                        return await client.print_providers(blueprint_id)
                    except Exception as detail_exc:
                        return detail_exc

                blueprint_ids = sorted(int(key) for key in blueprints)
                provider_lists = await asyncio.gather(
                    *(blueprint_providers(blueprint_id) for blueprint_id in blueprint_ids)
                )
                for blueprint_id, offerings in zip(blueprint_ids, provider_lists, strict=True):
                    if isinstance(offerings, Exception):
                        transient_forbidden = (
                            isinstance(offerings, PrintifyHTTPError)
                            and offerings.status_code == 403
                        )
                        if not (_unavailable_error(offerings) or transient_forbidden):
                            raise offerings from None
                        if transient_forbidden:
                            preserved_blueprint_ids.add(blueprint_id)
                        warnings.append(
                            _warning(
                                "blueprint_provider_discovery",
                                offerings,
                                blueprint_id=blueprint_id,
                            )
                        )
                        continue
                    for provider in offerings:
                        provider_id = provider.get("id")
                        if type(provider_id) is not int:
                            continue
                        providers[str(provider_id)] = _provider_summary(provider)
                        pairs.add((blueprint_id, provider_id))
            else:
                providers = {
                    str(item["id"]): _provider_summary(item)
                    for item in provider_values
                    if type(item.get("id")) is int
                }

                async def provider_offerings(
                    provider_id: int,
                ) -> dict[str, Any] | Exception:
                    try:
                        return await client.catalog_print_provider(provider_id)
                    except Exception as detail_exc:
                        return detail_exc

                provider_ids = sorted(int(key) for key in providers)
                provider_details: dict[int, dict[str, Any]] = {}
                provider_errors: dict[int, Exception] = {}
                pending_provider_ids = set(provider_ids)
                for _ in range(CATALOG_PROVIDER_DISCOVERY_ATTEMPTS):
                    attempted_provider_ids = sorted(pending_provider_ids)
                    if not attempted_provider_ids:
                        break
                    details = await asyncio.gather(
                        *(provider_offerings(provider_id) for provider_id in attempted_provider_ids)
                    )
                    pending_provider_ids = set()
                    for provider_id, detail in zip(attempted_provider_ids, details, strict=True):
                        if not isinstance(detail, Exception):
                            provider_details[provider_id] = detail
                            provider_errors.pop(provider_id, None)
                            continue
                        provider_errors[provider_id] = detail
                        if isinstance(detail, PrintifyHTTPError) and detail.status_code == 403:
                            pending_provider_ids.add(provider_id)
                        elif not _unavailable_error(detail):
                            raise detail

                for provider_id in provider_ids:
                    provider_detail = provider_details.get(provider_id)
                    if provider_detail is None:
                        continue
                    providers[str(provider_id)] = _provider_summary(provider_detail)
                    for offering in provider_detail.get("blueprints", []):
                        blueprint_id = offering.get("id")
                        if (
                            type(blueprint_id) is not int
                            or blueprint_id not in ranked_blueprint_ids
                        ):
                            continue
                        pairs.add((blueprint_id, provider_id))

                recovered_provider_ids: set[int] = set()
                if pending_provider_ids:

                    async def blueprint_providers_for_pending(
                        blueprint_id: int,
                    ) -> list[dict[str, Any]] | Exception:
                        try:
                            return await client.print_providers(blueprint_id)
                        except Exception as detail_exc:
                            return detail_exc

                    blueprint_ids = sorted(int(key) for key in blueprints)
                    provider_lists = await asyncio.gather(
                        *(
                            blueprint_providers_for_pending(blueprint_id)
                            for blueprint_id in blueprint_ids
                        )
                    )
                    for blueprint_id, offerings in zip(blueprint_ids, provider_lists, strict=True):
                        if isinstance(offerings, Exception):
                            transient_forbidden = (
                                isinstance(offerings, PrintifyHTTPError)
                                and offerings.status_code == 403
                            )
                            if not (_unavailable_error(offerings) or transient_forbidden):
                                raise offerings from None
                            if transient_forbidden:
                                preserved_blueprint_ids.add(blueprint_id)
                            warnings.append(
                                _warning(
                                    "blueprint_provider_discovery",
                                    offerings,
                                    blueprint_id=blueprint_id,
                                )
                            )
                            continue
                        for provider in offerings:
                            provider_id = provider.get("id")
                            if (
                                type(provider_id) is not int
                                or provider_id not in pending_provider_ids
                            ):
                                continue
                            recovered_provider_ids.add(provider_id)
                            providers[str(provider_id)] = _provider_summary(provider)
                            pairs.add((blueprint_id, provider_id))

                unresolved_provider_ids = set(provider_errors) - recovered_provider_ids
                preserved_provider_ids = pending_provider_ids - recovered_provider_ids
                for provider_id in sorted(unresolved_provider_ids):
                    warnings.append(
                        _warning(
                            "provider_discovery",
                            provider_errors[provider_id],
                            print_provider_id=provider_id,
                        )
                    )

                if not pairs and pending_provider_ids:
                    raise provider_errors[min(pending_provider_ids)]
            manifest = {
                "version": 2,
                "fake": False,
                "blueprints": blueprints,
                "providers": providers,
                "pairs": [list(item) for item in sorted(pairs)],
                "ranked_blueprints": {
                    str(blueprint_id): rank
                    for blueprint_id, (rank, _) in ranked.items()
                },
                "preserved_provider_ids": sorted(preserved_provider_ids),
                "preserved_blueprint_ids": sorted(preserved_blueprint_ids),
            }
        finally:
            await client.close()

    if not manifest["pairs"]:
        raise RuntimeError("Printify catalog discovery returned no blueprint/provider offerings")
    with session_scope() as session:
        record = CatalogRefreshRepository(session).store_manifest(
            refresh_id, manifest, warnings=warnings
        )
        return CatalogRefreshRepository.payload(record)


async def refresh_catalog_batch(
    refresh_id: str,
    settings: Settings | None = None,
    *,
    batch_size: int = CATALOG_REFRESH_BATCH_SIZE,
) -> dict[str, Any]:
    settings = settings or get_settings()
    with session_scope() as session:
        refresh = CatalogRefreshRepository(session).get(refresh_id)
        manifest = refresh.manifest
        if manifest is None:
            raise RuntimeError("catalog refresh has no discovery manifest")
        start = refresh.cursor
        pairs = manifest.get("pairs", [])
        batch = pairs[start : start + batch_size]
        tag_cache = CatalogRepository(session).tags_by_blueprint({int(item[0]) for item in batch})
    if not batch:
        return get_catalog_refresh(refresh_id)

    synced_at = datetime.now(UTC)
    products: list[CatalogProduct] = []
    unavailable: list[tuple[int, int, str]] = []
    warnings: list[dict[str, Any]] = []
    if manifest.get("fake"):
        fixtures = {
            (item.blueprint_id, item.print_provider_id): item for item in fixture_catalog(synced_at)
        }
        for raw_blueprint_id, raw_provider_id in batch:
            pair = (int(raw_blueprint_id), int(raw_provider_id))
            if pair in fixtures:
                products.append(fixtures[pair])
    else:
        client = PrintifyClient(settings)

        async def fetch_pair(
            blueprint_id: int, provider_id: int
        ) -> CatalogProduct | tuple[int, int, str]:
            try:
                variants, shipping = await asyncio.gather(
                    client.variants(blueprint_id, provider_id),
                    client.shipping(blueprint_id, provider_id),
                )
                blueprint = dict(manifest["blueprints"][str(blueprint_id)])
                blueprint["tags"] = tag_cache.get(blueprint_id, [])
                provider = manifest["providers"].get(str(provider_id), {"id": provider_id})
                return normalize_catalog_product(
                    blueprint, provider, variants, shipping, synced_at=synced_at
                )
            except Exception as exc:
                if _unavailable_error(exc) or isinstance(exc, ValueError):
                    return blueprint_id, provider_id, str(exc)
                raise

        try:
            results = await asyncio.gather(
                *(
                    fetch_pair(int(blueprint_id), int(provider_id))
                    for blueprint_id, provider_id in batch
                )
            )
        finally:
            await client.close()
        for result in results:
            if isinstance(result, CatalogProduct):
                products.append(result)
            else:
                blueprint_id, provider_id, error = result
                unavailable.append(result)
                warnings.append(
                    _warning(
                        "offering_refresh",
                        error,
                        blueprint_id=blueprint_id,
                        print_provider_id=provider_id,
                    )
                )

    with session_scope() as session:
        catalog = CatalogRepository(session)
        ranks = {
            int(blueprint_id): int(rank)
            for blueprint_id, rank in manifest.get("ranked_blueprints", {}).items()
        }
        for product in products:
            catalog.upsert_product(
                product,
                refresh_id=refresh_id,
                printify_rank=ranks[product.blueprint_id],
            )
        for blueprint_id, provider_id, error in unavailable:
            catalog.mark_unavailable(blueprint_id, provider_id, error)
        record = CatalogRefreshRepository(session).record_batch(
            refresh_id,
            cursor=start + len(batch),
            refreshed=len(products),
            skipped=len(unavailable),
            warnings=warnings,
        )
        return CatalogRefreshRepository.payload(record)


def finalize_catalog_refresh(refresh_id: str) -> dict[str, Any]:
    with session_scope() as session:
        refreshes = CatalogRefreshRepository(session)
        refresh = refreshes.get(refresh_id)
        if refresh.status in {"completed", "completed_with_warnings"}:
            result = refreshes.payload(refresh)
        else:
            if refresh.manifest is None or refresh.cursor < refresh.total_pairs:
                raise RuntimeError("catalog refresh cannot finish before every discovered pair")
            preserved_provider_ids = {
                int(value) for value in refresh.manifest.get("preserved_provider_ids", [])
            }
            preserved_blueprint_ids = {
                int(value) for value in refresh.manifest.get("preserved_blueprint_ids", [])
            }
            allowed_blueprint_ids = {
                int(value) for value in refresh.manifest.get("ranked_blueprints", {})
            }
            ranks = {
                int(blueprint_id): int(rank)
                for blueprint_id, rank in refresh.manifest.get("ranked_blueprints", {}).items()
            }
            catalog = CatalogRepository(session)
            catalog.apply_blueprint_ranks(ranks)
            retired = catalog.retire_not_seen(
                refresh_id,
                preserve_provider_ids=preserved_provider_ids,
                preserve_blueprint_ids=preserved_blueprint_ids,
                allowed_blueprint_ids=allowed_blueprint_ids,
            )
            result = refreshes.payload(refreshes.complete(refresh_id, retired))
    from merch.catalog_curation import curate_synced_catalog

    result["curation"] = curate_synced_catalog(apply=True)
    return result


def fail_catalog_refresh(refresh_id: str, error: str) -> dict[str, Any]:
    with session_scope() as session:
        repository = CatalogRefreshRepository(session)
        return repository.payload(repository.fail(refresh_id, error))


async def refresh_catalog_product(
    blueprint_id: int,
    print_provider_id: int,
    settings: Settings | None = None,
) -> CatalogProduct:
    settings = settings or get_settings()
    if settings.provider_mode == "fake":
        product = next(
            (
                item
                for item in fixture_catalog()
                if item.blueprint_id == blueprint_id and item.print_provider_id == print_provider_id
            ),
            None,
        )
        if product is None:
            raise CatalogProductUnavailable(
                f"Printify catalog product {blueprint_id}/{print_provider_id} is unavailable"
            )
    else:
        client = PrintifyClient(settings)
        try:
            blueprint, variants, shipping = await asyncio.gather(
                client.blueprint(blueprint_id),
                client.variants(blueprint_id, print_provider_id),
                client.shipping(blueprint_id, print_provider_id),
            )
            product = normalize_catalog_product(
                blueprint,
                {"id": print_provider_id},
                variants,
                shipping,
                synced_at=datetime.now(UTC),
            )
            viability_error = _viability_error(product)
            if viability_error is not None:
                raise ValueError(viability_error)
        except Exception as exc:
            if not (_unavailable_error(exc) or isinstance(exc, ValueError)):
                raise
            with session_scope() as session:
                CatalogRepository(session).mark_unavailable(
                    blueprint_id, print_provider_id, str(exc)
                )
            raise CatalogProductUnavailable(str(exc)) from exc
        finally:
            await client.close()
    with session_scope() as session:
        CatalogRepository(session).upsert_product(product)
    return product


async def recover_catalog_providers(
    provider_ids: set[int], settings: Settings | None = None
) -> dict[str, Any]:
    """Add missing products from unresolved providers without refreshing active pairs."""
    settings = settings or get_settings()
    requested_provider_ids = {int(value) for value in provider_ids}
    if not requested_provider_ids:
        return {
            "requested_provider_ids": [],
            "resolved_provider_ids": [],
            "unresolved_provider_ids": [],
            "discovered_pairs": 0,
            "already_active_pairs": 0,
            "missing_pairs": 0,
            "recovered_products": 0,
            "skipped_products": 0,
            "warnings": [],
        }
    if settings.provider_mode == "fake":
        raise ValueError("targeted provider recovery requires live provider mode")

    client = PrintifyClient(settings)
    provider_details: dict[int, dict[str, Any]] = {}
    provider_errors: dict[int, Exception] = {}
    try:
        ranked = _ranked_blueprint_map(await client.ranked_blueprints())
        ranked_blueprint_ids = set(ranked)

        async def provider_detail(provider_id: int) -> dict[str, Any] | Exception:
            try:
                return await client.catalog_print_provider(provider_id)
            except Exception as exc:
                return exc

        pending_provider_ids = set(requested_provider_ids)
        for _ in range(CATALOG_PROVIDER_DISCOVERY_ATTEMPTS):
            attempted_provider_ids = sorted(pending_provider_ids)
            if not attempted_provider_ids:
                break
            details = await asyncio.gather(
                *(provider_detail(provider_id) for provider_id in attempted_provider_ids)
            )
            pending_provider_ids = set()
            for provider_id, detail in zip(attempted_provider_ids, details, strict=True):
                if not isinstance(detail, Exception):
                    provider_details[provider_id] = detail
                    provider_errors.pop(provider_id, None)
                    continue
                provider_errors[provider_id] = detail
                if isinstance(detail, PrintifyHTTPError) and detail.status_code == 403:
                    pending_provider_ids.add(provider_id)

        offerings: dict[tuple[int, int], dict[str, Any]] = {}
        for provider_id, detail in provider_details.items():
            for offering in detail.get("blueprints", []):
                blueprint_id = offering.get("id")
                if type(blueprint_id) is int and blueprint_id in ranked_blueprint_ids:
                    offerings[(blueprint_id, provider_id)] = offering

        with session_scope() as session:
            catalog = CatalogRepository(session)
            catalog.apply_blueprint_ranks(
                {blueprint_id: rank for blueprint_id, (rank, _) in ranked.items()}
            )
            active_keys = catalog.active_keys()
            tags = catalog.tags_by_blueprint({blueprint_id for blueprint_id, _ in offerings})
        missing_pairs = [
            pair
            for pair in sorted(offerings)
            if CatalogRepository.product_key(*pair) not in active_keys
        ]
        already_active_pairs = len(offerings) - len(missing_pairs)

        blueprints: dict[int, dict[str, Any]] = {}
        if missing_pairs:
            blueprints = {
                int(item["id"]): _blueprint_summary(item)
                for item in await client.blueprints()
                if type(item.get("id")) is int
            }

        synced_at = datetime.now(UTC)

        async def fetch_missing_pair(
            pair: tuple[int, int],
        ) -> CatalogProduct | tuple[int, int, str]:
            blueprint_id, provider_id = pair
            try:
                variants, shipping = await asyncio.gather(
                    client.variants(blueprint_id, provider_id),
                    client.shipping(blueprint_id, provider_id),
                )
                blueprint = dict(
                    blueprints.get(blueprint_id, offerings[(blueprint_id, provider_id)])
                )
                blueprint["tags"] = tags.get(blueprint_id, [])
                return normalize_catalog_product(
                    blueprint,
                    _provider_summary(provider_details[provider_id]),
                    variants,
                    shipping,
                    synced_at=synced_at,
                )
            except Exception as exc:
                return blueprint_id, provider_id, str(exc)

        results = await asyncio.gather(*(fetch_missing_pair(pair) for pair in missing_pairs))
    finally:
        await client.close()

    products = [item for item in results if isinstance(item, CatalogProduct)]
    pair_errors = [item for item in results if not isinstance(item, CatalogProduct)]
    with session_scope() as session:
        catalog = CatalogRepository(session)
        for product in products:
            catalog.upsert_product(
                product,
                printify_rank=ranked[product.blueprint_id][0],
            )

    unresolved_provider_ids = requested_provider_ids - set(provider_details)
    warnings = [
        _warning(
            "provider_recovery",
            provider_errors[provider_id],
            print_provider_id=provider_id,
        )
        for provider_id in sorted(unresolved_provider_ids)
    ]
    warnings.extend(
        _warning(
            "offering_recovery",
            error,
            blueprint_id=blueprint_id,
            print_provider_id=provider_id,
        )
        for blueprint_id, provider_id, error in pair_errors
    )
    return {
        "requested_provider_ids": sorted(requested_provider_ids),
        "resolved_provider_ids": sorted(provider_details),
        "unresolved_provider_ids": sorted(unresolved_provider_ids),
        "discovered_pairs": len(offerings),
        "already_active_pairs": already_active_pairs,
        "missing_pairs": len(missing_pairs),
        "recovered_products": len(products),
        "skipped_products": len(pair_errors),
        "warnings": warnings[:100],
    }


async def refresh_catalog_candidates(
    limit: int, settings: Settings | None = None
) -> tuple[list[CatalogProduct], dict[str, Any]]:
    settings = settings or get_settings()
    with session_scope() as session:
        repository = CatalogRepository(session)
        has_curations = repository.has_curations()
        cached = repository.list_research_products()
    if not cached:
        if not has_curations:
            raise RuntimeError("Cached Printify catalog is empty; run `merch catalog-sync` once")
        raise RuntimeError(
            "Popular curated Printify catalog is empty; run `merch catalog-curate --apply`"
        )
    refreshed: list[CatalogProduct] = []
    skipped = 0
    if settings.provider_mode == "fake":
        fixtures = {(item.blueprint_id, item.print_provider_id): item for item in fixture_catalog()}
        for cached_product in cached:
            if len(refreshed) >= limit:
                break
            key = (
                cached_product.blueprint_id,
                cached_product.print_provider_id,
            )
            product = fixtures.get(key)
            error = _viability_error(product) if product is not None else "fixture is unavailable"
            if product is None or error is not None:
                with session_scope() as session:
                    CatalogRepository(session).mark_unavailable(*key, error or "unavailable")
                skipped += 1
                continue
            with session_scope() as session:
                CatalogRepository(session).upsert_product(product)
            refreshed.append(product)
        if not refreshed:
            raise RuntimeError("No Printify catalog products were available for research")
        return refreshed, _candidate_refresh_stats(refreshed, skipped)

    client = PrintifyClient(settings)
    details: dict[int, dict[str, Any] | Exception] = {}
    try:
        for cached_product in cached:
            if len(refreshed) >= limit:
                break
            blueprint_id = cached_product.blueprint_id
            provider_id = cached_product.print_provider_id
            if blueprint_id not in details:
                try:
                    details[blueprint_id] = await client.blueprint(blueprint_id)
                except Exception as exc:
                    details[blueprint_id] = exc
            detail = details[blueprint_id]
            if isinstance(detail, Exception):
                if not _unavailable_error(detail):
                    raise detail
                with session_scope() as session:
                    CatalogRepository(session).mark_unavailable(
                        blueprint_id, provider_id, str(detail)
                    )
                skipped += 1
                continue
            try:
                variants, shipping = await asyncio.gather(
                    client.variants(blueprint_id, provider_id),
                    client.shipping(blueprint_id, provider_id),
                )
                product = normalize_catalog_product(
                    detail,
                    {"id": provider_id},
                    variants,
                    shipping,
                    synced_at=datetime.now(UTC),
                )
                viability_error = _viability_error(product)
                if viability_error is not None:
                    raise ValueError(viability_error)
            except Exception as exc:
                if not (_unavailable_error(exc) or isinstance(exc, ValueError)):
                    raise
                with session_scope() as session:
                    CatalogRepository(session).mark_unavailable(blueprint_id, provider_id, str(exc))
                skipped += 1
                continue
            with session_scope() as session:
                CatalogRepository(session).upsert_product(product)
            refreshed.append(product)
    finally:
        await client.close()
    if not refreshed:
        raise RuntimeError("No Printify catalog products were available for research")
    return refreshed, _candidate_refresh_stats(refreshed, skipped)


def _candidate_refresh_stats(refreshed: list[CatalogProduct], skipped: int) -> dict[str, Any]:
    with session_scope() as session:
        active = CatalogRepository(session).list_research_products()
    now = datetime.now(UTC)
    ages = [max(0.0, (now - item.synced_at).total_seconds() / 3600) for item in active]
    return {
        "active_products": len(active),
        "refreshed_products": len(refreshed),
        "skipped_products": skipped,
        "oldest_catalog_age_hours": round(max(ages, default=0.0), 2),
    }
