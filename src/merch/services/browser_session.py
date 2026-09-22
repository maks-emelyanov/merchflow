"""Encrypted Playwright sessions and account-specific Printify cost collection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from merch.config import Settings, get_settings
from merch.database import session_scope
from merch.repository import BrowserSessionRepository, CatalogRepository
from merch.schemas import CatalogProduct
from merch.services.credentials import CredentialCipher

PRINTIFY_SESSION = "printify"
_PRICE = re.compile(r"(?:US\s*)?\$\s*(\d[\d,]*)(?:\.(\d{2}))?")


def _money_cents(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if not isinstance(value, str):
        return None
    match = _PRICE.search(value)
    if match is None:
        return None
    return int(match.group(1).replace(",", "")) * 100 + int(match.group(2) or 0)


def extract_variant_costs(
    variant_ids: set[int],
    *,
    rows: list[dict[str, Any]],
    embedded_payloads: list[Any],
) -> dict[int, int]:
    """Extract only expected variant costs from explicit rows or embedded page state."""
    result: dict[int, int] = {}
    for row in rows:
        raw_variant_id = row.get("variant_id")
        try:
            variant_id = int(raw_variant_id) if raw_variant_id is not None else -1
        except TypeError, ValueError:
            continue
        cost = _money_cents(row.get("cost"))
        if variant_id in variant_ids and cost is not None:
            result[variant_id] = cost

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            raw_id = value.get("variant_id", value.get("variantId", value.get("id")))
            try:
                variant_id = int(raw_id) if raw_id is not None else -1
            except TypeError, ValueError:
                variant_id = -1
            if variant_id in variant_ids:
                for key in ("cost", "base_cost", "baseCost", "price"):
                    cost = _money_cents(value.get(key))
                    if cost is not None:
                        result.setdefault(variant_id, cost)
                        break
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for payload in embedded_payloads:
        walk(payload)
    return result


def embedded_product_identities(payloads: list[Any]) -> set[tuple[int, int]]:
    """Read explicit blueprint/provider pairs without trusting the requested URL."""
    identities: set[tuple[int, int]] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            raw_blueprint = value.get("blueprint_id", value.get("blueprintId"))
            raw_provider = value.get(
                "print_provider_id", value.get("printProviderId", value.get("provider_id"))
            )
            try:
                if raw_blueprint is not None and raw_provider is not None:
                    identities.add((int(raw_blueprint), int(raw_provider)))
            except TypeError, ValueError:
                pass
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for payload in payloads:
        walk(payload)
    return identities


def _cipher(settings: Settings) -> CredentialCipher:
    return CredentialCipher(settings.credential_encryption_key.get_secret_value())


async def connect_printify_browser(settings: Settings | None = None) -> dict[str, Any]:
    """Open a headed browser and persist storage state after an operator logs in."""
    settings = settings or get_settings()
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(
            f"{settings.printify_dashboard_base_url.rstrip('/')}/app",
            wait_until="domcontentloaded",
            timeout=settings.browser_navigation_timeout_seconds * 1000,
        )
        await asyncio.to_thread(
            input,
            "Log in to Printify in the opened browser, then press Enter here to save the session: ",
        )
        state = await context.storage_state()
        await browser.close()
    serialized = json.dumps(state, separators=(",", ":"))
    encrypted = _cipher(settings).encrypt(serialized)
    expiries = [
        float(cookie["expires"])
        for cookie in state.get("cookies", [])
        if isinstance(cookie.get("expires"), (int, float)) and cookie["expires"] > 0
    ]
    expires_at = datetime.fromtimestamp(min(expiries), UTC) if expiries else None
    with session_scope() as session:
        BrowserSessionRepository(session).save(PRINTIFY_SESSION, encrypted, expires_at=expires_at)
    return {"source": PRINTIFY_SESSION, "connected": True, "expires_at": expires_at}


def browser_session_health(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    with session_scope() as session:
        record = BrowserSessionRepository(session).get(PRINTIFY_SESSION)
        if record is None:
            return {"source": PRINTIFY_SESSION, "healthy": False, "detail": "not connected"}
        expired = record.expires_at is not None and record.expires_at <= datetime.now(UTC)
        decryptable = True
        try:
            _cipher(settings).decrypt(record.encrypted_state)
        except RuntimeError, ValueError:
            decryptable = False
        return {
            "source": PRINTIFY_SESSION,
            "healthy": record.healthy and not expired and decryptable,
            "detail": (
                "session expired"
                if expired
                else "session cannot be decrypted"
                if not decryptable
                else record.detail
            ),
            "expires_at": record.expires_at,
            "updated_at": record.updated_at,
        }


async def collect_printify_costs(
    product: CatalogProduct, settings: Settings | None = None
) -> dict[int, int]:
    settings = settings or get_settings()
    if settings.provider_mode == "fake":
        costs = {
            item.variant_id: item.production_cost_cents
            for item in product.variants
            if item.production_cost_cents is not None
        }
        observed_at = datetime.now(UTC)
        with session_scope() as session:
            repository = CatalogRepository(session)
            for variant_id, cost in costs.items():
                repository.observe_cost(
                    account_plan="fixture",
                    blueprint_id=product.blueprint_id,
                    print_provider_id=product.print_provider_id,
                    variant_id=variant_id,
                    cost_cents=cost,
                    source_fingerprint=product.source_fingerprint,
                    evidence={"fixture": True, "extractor_version": "1"},
                    observed_at=observed_at,
                )
        return costs
    with session_scope() as session:
        record = BrowserSessionRepository(session).get(PRINTIFY_SESSION)
        if record is None or not record.healthy:
            raise RuntimeError("Printify browser session is not connected")
        if record.expires_at is not None and record.expires_at <= datetime.now(UTC):
            raise RuntimeError("Printify browser session has expired")
        storage_state = json.loads(_cipher(settings).decrypt(record.encrypted_state))

    from playwright.async_api import async_playwright

    url = (
        f"{settings.printify_dashboard_base_url.rstrip('/')}/app/products/"
        f"{product.blueprint_id}?print_provider_id={product.print_provider_id}"
    )
    variant_ids = {item.variant_id for item in product.variants if item.available}
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=settings.browser_headless)
            context = await browser.new_context(
                storage_state=storage_state, service_workers="block"
            )

            async def printify_only(route: Any) -> None:
                from urllib.parse import urlparse

                hostname = (urlparse(route.request.url).hostname or "").casefold()
                if hostname in {"printify.com", "printify.me"} or hostname.endswith(
                    (".printify.com", ".printify.me")
                ):
                    await route.continue_()
                else:
                    await route.abort()

            await context.route("**/*", printify_only)
            page = await context.new_page()
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=settings.browser_navigation_timeout_seconds * 1000,
            )
            if await page.locator("text=/captcha|verify you are human|sign in/i").count():
                raise RuntimeError(
                    "Printify browser session requires authentication or a challenge"
                )
            rows = await page.locator("[data-variant-id]").evaluate_all(
                """elements => elements.map(element => ({
                    variant_id: element.getAttribute('data-variant-id'),
                    cost: element.getAttribute('data-cost') || element.innerText
                }))"""
            )
            script_texts = await page.locator(
                "script[type='application/json'], script#__NEXT_DATA__"
            ).all_text_contents()
            content_fingerprint = hashlib.sha256((await page.content()).encode()).hexdigest()
            embedded_payloads = []
            for text in script_texts:
                try:
                    embedded_payloads.append(json.loads(text))
                except json.JSONDecodeError:
                    continue
            identities = embedded_product_identities(embedded_payloads)
            expected_identity = (product.blueprint_id, product.print_provider_id)
            if expected_identity not in identities:
                raise RuntimeError(
                    "Printify dashboard did not confirm the selected blueprint/provider identity"
                )
            costs = extract_variant_costs(
                variant_ids, rows=rows, embedded_payloads=embedded_payloads
            )
            await browser.close()
    except Exception as exc:
        with session_scope() as session:
            BrowserSessionRepository(session).mark_unhealthy(PRINTIFY_SESSION, str(exc))
        raise
    if set(costs) != variant_ids:
        missing = sorted(variant_ids - set(costs))
        raise RuntimeError(f"Printify cost collection is incomplete for variants {missing[:10]}")
    observed_at = datetime.now(UTC)
    with session_scope() as session:
        repository = CatalogRepository(session)
        for variant_id, cost in costs.items():
            repository.observe_cost(
                account_plan="authenticated",
                blueprint_id=product.blueprint_id,
                print_provider_id=product.print_provider_id,
                variant_id=variant_id,
                cost_cents=cost,
                source_fingerprint=content_fingerprint,
                evidence={"url": url, "extractor_version": "1"},
                observed_at=observed_at,
            )
    return costs


def fresh_costs(product: CatalogProduct, settings: Settings | None = None) -> dict[int, int]:
    settings = settings or get_settings()
    if settings.provider_mode == "fake":
        return {
            item.variant_id: item.production_cost_cents
            for item in product.variants
            if item.production_cost_cents is not None
        }
    cutoff = datetime.now(UTC) - timedelta(hours=settings.cost_freshness_hours)
    with session_scope() as session:
        records = CatalogRepository(session).latest_costs(
            product.blueprint_id, product.print_provider_id, observed_since=cutoff
        )
        return {variant_id: item.cost_cents for variant_id, item in records.items()}
