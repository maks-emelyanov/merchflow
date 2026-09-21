"""Turn color-specific contrast findings into options for one publication."""

from __future__ import annotations

from typing import Any

from merch.domain.print_areas import proportionally_compatible, variant_print_dimensions
from merch.schemas import ProductTemplate, QAReport, VariantConfig

# Ordered reserve shades are verified against Printify's live catalog at run
# time. Swatches are used only to screen artwork contrast. Keep broad, familiar
# colors first so a product does not fill up with novelty shades.
BELLA_FALLBACK_COLORS = {
    "Silver": "#D6D5D1",
    "Forest": "#20362F",
    "Steel Blue": "#5F849C",
    "Sage": "#97A798",
    "Heather Navy": "#3B4559",
    "Heather Dust": "#CDC6BD",
    "Baby Blue": "#83B0C1",
    "Heather Forest": "#48655B",
    "Deep Teal": "#00597A",
    "Mint": "#A0DAB3",
    "Pink": "#F7CED7",
    "Rust": "#9E483F",
    "Heather Maroon": "#5B2B42",
    "Heather Military Green": "#6C6E60",
    "Heather True Royal": "#24509A",
    "Kelly": "#007A53",
    "Orange": "#FF6A39",
    "Black Heather": "#454038",
    "Dark Olive": "#36362D",
    "Heather Prism Natural": "#D2CFC4",
    "Heather Ice Blue": "#C4E1DE",
    "Tan": "#B8B298",
    "Heather Slate": "#586975",
    "Heather Peach": "#F3BE8D",
    "Heather Kelly": "#00965E",
    "Heather Olive": "#7F7457",
    "Heather Prism Blue": "#A5B3CC",
    "Heather Prism Mint": "#A0DAB3",
    "Heather Prism Peach": "#EABEB0",
    "Heather Brown": "#5E4B3C",
    "Evergreen": "#115740",
    "Heather Blue Lagoon": "#86A1A9",
    "Heather Prism Lilac": "#C7A1B2",
}

COMFORT_COLORS_FALLBACK_COLORS = {
    "Graphite": "#373231",
    "Grey": "#7A7F79",
    "Navy": "#263040",
    "Blue Spruce": "#536758",
    "Chambray": "#D9EDF5",
    "Light Green": "#738874",
    "Khaki": "#AEA583",
    "Seafoam": "#609A95",
    "Orchid": "#CBB3CC",
    "Watermelon": "#DA665F",
    "Brick": "#915C5C",
    "Denim": "#4E5064",
    "Berry": "#875570",
    "Bright Salmon": "#FF796C",
    "Burnt Orange": "#E27C4B",
    "Chalky Mint": "#A7D9D4",
    "Chili": "#853F44",
    "China Blue": "#43516E",
    "Citrus": "#FFC86E",
    "Crunchberry": "#EB7CA2",
    "Flo Blue": "#7682C2",
    "Granite": "#8A8E90",
    "Grape": "#645C81",
    "Hemp": "#676A4A",
    "Hydrangea": "#B2DAF3",
    "Ice Blue": "#7B8E95",
    "Island Green": "#5AB98F",
    "Island Reef": "#A2D8C2",
    "Lagoon Blue": "#89E4ED",
    "Melon": "#FF9A5F",
    "Midnight": "#3F485B",
    "Mustard": "#D0AE6E",
    "Mystic Blue": "#647CA3",
    "Neon Lemon": "#C9DB78",
    "Neon Pink": "#F57CAF",
    "Neon Red Orange": "#FF867B",
    "Neon Violet": "#E8ACE3",
    "Paprika": "#FF4645",
    "Peachy": "#F7C3AE",
    "Periwinkle": "#6570AF",
    "Red": "#A80D27",
    "Royal Caribe": "#5D8AC7",
    "Sandstone": "#A69F88",
    "Sapphire": "#03B2D3",
    "Terracotta": "#DB8C76",
    "Violet": "#A88FD7",
    "Washed Denim": "#8595B8",
    "Wine": "#5E5266",
}

FALLBACK_COLORS_BY_GARMENT = {
    (12, 39): BELLA_FALLBACK_COLORS,
    (706, 99): COMFORT_COLORS_FALLBACK_COLORS,
}
TARGET_COLORS_BY_GARMENT = {
    (12, 39): 14,
    (706, 99): 14,
}


def replacement_color_target(template: ProductTemplate) -> int | None:
    """Return the preserved palette size for garments with reserve colors."""
    target = TARGET_COLORS_BY_GARMENT.get(
        (template.blueprint_id, template.print_provider_id)
    )
    enabled_colors = {item.color for item in template.variants if item.enabled}
    return target if target is not None and len(enabled_colors) == target else None


def exclude_low_contrast_colors(
    report: QAReport, template: ProductTemplate, *, allow_all: bool = False
) -> tuple[QAReport, list[str]]:
    """Accept localized contrast failures by removing those garment colors."""
    enabled = {item.color for item in template.variants if item.enabled}
    lookup: dict[str, set[str]] = {}
    for item in template.variants:
        if item.enabled:
            lookup.setdefault(item.color.casefold(), set()).add(item.color)
            if item.color_hex:
                lookup.setdefault(item.color_hex.casefold(), set()).add(item.color)
    localized: dict[int, set[str]] = {}
    for index, issue in enumerate(report.issues):
        if issue.severity != "error" or "contrast" not in issue.code.casefold():
            continue
        if issue.affected_shirt_colors and all(
            color.casefold() in lookup for color in issue.affected_shirt_colors
        ):
            localized[index] = set().union(
                *(lookup[color.casefold()] for color in issue.affected_shirt_colors)
            )
    excluded = set().union(*localized.values()) if localized else set()
    if not excluded or (excluded == enabled and not allow_all):
        return report, []
    issues = [
        issue.model_copy(
            update={
                "severity": "warning",
                "recommended_fix": "This product omits the affected shirt colors",
            }
        )
        if index in localized
        else issue
        for index, issue in enumerate(report.issues)
    ]
    remaining_errors = any(issue.severity == "error" for issue in issues)
    return (
        report.model_copy(
            update={"issues": issues, "passed": not remaining_errors and (report.passed or bool(localized))}
        ),
        sorted(excluded),
    )


