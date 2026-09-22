"""Deterministic and vision-assisted safeguards for guided marketplace references."""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import cast

from PIL import Image, ImageDraw, ImageOps

from merch.schemas import (
    OriginalityReport,
    OriginalityVisionAssessment,
    SimilarityFinding,
)


def normalized_wording(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def wording_similarity(first: str, second: str) -> float:
    left = normalized_wording(first)
    right = normalized_wording(second)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _difference_hash_image(source: Image.Image) -> int:
    image = ImageOps.grayscale(source).resize((9, 8), Image.Resampling.LANCZOS)
    pixels = [cast(int, image.getpixel((x, y))) for y in range(8) for x in range(9)]
    value = 0
    for row in range(8):
        for column in range(8):
            value <<= 1
            value |= pixels[row * 9 + column] > pixels[row * 9 + column + 1]
    return value


def difference_hash(data: bytes) -> int:
    with Image.open(io.BytesIO(data)) as source:
        return _difference_hash_image(source)


def _cropped_hashes(data: bytes) -> set[int]:
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGB")
        width, height = image.size
        boxes = [
            (0, 0, width, height),
            (width // 10, height // 10, width * 9 // 10, height * 9 // 10),
            (0, 0, width * 4 // 5, height * 4 // 5),
            (width // 5, 0, width, height * 4 // 5),
            (0, height // 5, width * 4 // 5, height),
            (width // 5, height // 5, width, height),
        ]
        return {_difference_hash_image(image.crop(box)) for box in boxes}


def perceptual_hash_distance(first: bytes, second: bytes) -> int:
    return min(
        (left ^ right).bit_count()
        for left in _cropped_hashes(first)
        for right in _cropped_hashes(second)
    )


def make_contact_sheet(
    images: list[tuple[str, bytes]],
    *,
    tile_size: tuple[int, int] = (512, 512),
) -> bytes:
    if not images:
        raise ValueError("a contact sheet requires at least one image")
    width, height = tile_size
    sheet = Image.new("RGB", (width * len(images), height + 42), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (label, data) in enumerate(images):
        with Image.open(io.BytesIO(data)) as source:
            tile = ImageOps.contain(source.convert("RGBA"), (width, height))
            background = Image.new("RGBA", (width, height), "white")
            background.alpha_composite(
                tile, ((width - tile.width) // 2, (height - tile.height) // 2)
            )
            sheet.paste(background.convert("RGB"), (index * width, 0))
        draw.text((index * width + 8, height + 12), label[:60], fill="black")
    output = io.BytesIO()
    sheet.save(output, format="PNG")
    return output.getvalue()


def evaluate_originality(
    *,
    generated_image: bytes,
    generated_wording: str,
    references: list[tuple[str, bytes | None, str]],
    vision: OriginalityVisionAssessment,
    perceptual_block_distance: int = 8,
    wording_block_similarity: float = 0.80,
    minimum_originality_score: int = 80,
    maximum_copying_risk: int = 20,
) -> OriginalityReport:
    findings: list[SimilarityFinding] = []
    for reference_id, image, wording in references:
        distance = perceptual_hash_distance(generated_image, image) if image is not None else None
        similarity = wording_similarity(generated_wording, wording)
        reasons = []
        if distance is not None and distance <= perceptual_block_distance:
            reasons.append("perceptual image hash is too close to the reference")
        if similarity >= wording_block_similarity and len(normalized_wording(wording).split()) >= 3:
            reasons.append("printed wording is too similar to the reference")
        if vision.copying_risk > maximum_copying_risk:
            reasons.append("vision review found excessive copying risk")
        findings.append(
            SimilarityFinding(
                reference_listing_id=reference_id,
                perceptual_hash_distance=distance,
                wording_similarity=similarity,
                vision_similarity_risk=vision.copying_risk,
                blocking_reasons=reasons,
            )
        )
    passed = (
        vision.originality_score >= minimum_originality_score
        and vision.copying_risk <= maximum_copying_risk
        and all(not item.blocking_reasons for item in findings)
    )
    return OriginalityReport(
        passed=passed,
        originality_score=vision.originality_score,
        copying_risk=vision.copying_risk,
        findings=findings,
        checked_at=datetime.now(UTC),
    )
