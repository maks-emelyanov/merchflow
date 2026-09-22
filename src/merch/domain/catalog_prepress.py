"""Method-aware fail-closed prepress checks for catalog artwork."""

from __future__ import annotations

import io
from typing import cast

from PIL import Image

from merch.schemas import PrintSurface


def validate_surface_artwork(data: bytes, surface: PrintSurface) -> list[str]:
    """Return blocking issues for an artwork/surface pair."""
    issues: list[str] = []
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGBA")
        if image.size != (surface.width, surface.height):
            issues.append(
                f"dimensions are {image.width}x{image.height}; expected "
                f"{surface.width}x{surface.height}"
            )
        alpha = image.getchannel("A")
        alpha_extrema = cast(tuple[int, int], alpha.getextrema())
        has_transparency = alpha_extrema[0] < 255
        if surface.placement in {"placed", "restricted_palette"} and not has_transparency:
            issues.append("placed artwork must include a transparent background")
        if surface.placement in {"full_bleed", "repeat"}:
            if has_transparency:
                issues.append("full-bleed artwork cannot contain transparent pixels")
            edge_alpha = [
                *(cast(int, alpha.getpixel((x, 0))) for x in range(image.width)),
                *(cast(int, alpha.getpixel((x, image.height - 1))) for x in range(image.width)),
                *(cast(int, alpha.getpixel((0, y))) for y in range(image.height)),
                *(cast(int, alpha.getpixel((image.width - 1, y))) for y in range(image.height)),
            ]
            if any(value < 255 for value in edge_alpha):
                issues.append("full-bleed edge coverage is incomplete")
        if surface.placement == "restricted_palette":
            colors = image.convert("RGB").getcolors(maxcolors=17)
            if colors is None or len(colors) > 16:
                issues.append("restricted-palette artwork exceeds 16 colors")
    return issues
