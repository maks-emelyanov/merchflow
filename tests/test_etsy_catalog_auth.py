from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from merch.config import Settings
from merch.schemas import CatalogProduct, Channel, ProductTemplate
from merch.services import etsy_catalog


@pytest.mark.asyncio
async def test_resolve_etsy_profile_uses_resolved_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        provider_mode="live",
        etsy_customer_shipping_cents=0,
    )
    seen: dict[str, object] = {}

    async def resolved_token(current: Settings) -> str:
        seen["token_settings"] = current
        return "encrypted-store-token"

    class FakeEtsyStorefrontClient:
        def __init__(
            self,
            current: Settings,
            access_token: str | None = None,
        ) -> None:
            seen["client_settings"] = current
            seen["access_token"] = access_token

        async def seller_taxonomy_nodes(self) -> list[dict[str, object]]:
            return [{"id": 11, "name": "T Shirt", "level": 1}]

        async def taxonomy_properties(self, taxonomy_id: int) -> list[dict[str, object]]:
            assert taxonomy_id == 11
            return [
                {
                    "property_id": 100,
                    "display_name": "Color",
                    "supports_variations": True,
                }
            ]

        async def shipping_profile(self, profile_id: int) -> dict[str, object]:
            return {
                "shipping_profile_id": profile_id,
                "shipping_profile_destinations": [
                    {
                        "destination_country_iso": "US",
                        "primary_cost": {"amount": 0, "divisor": 100},
                    }
                ],
            }

        async def return_policy(self, policy_id: int) -> dict[str, int]:
            return {"return_policy_id": policy_id}

        async def readiness_states(self) -> list[dict[str, int]]:
            return [{"readiness_state_id": 33}]

        async def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(etsy_catalog, "etsy_access_token", resolved_token)
    monkeypatch.setattr(etsy_catalog, "EtsyStorefrontClient", FakeEtsyStorefrontClient)
    defaults = SimpleNamespace(
        taxonomy_id=11,
        shipping_profile_id=22,
        return_policy_id=44,
        readiness_state_id=33,
        production_partner_ids=[55],
        quantity=7,
    )
    template = SimpleNamespace(
        channels=[
            SimpleNamespace(
                channel=Channel.ETSY,
                enabled=True,
                etsy_listing_defaults=defaults,
            )
        ]
    )
    product = SimpleNamespace(title="T Shirt", description="", tags=["shirt"])

    profile = await etsy_catalog.resolve_etsy_profile(
        cast(CatalogProduct, product),
        ["Color"],
        cast(ProductTemplate, template),
        settings,
    )

    assert seen["token_settings"] is settings
    assert seen["client_settings"] is settings
    assert seen["access_token"] == "encrypted-store-token"
    assert seen["closed"] is True
    assert profile.variation_property_ids == {"Color": 100}
    assert profile.shipping_profile_id == 22
