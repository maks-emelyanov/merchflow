"""Source-aware research feedback without inventing missing funnel measurements."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

FIELDS = ("orders", "units", "gross_revenue_cents", "visits", "impressions", "favorites")


def summarize_performance(
    rows: list[dict[str, Any]], concepts: dict[str, dict[str, Any]], days: int = 90
) -> dict[str, Any]:
    # Amazon's ASIN rows describe a reporting window, not one day's sales.
    # Choose one latest window per product instead of summing overlapping pulls.
    amazon: dict[tuple[str, str], dict[str, Any]] = {}
    daily: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        product = str(row.get("external_product_id") or "unattributed")
        if row["source"] == "amazon_sales_and_traffic":
            key = (row["channel"], product)
            previous = amazon.get(key)
            if previous is None or row["metric_date"] > previous["metric_date"]:
                amazon[key] = row
        else:
            daily[(row["channel"], product, row["metric_date"])].append(row)

    buckets: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    def add(row: dict[str, Any], values: dict[str, int]) -> None:
        concept_id = str(row.get("concept_id") or "")
        window = row["source"] == "amazon_sales_and_traffic"
        start = str(row.get("period_start") or "unknown") if window else "daily"
        end = str(row.get("period_end") or row["metric_date"]) if window else "daily"
        key = (row["channel"], row["source"], concept_id, start, end)
        bucket = buckets.setdefault(key, {
            "channel": row["channel"], "source": row["source"],
            "concept_id": concept_id or None, "concept": concepts.get(concept_id),
            "reporting": "latest_window_per_product" if window else "daily_observations",
            "period_start": start if window else None,
            "period_end": end if window else None,
            "first_observed_date": row["metric_date"], "last_observed_date": row["metric_date"],
            "metrics": dict.fromkeys(FIELDS),
            "observations": dict.fromkeys(FIELDS, 0),
        })
        bucket["first_observed_date"] = min(bucket["first_observed_date"], row["metric_date"])
        bucket["last_observed_date"] = max(bucket["last_observed_date"], row["metric_date"])
        for field, value in values.items():
            bucket["metrics"][field] = (bucket["metrics"][field] or 0) + value
            bucket["observations"][field] += 1

    for group in daily.values():
        # Confirmed complete Etsy receipt windows own sales; CSV fills missing
        # fields and supersedes legacy API pulls that may have stopped at 100.
        ranked = sorted(group, key=lambda row: (
            0 if row["source"] == "etsy_api" and
            (row.get("completeness") or {}).get("receipt_window_complete") else
            {"etsy_csv": 1, "etsy_api": 2}.get(row["source"], 3), row["source"],
        ))
        consumed: set[str] = set()
        for row in ranked:
            values = {
                field: int(row[field]) for field in FIELDS
                if field not in consumed and row.get(field) is not None
            }
            if values:
                add(row, values)
                consumed.update(values)
        if not consumed:
            add(ranked[0], {})
    for row in amazon.values():
        add(row, {field: int(row[field]) for field in FIELDS if row.get(field) is not None})
    observations = sorted(
        buckets.values(), key=lambda item: (
            item["last_observed_date"], item["channel"], item["source"], item["concept_id"] or "",
        ), reverse=True,
    )
    return {
        "lookback_days": days,
        "limitations": [
            "Null metrics mean unknown, not zero. Observation counts describe coverage, not exposure.",
            "Etsy API and CSV overlap is resolved per product/day/metric; sources remain separate.",
            "Complete Etsy API windows take precedence; CSV takes precedence over legacy API rows with unknown receipt coverage.",
            "Amazon includes only the latest reporting window per product; older window dates may be unknown.",
            "Unpublished or failed production runs are not evidence of low customer demand.",
            "No conversion-rate or profitability conclusions are supported by these partial observations.",
        ],
        "omitted_groups": max(0, len(observations) - 60),
        "performance": observations[:60],
    }
