"""Validate one shared artwork canvas against Printify variant placeholders."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def variant_print_dimensions(
    variant: dict[str, Any], *, position: str, decoration_method: str
) -> tuple[int, int]:
    """Return the sole matching positive print area for a catalog variant."""
    areas = [
        area
        for area in variant.get("placeholders", [])
        if area.get("position") == position
        and area.get("decoration_method") == decoration_method
    ]
    label = f"{position}-{decoration_method.upper()}"
    if len(areas) != 1:
        raise ValueError(f"Expected one {label} print area")
    width, height = areas[0].get("width"), areas[0].get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError(f"Invalid {label} print dimensions")
    return width, height


def proportionally_compatible(
    dimensions: tuple[int, int], canvas: tuple[int, int], *, tolerance: float = 1.0
) -> bool:
    """Allow only the one-pixel rounding seen between garment size placeholders."""
    width, height = dimensions
    canvas_width, canvas_height = canvas
    if min(width, height, canvas_width, canvas_height) <= 0:
        return False
    return (
        abs(width - canvas_width * height / canvas_height) <= tolerance
        and abs(height - canvas_height * width / canvas_width) <= tolerance
    )


def largest_compatible_print_area(dimensions: Iterable[tuple[int, int]]) -> tuple[int, int]:
    """Return the largest shared canvas after validating every placeholder ratio."""
    values = list(dimensions)
    if not values:
        raise ValueError("No print dimensions were selected")
    canvas = (max(width for width, _ in values), max(height for _, height in values))
    if any(not proportionally_compatible(item, canvas) for item in values):
        raise ValueError("Selected variants have incompatible print dimensions")
    return canvas
