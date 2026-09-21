"""Deterministic print effects applied to clean, shaped artwork layers."""

from __future__ import annotations

import hashlib
import io
import json
import math
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from merch.domain.fonts import resolve_font, svg_uses_font
from merch.schemas import QAIssue, TypographySpec

RENDERER_VERSION = "print-effects-2"
ARC_RADIANS = math.pi / 3


def curve_line(image: Image.Image, direction: str) -> Image.Image:
    """Wrap a complete shaped line around a 60-degree annular sector.

    Both backends use this inverse mesh mapping. Shaping, accents, outlines,
    and ligatures travel with the line, rather than rotating Unicode codepoints.
    """
    if direction == "none":
        return image.copy()
    if direction not in {"up", "down"}:
        raise ValueError(f"Unsupported arc: {direction}")
    if direction == "down":
        return ImageOps.flip(curve_line(ImageOps.flip(image), "up"))
    radius = image.width / ARC_RADIANS
    outer = radius + image.height
    margin = 3
    half_width = outer * math.sin(ARC_RADIANS / 2)
    size = (
        math.ceil(2 * half_width) + 2 * margin,
        math.ceil(outer - radius * math.cos(ARC_RADIANS / 2)) + 2 * margin,
    )
    center_x, center_y = size[0] / 2, outer + margin

    def source(x: int, y: int) -> tuple[float, float]:
        dx, dy = x - center_x, center_y - y
        angle = math.atan2(dx, dy)
        return angle * radius + image.width / 2, outer - math.hypot(dx, dy)

    mesh = []
    # Fine enough to keep the curvature smooth even on small preview layers.
    step = max(4, min(16, image.height // 8))
    for y in range(0, size[1], step):
        for x in range(0, size[0], step):
            right, bottom = min(x + step, size[0]), min(y + step, size[1])
            quad = (*source(x, y), *source(x, bottom), *source(right, bottom), *source(right, y))
            mesh.append(((x, y, right, bottom), quad))
    curved = image.transform(size, Image.Transform.MESH, mesh, Image.Resampling.BICUBIC)
    bounds = curved.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError("Curving the text produced an empty layer")
    return curved.crop(bounds)


def _font(font_file: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_file), size=size)


def _shaped_line(
    line: str,
    spec: TypographySpec,
    font_size: int,
    font_family: str,
    font_file: Path,
    font_weight: int,
    rsvg: str | None,
) -> Image.Image:
    font = _font(font_file, font_size)
    stroke = max(1, font_size // 30) if spec.outline else 0
    box = font.getbbox(line, stroke_width=stroke)
    tracking = spec.letter_spacing * font_size * max(0, len(line) - 1)
    # SVG and Pillow font metrics can differ. Leave a full em on every side and
    # retry the SVG viewport if the actual shaped line touches an edge.
    margin = font_size
    width = max(1, math.ceil(box[2] - box[0] + abs(tracking))) + margin * 2
    height = math.ceil(max(1, box[3] - box[1])) + margin * 2
    if rsvg:
        for _ in range(3):
            text = (
                f'<text x="{margin}" y="{margin - box[1]}" xml:space="preserve" '
                f'font-family={quoteattr(font_family)} font-size="{font_size}" '
                f'font-weight="{font_weight}" letter-spacing="{spec.letter_spacing}em" '
                f"fill={quoteattr(spec.primary_color)} "
                f'stroke={quoteattr(spec.outline or "none")} stroke-width="{stroke * 2}" '
                f'paint-order="stroke fill">{escape(line)}</text>'
            )
            svg = (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
                f"{text}</svg>"
            )
            result = subprocess.run(
                [rsvg, "--format=png"],
                input=svg.encode(),
                capture_output=True,
                check=True,
                timeout=60,
            )
            layer = Image.open(io.BytesIO(result.stdout)).convert("RGBA")
            bounds = layer.getchannel("A").getbbox()
            if (
                bounds
                and bounds[0] > 0
                and bounds[1] > 0
                and bounds[2] < width
                and bounds[3] < height
            ):
                return layer.crop(bounds)
            width *= 2
            height *= 2
            margin *= 2
        raise ValueError("Shaped SVG text is empty or exceeds its rendering viewport")
    layer = Image.new("RGBA", (width, height))
    ImageDraw.Draw(layer).text(
        (margin - box[0], margin - box[1]),
        line,
        font=font,
        fill=spec.primary_color,
        stroke_width=stroke,
        stroke_fill=spec.outline or spec.primary_color,
    )
    bounds = layer.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError("The text line contains no printable glyphs")
    layer = layer.crop(bounds)
    return layer


def typography_layer(
    size: tuple[int, int],
    spec: TypographySpec,
    font_family: str | None,
    font_file: Path | None,
) -> tuple[Image.Image, dict[str, Any], list[QAIssue]]:
    rsvg = shutil.which("rsvg-convert")
    metadata: dict[str, Any] = {
        "requested_arc": spec.text_arc_or_shape,
        "applied_arc": "none",
        "backend": "svg" if rsvg else "pillow",
        "font_hash": None,
        "requested_font_category": spec.font_category,
        "requested_font_weight": spec.font_weight,
        "font_family": None,
        "applied_font_weight": None,
        "font_source": "custom" if font_family is not None or font_file is not None else "registry",
        "bounds": None,
        "font_size": 0,
        "requested_letter_spacing": spec.letter_spacing,
        "applied_letter_spacing": spec.letter_spacing if rsvg else 0,
    }
    canvas = Image.new("RGBA", size)
    try:
        selected_font = resolve_font(
            spec.font_category, spec.font_weight, family=font_family, file=font_file,
        )
        font_family, font_file = selected_font.family, selected_font.file
        if rsvg and not svg_uses_font(selected_font):
            rsvg = None
        metadata.update(
            font_hash=hashlib.sha256(font_file.read_bytes()).hexdigest(),
            font_family=font_family,
            applied_font_weight=selected_font.weight,
            font_source=selected_font.source,
            backend="svg" if rsvg else "pillow",
            applied_letter_spacing=spec.letter_spacing if rsvg else 0,
        )
        if not spec.line_breaks or any(not line.strip() for line in spec.line_breaks):
            raise ValueError("Every slogan line must contain printable text")
        max_width = max(1, int(size[0] * min(0.96, spec.relative_width)))
        max_height = max(1, int(size[1] * min(0.96, spec.relative_height)))
        font = _font(font_file, 100)
        longest = max(font.getlength(line) for line in spec.line_breaks) / 100
        font_size = max(24, min(512, math.ceil(max_width * 1.5 / max(1, longest))))
        lines = [
            curve_line(
                _shaped_line(
                    line, spec, font_size, font_family, font_file, selected_font.weight, rsvg,
                ),
                spec.text_arc_or_shape,
            )
            for line in spec.line_breaks
        ]
        # Arc heights and outlines are included before fitting the entire block.
        gap = max(font_size * 0.12, font_size * (spec.line_spacing - 1))
        group_size = (
            max(line.width for line in lines),
            math.ceil(sum(line.height for line in lines) + gap * (len(lines) - 1)),
        )
        group = Image.new("RGBA", group_size)
        y = 0.0
        for line in lines:
            x = {
                "left": 0,
                "center": (group.width - line.width) // 2,
                "right": group.width - line.width,
            }[spec.text_alignment]
            group.alpha_composite(line, (x, round(y)))
            y += line.height + gap
        scale = min(max_width / group.width, max_height / group.height)
        fitted = group.resize(
            (max(1, int(group.width * scale)), max(1, int(group.height * scale))),
            Image.Resampling.LANCZOS,
        )
        left = (size[0] - max_width) // 2
        x = {
            "left": left,
            "center": (size[0] - fitted.width) // 2,
            "right": left + max_width - fitted.width,
        }[spec.text_alignment]
        y_position = (size[1] - fitted.height) // 2
        canvas.alpha_composite(fitted, (x, y_position))
        metadata.update(
            applied_arc=spec.text_arc_or_shape,
            bounds=list(canvas.getchannel("A").getbbox() or ()),
            font_size=round(font_size * scale, 2),
        )
        issues = []
        if font_size * scale < max(8, min(size) * 0.004):
            issues.append(
                QAIssue(
                    code="TYPOGRAPHY_READABILITY",
                    severity="error",
                    message="The fitted slogan is too small for reliable printing.",
                    recommended_fix="Increase text bounds, reduce line count, or remove the arc while preserving the exact slogan.",
                )
            )
        return canvas, metadata, issues
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        metadata["error"] = str(exc)[:500]
        return (
            canvas,
            metadata,
            [
                QAIssue(
                    code="TYPOGRAPHY_LAYOUT",
                    severity="error",
                    message=f"Text rendering failed: {str(exc)[:300]}",
                    recommended_fix="Simplify the arc and layout, use a supported font and colors, and preserve the exact slogan.",
                )
            ],
        )


def distress_layer(
    image: Image.Image, level: int, *, seed: str
) -> tuple[Image.Image, dict[str, Any]]:
    """Remove small, separated patches of opaque ink without cutting silhouettes."""
    if not 0 <= level <= 5:
        raise ValueError("Distress level must be between 0 and 5")
    metadata: dict[str, Any] = {
        "requested_level": level,
        "applied_level": 0,
        "target_fraction": 0.02 * level,
        "removed_fraction": 0.0,
        "seed": seed,
    }
    if level == 0:
        return image.copy(), metadata
    alpha = image.getchannel("A")
    bounds = alpha.getbbox()
    ink = sum(value * count for value, count in enumerate(alpha.histogram())) / 255
    if not bounds or not ink:
        return image.copy(), metadata
    unit = max(1, round(min(image.size) / 750))
    solid = alpha.point(lambda value: 255 if value >= 250 else 0)
    # Erosion protects outlines and thin strokes. Protect internal color edges as
    # well, so small details in a solid illustration aren't mistaken for blank ink.
    eligible = solid.filter(ImageFilter.BoxBlur(unit)).point(
        lambda value: 255 if value == 255 else 0
    )
    edges = image.convert("RGB").filter(ImageFilter.FIND_EDGES)
    red, green, blue = edges.split()
    edges_mask = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(
        lambda value: 255 if value > 24 else 0
    )
    edges_mask = edges_mask.filter(ImageFilter.BoxBlur(unit)).point(
        lambda value: 255 if value else 0
    )
    eligible = ImageChops.subtract(eligible, edges_mask)
    rng = random.Random(seed)
    removal = Image.new("L", image.size)
    target = int(ink * 0.02 * level)
    removed = 0
    unsuccessful = 0
    for _ in range(min(120_000, max(1000, target * 15 // (unit * unit)))):
        if removed >= target or unsuccessful >= 3000:
            break
        scratch = rng.random() < 0.3
        width = rng.randint(3, 7) * unit if scratch else rng.randint(2, 4) * unit
        height = rng.randint(1, 2) * unit if scratch else rng.randint(2, 4) * unit
        x = rng.randrange(bounds[0], bounds[2])
        y = rng.randrange(bounds[1], bounds[3])
        box = (x, y, x + width, y + height)
        if box[2] > image.width or box[3] > image.height:
            unsuccessful += 1
            continue
        patch = Image.new("L", (width, height))
        draw = ImageDraw.Draw(patch)
        if scratch:
            draw.line((0, height // 3, width - 1, 2 * height // 3), fill=255, width=unit)
        else:
            draw.ellipse((0, 0, width - 1, height - 1), fill=255)
        area = patch.histogram()[255]
        if area > target - removed or ImageChops.subtract(patch, eligible.crop(box)).getbbox():
            unsuccessful += 1
            continue
        removal.paste(255, box, patch)
        # Keep a protected bridge between adjacent holes.
        ImageDraw.Draw(eligible).rectangle(
            (x - unit, y - unit, x + width + unit, y + height + unit), fill=0
        )
        removed += area
        unsuccessful = 0
    result = image.copy()
    result.putalpha(ImageChops.subtract(alpha, removal))
    fraction = removed / ink
    metadata.update(
        removed_fraction=round(fraction, 6), applied_level=min(level, round(fraction / 0.02))
    )
    if removed < target * 0.9:
        metadata["reduced_for_detail"] = True
    return result, metadata


def apply_design_effects(
    canvas: Image.Image,
    typography: TypographySpec | None,
    *,
    artwork_distress_level: int = 0,
    font_family: str | None = None,
    font_file: Path | None = None,
) -> tuple[Image.Image, dict[str, Any], list[QAIssue]]:
    text_layer = Image.new("RGBA", canvas.size)
    typography_metadata: dict[str, Any] = {
        "requested_arc": "none",
        "applied_arc": "none",
        "backend": "none",
        "bounds": None,
    }
    issues: list[QAIssue] = []
    if typography:
        text_layer, typography_metadata, issues = typography_layer(
            canvas.size, typography, font_family, font_file
        )
    scope = (
        "design"
        if artwork_distress_level
        else "text"
        if typography and typography.distress_level
        else "none"
    )
    level = artwork_distress_level or (typography.distress_level if typography else 0)
    clean = Image.alpha_composite(canvas, text_layer)
    seed_material = {
        "renderer": RENDERER_VERSION,
        "size": canvas.size,
        "scope": scope,
        "clean_sha256": hashlib.sha256(clean.tobytes()).hexdigest(),
        "typography": typography.model_dump(exclude={"distress_level"}) if typography else None,
        "font_hash": typography_metadata.get("font_hash"),
    }
    seed = hashlib.sha256(json.dumps(seed_material, sort_keys=True).encode()).hexdigest()
    effect_source = clean if scope != "text" else text_layer
    try:
        effected, distress = distress_layer(effect_source, level, seed=seed)
    except (OSError, ValueError) as exc:
        effected = effect_source
        distress = {
            "requested_level": level,
            "applied_level": 0,
            "target_fraction": 0.02 * level,
            "removed_fraction": 0.0,
            "seed": seed,
            "error": str(exc)[:500],
        }
        issues.append(
            QAIssue(
                code="DISTRESS_PRINTABILITY",
                severity="error",
                message=f"Distress rendering failed: {str(exc)[:300]}",
                recommended_fix="Reduce or disable distress and preserve the clean artwork details.",
            )
        )
    result = Image.alpha_composite(canvas, effected) if scope == "text" else effected
    distress.update(
        scope=scope,
        requested_text_level=typography.distress_level if typography else 0,
        requested_artwork_level=artwork_distress_level,
        text_distress_suppressed=bool(
            artwork_distress_level and typography and typography.distress_level
        ),
    )
    warnings = []
    if distress.get("reduced_for_detail"):
        warnings.append("Distress was reduced to protect narrow strokes and small details.")
    metadata = {
        "renderer_version": RENDERER_VERSION,
        "typography": typography_metadata,
        "distress": distress,
        "warnings": warnings,
    }
    return result, metadata, issues