def catalog_replacement_groups(
    template: ProductTemplate, catalog: dict[str, Any]
) -> list[list[VariantConfig]]:
    """Return complete, available color groups with the same print area and sizes."""
    fallback_colors = FALLBACK_COLORS_BY_GARMENT.get(
        (template.blueprint_id, template.print_provider_id)
    )
    if fallback_colors is None:
        return []
    base_colors = {item.color for item in template.variants if item.enabled}
    enabled = [item for item in template.variants if item.enabled]
    sizes = list(dict.fromkeys(item.size for item in enabled))
    costs_by_size = {
        size: {item.production_cost_cents for item in enabled if item.size == size}
        for size in sizes
    }
    if any(len(values) != 1 for values in costs_by_size.values()):
        return []
    costs = {size: next(iter(values)) for size, values in costs_by_size.items()}
    by_options: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in catalog.get("variants", []):
        options = item.get("options", {})
        key = (options.get("color"), options.get("size"))
        if all(isinstance(value, str) for value in key):
            by_options.setdefault(key, []).append(item)
    base_ids = {item.variant_id for item in template.variants}
    candidate_ids: set[int] = set()
    groups: list[list[VariantConfig]] = []
    for color, swatch in fallback_colors.items():
        if color in base_colors:
            continue
        group: list[VariantConfig] = []
        for size in sizes:
            matches = by_options.get((color, size), [])
            if len(matches) != 1:
                break
            remote = matches[0]
            variant_id = remote.get("id")
            if (
                remote.get("is_available") is False
                or type(variant_id) is not int
                or variant_id <= 0
            ):
                break
            try:
                dimensions = variant_print_dimensions(
                    remote,
                    position=template.position,
                    decoration_method=template.decoration_method,
                )
            except ValueError:
                break
            if (
                dimensions[0] > template.print_width
                or dimensions[1] > template.print_height
                or not proportionally_compatible(
                    dimensions, (template.print_width, template.print_height)
                )
            ):
                break
            group.append(
                VariantConfig(
                    variant_id=variant_id,
                    title=str(remote.get("title") or f"{color} / {size}"),
                    color=color,
                    color_hex=swatch,
                    size=size,
                    production_cost_cents=costs[size],
                )
            )
        if len(group) == len(sizes):
            group_ids = {item.variant_id for item in group}
            if len(group_ids) != len(group) or group_ids & (base_ids | candidate_ids):
                continue
            groups.append(group)
            candidate_ids.update(group_ids)
    return groups


def full_color_publication_template(
    template: ProductTemplate,
    excluded_colors: set[str],
    candidate_groups: list[list[VariantConfig]],
    rejected_candidates: set[str],
) -> ProductTemplate | None:
    """Keep passing base colors and fill every vacant slot from vetted reserves."""
    base_colors = {item.color for item in template.variants if item.enabled}
    if excluded_colors - base_colors:
        raise ValueError("QA excluded a shirt color outside the approved template")
    retained = [
        item for item in template.variants if item.enabled and item.color not in excluded_colors
    ]
    needed = len(base_colors) - len({item.color for item in retained})
    replacements = [
        group for group in candidate_groups
        if group[0].color not in rejected_candidates and group[0].color not in base_colors
    ][:needed]
    if len(replacements) != needed:
        return None
    variants = retained + [item for group in replacements for item in group]
    featured = next(
        (item for item in variants if item.variant_id == template.featured_variant().variant_id),
        None,
    ) or next(
        (item for item in variants if item.size == template.featured_variant().size),
        variants[0],
    )
    data = template.model_dump(mode="json")
    data["variants"] = [item.model_dump(mode="json") for item in variants]
    data["featured_variant_id"] = featured.variant_id
    return ProductTemplate.model_validate(data)


def publication_template(
    template: ProductTemplate,
    excluded_colors: list[str],
    snapshot: dict[str, Any] | None = None,
) -> ProductTemplate:
    """Use the saved product palette, or disable excluded colors for older runs."""
    if snapshot is not None:
        publication = ProductTemplate.model_validate(snapshot)
        if (
            publication.blueprint_id != template.blueprint_id
            or publication.print_provider_id != template.print_provider_id
            or publication.print_width != template.print_width
            or publication.print_height != template.print_height
        ):
            raise ValueError("Publication snapshot does not match the approved garment")
        return publication
    excluded = set(excluded_colors)
    enabled_colors = {item.color for item in template.variants if item.enabled}
    if excluded - enabled_colors:
        raise ValueError("QA excluded a shirt color outside the approved template")
    variants = [
        item.model_copy(update={"enabled": item.enabled and item.color not in excluded})
        for item in template.variants
    ]
    available = [item for item in variants if item.enabled]
    if not available:
        raise ValueError("No shirt colors remain after contrast QA")
    original_featured = template.featured_variant()
    featured = next(
        (item for item in available if item.variant_id == original_featured.variant_id),
        None,
    ) or next(
        (item for item in available if item.size == original_featured.size),
        available[0],
    )
    data = template.model_dump(mode="json")
    data["variants"] = [item.model_dump(mode="json") for item in variants]
    data["featured_variant_id"] = featured.variant_id
    return ProductTemplate.model_validate(data)
