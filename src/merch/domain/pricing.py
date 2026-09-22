from __future__ import annotations

import math
import statistics

from merch.schemas import Channel, CompetitorListingSnapshot, PriceDecision, PriceQuote


def round_up_to_99(cents: float) -> int:
    """Return the smallest whole-dollar .99 price greater than or equal to cents."""
    if cents < 0:
        raise ValueError("price cannot be negative")
    return math.ceil((cents + 1) / 100) * 100 - 1


def quote_price(
    *,
    channel: Channel,
    variant_id: int,
    production_cost_cents: int,
    percent_fee: float,
    fixed_fee_cents: int,
    target_margin: float = 0.40,
) -> PriceQuote:
    denominator = 1 - percent_fee - target_margin
    if denominator <= 0:
        raise ValueError("fee and target margin leave no room for a valid price")
    raw = (production_cost_cents + fixed_fee_cents) / denominator
    retail = round_up_to_99(raw)
    fee = round(retail * percent_fee) + fixed_fee_cents
    margin = (retail - production_cost_cents - fee) / retail
    return PriceQuote(
        channel=channel,
        variant_id=variant_id,
        production_cost_cents=production_cost_cents,
        retail_price_cents=retail,
        estimated_fee_cents=fee,
        estimated_margin=margin,
    )


def comparable_median_delivered(
    listings: list[CompetitorListingSnapshot], *, minimum_prices: int = 3
) -> int | None:
    """Return the median delivered USD price only when the comparison set is usable."""
    values = sorted(
        item.delivered_price_cents
        for item in listings
        if item.currency == "USD" and item.delivered_price_cents is not None
    )
    if len(values) < minimum_prices:
        return None
    return round(statistics.median(values))


def _highest_99_at_or_below(limit_cents: int) -> int | None:
    if limit_cents < 99:
        return None
    candidate = (limit_cents // 100) * 100 + 99
    if candidate > limit_cents:
        candidate -= 100
    return candidate if candidate >= 99 else None


def competitive_price(
    *,
    variant_id: int,
    production_cost_cents: int,
    fulfillment_shipping_cents: int,
    customer_shipping_cents: int,
    percent_fee: float,
    fixed_fee_cents: int,
    benchmark_median_delivered_cents: int | None,
    target_margin: float = 0.40,
    discount_cents: int = 0,
) -> PriceDecision:
    """Choose the highest profitable .99 undercut, otherwise the margin floor."""
    if min(
        production_cost_cents,
        fulfillment_shipping_cents,
        customer_shipping_cents,
        fixed_fee_cents,
        discount_cents,
    ) < 0:
        raise ValueError("pricing inputs cannot be negative")
    denominator = 1 - percent_fee - target_margin
    if denominator <= 0:
        raise ValueError("fee and target margin leave no room for a valid price")
    fixed_costs = (
        production_cost_cents
        + fulfillment_shipping_cents
        + fixed_fee_cents
        + discount_cents
    )
    minimum_revenue = fixed_costs / denominator
    minimum_item_price = max(0, math.ceil(minimum_revenue - customer_shipping_cents))
    floor_price = round_up_to_99(minimum_item_price)

    undercut_candidate = None
    if benchmark_median_delivered_cents is not None:
        item_limit = benchmark_median_delivered_cents - customer_shipping_cents - 1
        undercut_candidate = _highest_99_at_or_below(item_limit)

    if undercut_candidate is not None and undercut_candidate >= floor_price:
        item_price = undercut_candidate
        undercut_status = "true"
        reason = "Highest .99 delivered price below the comparable median at the margin floor"
    else:
        item_price = floor_price
        if benchmark_median_delivered_cents is None:
            undercut_status = "unknown"
            reason = "Comparable set lacks three complete delivered prices; used the margin floor"
        else:
            undercut_status = "false"
            reason = "Comparable median cannot be undercut while preserving the margin floor"

    revenue = item_price + customer_shipping_cents
    estimated_fee = round(revenue * percent_fee) + fixed_fee_cents
    contribution = (
        revenue
        - production_cost_cents
        - fulfillment_shipping_cents
        - estimated_fee
        - discount_cents
    )
    margin = contribution / revenue if revenue else 0.0
    if margin + 1e-9 < target_margin:
        raise ValueError("calculated competitive price does not preserve the target margin")
    return PriceDecision(
        variant_id=variant_id,
        benchmark_median_delivered_cents=benchmark_median_delivered_cents,
        item_price_cents=item_price,
        customer_shipping_cents=customer_shipping_cents,
        production_cost_cents=production_cost_cents,
        fulfillment_shipping_cents=fulfillment_shipping_cents,
        estimated_fee_cents=estimated_fee,
        contribution_margin=margin,
        undercut_status=undercut_status,
        reason=reason,
    )
