"""Small, licensed font registry shared by shaping and raster measurement."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from PIL import ImageFont

DEFAULT_FONT_FAMILY = "Noto Sans"
DEFAULT_FONT_FILE = Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf")

# Delivered by fonts-noto-core, fonts-noto-mono (OFL-1.1), and
# fonts-roboto-slab (Apache-2.0 in Debian bookworm), including their licenses.
FONT_FAMILIES = {
    "sans": "Noto Sans",
    "serif": "Noto Serif",
    "slab": "Roboto Slab",
    "display": "Noto Serif Display",
    "mono": "Noto Sans Mono",
}


@dataclass(frozen=True)
class ResolvedFont:
    family: str
    file: Path
    weight: int
    source: Literal["registry", "custom"]


def _fontconfig_match(family: str, weight: int) -> tuple[list[str], Path]:
    # A filename cannot safely be inferred: distributions ship Roboto Slab as
    # OTF or TTF. Verify the family because fc-match silently substitutes fonts.
    result = subprocess.run(
        [
            "fc-match", "--format=%{family}\n%{file}",
            f"{family}:weight={weight_to_fontconfig(weight)}",
        ],
        capture_output=True, text=True, check=True, timeout=10,
    )
    names, separator, filename = result.stdout.partition("\n")
    if not separator or not filename:
        raise ValueError(f"Fontconfig could not resolve {family}")
    return names.split(","), Path(filename)


def weight_to_fontconfig(weight: int) -> int:
    """Translate CSS weights to fontconfig's distinct numeric weight scale."""
    weights = {100: 0, 200: 40, 300: 50, 400: 80, 500: 100,
               600: 180, 700: 200, 800: 205, 900: 210}
    return weights[min(weights, key=lambda item: abs(item - weight))]


def _file_weight(file: Path) -> int:
    style = (ImageFont.truetype(str(file), 16).getname()[1] or "").lower().replace(" ", "")
    for names, weight in (
        (("thin", "hairline"), 100),
        (("extralight", "ultralight"), 200),
        (("light",), 300),
        (("semibold", "demibold", "demi"), 600),
        (("extrabold", "ultrabold"), 800),
        (("black", "heavy"), 900),
        (("bold",), 700),
        (("medium",), 500),
    ):
        if any(name in style for name in names):
            return weight
    return 400


@lru_cache(maxsize=10)
def _registry_font(category: str, weight: int) -> ResolvedFont:
    family = FONT_FAMILIES[category]
    families, file = _fontconfig_match(family, weight)
    if family not in families or not file.is_file():
        raise ValueError(f"Required font {family} is not installed")
    if _file_weight(file) != weight:
        raise ValueError(f"Required font {family} weight {weight} is not installed")
    return ResolvedFont(family, file, weight, "registry")


def resolve_font(
    category: str,
    requested_weight: int,
    *,
    family: str | None = None,
    file: Path | None = None,
) -> ResolvedFont:
    """Resolve a registry face, or honor an explicit family/file pair."""
    if family is not None or file is not None:
        if family is None or file is None:
            raise ValueError("A custom font requires both its family and file")
        if not file.is_file():
            raise ValueError(f"Custom font file does not exist: {file}")
        return ResolvedFont(family, file, _file_weight(file), "custom")
    if category not in FONT_FAMILIES:
        raise ValueError(f"Unsupported font category: {category}")
    return _registry_font(category, 400 if requested_weight < 550 else 700)


def svg_uses_font(font: ResolvedFont) -> bool:
    """Avoid SVG silently substituting an unregistered custom font file."""
    if font.source == "registry":
        return True
    try:
        families, file = _fontconfig_match(font.family, font.weight)
        return font.family in families and file.resolve() == font.file.resolve()
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
