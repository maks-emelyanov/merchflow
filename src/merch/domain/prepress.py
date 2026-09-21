from __future__ import annotations

import hashlib
import io
import math
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx
from PIL import Image, ImageCms, ImageColor, ImageDraw, ImageFilter

from merch.domain.design_effects import apply_design_effects
from merch.domain.fonts import DEFAULT_FONT_FAMILY, DEFAULT_FONT_FILE
from merch.schemas import QAIssue, QAReport, TypographySpec

MAX_GENERATION_EDGE = 3840
MAX_GENERATION_SHORT_EDGE = 2160
MAX_GENERATION_PIXELS = 8_294_400


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    sha256: str
    width: int
    height: int
    used_realesrgan: bool
    source_scale: float
    effects: dict[str, object] = field(default_factory=dict)
    issues: tuple[QAIssue, ...] = ()


def largest_generation_size(target_width: int, target_height: int) -> tuple[int, int]:
    scale = min(
        MAX_GENERATION_EDGE / max(target_width, target_height),
        MAX_GENERATION_SHORT_EDGE / min(target_width, target_height),
        math.sqrt(MAX_GENERATION_PIXELS / (target_width * target_height)),
    )
    if target_width * target_height < 655_360:
        scale = max(scale, 1.0)
    else:
        scale = min(scale, 1.0)
    width = max(16, int(target_width * scale) // 16 * 16)
    height = max(16, int(target_height * scale) // 16 * 16)
    while width * height > MAX_GENERATION_PIXELS:
        if width >= height:
            width -= 16
        else:
            height -= 16
    return width, height


def make_fixture_art(width: int, height: int) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad_x, pad_y = width // 8, height // 8
    draw.ellipse((pad_x, pad_y, width - pad_x, height - pad_y), fill=(239, 94, 80, 255))
    draw.polygon(
        [(width // 2, pad_y), (width - pad_x, height * 3 // 4), (pad_x, height * 3 // 4)],
        fill=(255, 209, 102, 255),
    )
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def render_typography_svg(
    base: Image.Image, typography: TypographySpec, font_family: str, font_file: Path
) -> Image.Image:
    rendered, _, _ = apply_design_effects(
        base, typography, font_family=font_family, font_file=font_file
    )
    return rendered


def prepare_artwork(
    source: bytes,
    target_width: int,
    target_height: int,
    *,
    typography: TypographySpec | None = None,
    font_family: str = DEFAULT_FONT_FAMILY,
    font_file: Path = DEFAULT_FONT_FILE,
    realesrgan_binary: Path | None = None,
    realesrgan_endpoint: str | None = None,
    flat_palette: list[str] | None = None,
    artwork_distress_level: int = 0,
) -> PreparedImage:
    image = Image.open(io.BytesIO(source)).convert("RGBA")
    if flat_palette:
        image = flatten_print_colors(image, flat_palette)
    visible = image.getchannel("A").point(lambda value: 255 if value >= 64 else 0)
    bounds = visible.getbbox()
    if bounds is not None:
        content_width = bounds[2] - bounds[0]
        content_height = bounds[3] - bounds[1]
        if content_width / image.width < 0.68 or content_height / image.height < 0.68:
            image = image.crop(bounds)
            fit_fraction = 0.78
        else:
            fit_fraction = 0.88
    else:
        fit_fraction = 0.88
    source_scale = min(
        target_width * fit_fraction / image.width,
        target_height * fit_fraction / image.height,
    )
    used_realesrgan = False
    if source_scale > 1.5 and realesrgan_binary and realesrgan_binary.exists():
        with tempfile.TemporaryDirectory(prefix="merch-upscale-") as directory:
            input_path = Path(directory) / "input.png"
            output_path = Path(directory) / "output.png"
            image.save(input_path)
            subprocess.run(
                [str(realesrgan_binary), "-i", str(input_path), "-o", str(output_path), "-s", "4"],
                check=True,
                timeout=600,
            )
            image = Image.open(output_path).convert("RGBA")
            used_realesrgan = True
    elif source_scale > 1.5 and realesrgan_endpoint:
        try:
            with httpx.Client(base_url=realesrgan_endpoint.rstrip("/"), timeout=600) as client:
                health = client.get("/health")
                health.raise_for_status()
                if health.json().get("healthy"):
                    source_buffer = io.BytesIO()
                    image.save(source_buffer, "PNG")
                    response = client.post(
                        "/upscale",
                        files={"image": ("input.png", source_buffer.getvalue(), "image/png")},
                    )
                    response.raise_for_status()
                    image = Image.open(io.BytesIO(response.content)).convert("RGBA")
                    used_realesrgan = True
        except httpx.HTTPError, OSError, ValueError:
            used_realesrgan = False
    scale = min(
        target_width * fit_fraction / image.width,
        target_height * fit_fraction / image.height,
    )
    image = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGBA", (target_width, target_height), (0, 0, 0, 0))
    canvas.alpha_composite(
        image, ((target_width - image.width) // 2, (target_height - image.height) // 2)
    )
    use_registry = font_family == DEFAULT_FONT_FAMILY and font_file == DEFAULT_FONT_FILE
    canvas, effects, issues = apply_design_effects(
        canvas,
        typography,
        artwork_distress_level=artwork_distress_level,
        font_family=None if use_registry else font_family,
        font_file=None if use_registry else font_file,
    )
    profile = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    # LCMS inserts the current time in the ICC header. Use a fixed creation date
    # so identical clean artwork and effects produce identical PNG hashes.
    profile[24:36] = struct.pack(">6H", 2000, 1, 1, 0, 0, 0)
    output = io.BytesIO()
    canvas.save(output, "PNG", dpi=(300, 300), icc_profile=bytes(profile), optimize=True)
    data = output.getvalue()
    return PreparedImage(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        width=target_width,
        height=target_height,
        used_realesrgan=used_realesrgan,
        source_scale=source_scale,
        effects=effects,
        issues=tuple(issues),
    )


def flatten_print_colors(image: Image.Image, palette: list[str]) -> Image.Image:
    """Make a flat-color illustration opaque and remove low-alpha image-model debris."""
    colors = []
    for entry in palette:
        match = re.search(r"#[0-9A-Fa-f]{6}\b", entry)
        if match:
            color = ImageColor.getrgb(match.group())
            if color not in colors:
                colors.append(color)
    if not colors:
        return image
    rgba = image.convert("RGBA")
    palette_image = Image.new("P", (1, 1))
    palette_image.putpalette(
        [value for color in [*colors, *([colors[0]] * (256 - len(colors)))] for value in color]
    )
    indexed = (
        rgba.convert("RGB")
        .quantize(palette=palette_image, dither=Image.Dither.NONE)
        .filter(ImageFilter.ModeFilter(9))
    )
    alpha = rgba.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
    indexed, alpha = _remove_small_color_regions(indexed, alpha)
    flat = indexed.convert("RGB")
    flat.putalpha(alpha)
    return flat


def _remove_small_color_regions(
    indexed: Image.Image, alpha: Image.Image
) -> tuple[Image.Image, Image.Image]:
    width, height = indexed.size
    original = indexed.tobytes()
    mask = bytearray(alpha.tobytes())
    cleaned = bytearray(original)
    seen = bytearray(len(original))
    min_area = max(25, round(width * height * 0.00015))
    for start in range(len(original)):
        if seen[start] or not mask[start]:
            continue
        color = original[start]
        seen[start] = 1
        region = [start]
        for pixel in region:
            x = pixel % width
            neighbors = (
                pixel - width if pixel >= width else -1,
                pixel + width if pixel + width < len(original) else -1,
                pixel - 1 if x else -1,
                pixel + 1 if x < width - 1 else -1,
            )
            for neighbor in neighbors:
                if (
                    neighbor >= 0
                    and mask[neighbor]
                    and not seen[neighbor]
                    and original[neighbor] == color
                ):
                    seen[neighbor] = 1
                    region.append(neighbor)
        if len(region) >= min_area:
            continue
        adjacent: dict[int, int] = {}
        for pixel in region:
            x = pixel % width
            neighbors = (
                pixel - width if pixel >= width else -1,
                pixel + width if pixel + width < len(original) else -1,
                pixel - 1 if x else -1,
                pixel + 1 if x < width - 1 else -1,
            )
            for neighbor in neighbors:
                if neighbor >= 0 and mask[neighbor] and original[neighbor] != color:
                    adjacent[original[neighbor]] = adjacent.get(original[neighbor], 0) + 1
        if adjacent:
            replacement = max(adjacent, key=adjacent.__getitem__)
            for pixel in region:
                cleaned[pixel] = replacement
        else:
            for pixel in region:
                mask[pixel] = 0
    result = Image.frombytes("P", (width, height), bytes(cleaned))
    palette = indexed.getpalette()
    if palette is None:
        raise ValueError("indexed artwork has no palette")
    result.putpalette(palette)
    return result, Image.frombytes("L", (width, height), bytes(mask))


def _luminance(rgb: tuple[int, int, int]) -> float:
    values = []
    for value in rgb:
        channel = value / 255
        values.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    first, second = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (first + 0.05) / (second + 0.05)


def deterministic_qa(
    data: bytes,
    *,
    expected_width: int,
    expected_height: int,
    shirt_colors: list[str],
    revision: int,
    max_bytes: int,
    source_scale: float = 1.0,
    used_realesrgan: bool = False,
    expected_text: str | None = None,
    rendered_text: str | None = None,
) -> QAReport:
    image = Image.open(io.BytesIO(data)).convert("RGBA")
    issues: list[QAIssue] = []
    if image.size != (expected_width, expected_height):
        issues.append(
            QAIssue(
                code="dimensions",
                severity="error",
                message="Output dimensions do not match the print area",
            )
        )
    if len(data) > max_bytes:
        issues.append(
            QAIssue(
                code="file_size",
                severity="error",
                message="PNG exceeds the configured upload limit",
            )
        )
    if expected_text is not None and rendered_text != expected_text:
        issues.append(
            QAIssue(
                code="text_equality",
                severity="error",
                message="Rendered typography text does not exactly match the approved slogan",
            )
        )
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        issues.append(
            QAIssue(code="empty", severity="error", message="Artwork is fully transparent")
        )
    else:
        pad_x = min(bbox[0], image.width - bbox[2]) / image.width
        pad_y = min(bbox[1], image.height - bbox[3]) / image.height
        if min(pad_x, pad_y) < 0.02:
            issues.append(
                QAIssue(
                    code="padding",
                    severity="warning",
                    message="Artwork has less than 2% edge padding",
                )
            )
    histogram = alpha.histogram()
    semi = sum(histogram[1:255])
    occupied = image.width * image.height - histogram[0]
    sample_image = image.copy()
    sample_image.thumbnail((160, 160), Image.Resampling.BOX)
    rgba_pixels = cast(list[tuple[int, int, int, int]], sample_image.get_flattened_data())
    opaque_pixels: list[tuple[int, int, int]] = [
        (pixel[0], pixel[1], pixel[2]) for pixel in rgba_pixels if pixel[3] >= 240
    ]
    if opaque_pixels:
        for color in shirt_colors:
            raw_background = ImageColor.getrgb(color)
            background = (raw_background[0], raw_background[1], raw_background[2])
            readable_fraction = sum(
                _contrast(pixel, background) >= 1.8 for pixel in opaque_pixels
            ) / len(opaque_pixels)
            if readable_fraction < 0.5:
                issues.append(
                    QAIssue(
                        code="contrast",
                        severity="error",
                        message=f"Less than half of opaque artwork contrasts with {color}",
                        affected_shirt_colors=[color],
                    )
                )
            if _luminance(background) < 0.2 and occupied and semi / occupied > 0.15:
                issues.append(
                    QAIssue(
                        code="dark_gradient",
                        severity="warning",
                        message=f"Semi-transparent gradients may print poorly on dark color {color}",
                    )
                )
    if source_scale > 1.5 and not used_realesrgan:
        issues.append(
            QAIssue(
                code="upscale_fallback",
                severity="warning",
                message="Large enlargement used deterministic resizing because Real-ESRGAN was unavailable",
            )
        )
    alpha_extrema = cast(tuple[int, int], alpha.getextrema())
    return QAReport(
        passed=not any(issue.severity == "error" for issue in issues),
        revision=revision,
        issues=issues,
        width=image.width,
        height=image.height,
        has_alpha=alpha_extrema[0] < 255,
        color_profile="sRGB IEC61966-2.1",
    )
