from __future__ import annotations

import io
import shutil
import struct
from itertools import pairwise
from pathlib import Path
from typing import Literal

import pytest
from PIL import Image, ImageCms, ImageDraw

from merch.domain.design_effects import apply_design_effects, curve_line, distress_layer
from merch.domain.prepress import make_fixture_art, prepare_artwork
from merch.schemas import CreativeBrief, TypographySpec


@pytest.fixture
def effects_font() -> tuple[str, Path]:
    fonts = [
        ("DejaVu Sans", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        ("Noto Sans", Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf")),
    ]
    for family, path in fonts:
        if path.exists():
            return family, path
    pytest.skip("An installed font supporting diacritics is required")


def _typography(
    *,
    lines: list[str] | None = None,
    arc: Literal["none", "up", "down"] = "none",
    distress: int = 0,
) -> TypographySpec:
    lines = lines or ["Café, trails & starlight", "Let's wander together!"]
    return TypographySpec(
        exact_text=" ".join(lines), line_breaks=lines,
        letter_spacing=0.02, line_spacing=1.2, text_alignment="center",
        text_arc_or_shape=arc, outline="#FFFFFF", shadow=None,
        distress_level=distress, primary_color="#D97148", secondary_color=None,
        interaction_with_illustration="centered lettering",
        relative_width=0.85, relative_height=0.7,
    )


def _ink(image: Image.Image) -> int:
    return sum(index * count for index, count in enumerate(image.getchannel("A").histogram()))


def _solid_design() -> Image.Image:
    image = Image.new("RGBA", (360, 260))
    ImageDraw.Draw(image).rounded_rectangle((25, 25, 334, 234), radius=20, fill="#E38D45")
    return image


def test_no_curve_preserves_the_shaped_line() -> None:
    image = _solid_design()
    curved = curve_line(image, "none")
    assert curved.mode == "RGBA"
    assert curved.size == image.size
    assert curved.tobytes() == image.tobytes()


@pytest.mark.parametrize("direction", ["up", "down"])
def test_curve_makes_a_crest_or_bowl(direction: Literal["up", "down"]) -> None:
    image = Image.new("RGBA", (440, 60))
    ImageDraw.Draw(image).rectangle((10, 18, 429, 41), fill="white")
    curved = curve_line(image, direction)
    alpha = curved.getchannel("A")
    bounds = alpha.getbbox()
    assert bounds is not None
    left, _, right, _ = bounds

    def center_y(fraction: float) -> float:
        x = round(left + (right - left - 1) * fraction)
        values = list(alpha.crop((x, 0, x + 1, alpha.height)).tobytes())
        assert sum(values) > 0
        return sum(y * value for y, value in enumerate(values)) / sum(values)

    center = center_y(0.5)
    ends = (center_y(0.1) + center_y(0.9)) / 2
    if direction == "up":
        assert center < ends - 10
    else:
        assert center > ends + 10
    assert curved.mode == "RGBA"
    assert _ink(curved) >= _ink(image) * 0.8


def test_zero_distress_leaves_the_clean_layer_unchanged() -> None:
    image = _solid_design()
    distressed, metadata = distress_layer(image, 0, seed="repeatable-seed")
    assert distressed.tobytes() == image.tobytes()
    assert metadata["requested_level"] == metadata["applied_level"] == 0
    assert metadata["removed_fraction"] == 0


def test_default_effects_preserve_illustration_only_artwork(effects_font: tuple[str, Path]) -> None:
    image = _solid_design()
    family, font = effects_font
    rendered, metadata, issues = apply_design_effects(
        image, None, font_family=family, font_file=font,
    )
    assert rendered.tobytes() == image.tobytes()
    assert metadata["distress"]["scope"] == "none"
    assert metadata["distress"]["requested_level"] == 0
    assert not issues


def test_hybrid_prepress_reserves_a_nonoverlapping_bottom_text_band(
    monkeypatch: pytest.MonkeyPatch, effects_font: tuple[str, Path],
) -> None:
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: None)
    family, font = effects_font
    typography = _typography(lines=["KILN WEATHER BUREAU", "HEAT ADVISORY IN EFFECT"]).model_copy(
        update={
            "vertical_placement": "bottom",
            "relative_width": 0.88,
            "relative_height": 0.28,
        }
    )
    prepared = prepare_artwork(
        make_fixture_art(600, 720),
        800,
        1000,
        typography=typography,
        font_family=family,
        font_file=font,
    )
    layout = prepared.effects["layout"]
    illustration_bounds = layout["illustration_bounds"]
    text_bounds = layout["text_bounds"]
    assert illustration_bounds and text_bounds
    assert illustration_bounds[3] < text_bounds[1]
    assert layout["overlap_fraction"] == 0
    assert not [issue for issue in prepared.issues if issue.code == "TYPOGRAPHY_LAYOUT"]


def test_reserved_text_overlap_is_reported_as_layout_failure(
    monkeypatch: pytest.MonkeyPatch, effects_font: tuple[str, Path],
) -> None:
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: None)
    family, font = effects_font
    rendered, metadata, issues = apply_design_effects(
        _solid_design(),
        _typography(lines=["CENTERED WORDS"]).model_copy(
            update={"vertical_placement": "bottom", "relative_height": 0.3}
        ),
        font_family=family,
        font_file=font,
    )
    assert rendered.getchannel("A").getbbox()
    assert metadata["typography"]["overlap_fraction"] > 0.01
    assert any(issue.code == "TYPOGRAPHY_LAYOUT" for issue in issues)


def test_saved_creative_briefs_without_effects_default_to_clean_artwork() -> None:
    saved = {
        "concept_name": "Evening trails", "target_customer": "Hikers",
        "customer_motivation": "Enjoying time outdoors", "slogan": None,
        "design_mode": "illustration", "visual_concept": "A mountain trail at sunset",
        "composition": "Centered mountain and trail", "graphic_style": "Flat illustration",
        "palette": ["#E38D45"], "shirt_colors": ["#111111"],
        "typography_style": None, "generation_brief": "Isolated mountain trail, no text",
    }
    brief = CreativeBrief.model_validate(saved)
    assert brief.artwork_distress_level == 0
    assert brief.model_dump(exclude={"artwork_distress_level", "print_method", "strategy"}) == saved


def test_typography_rendering_failure_returns_a_reviewable_preview_and_failed_qa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: None)
    broken_font = tmp_path / "invalid-font.ttf"
    broken_font.write_bytes(b"This is not a font file")
    canvas = _solid_design()
    preview, metadata, issues = apply_design_effects(
        canvas, _typography(arc="up"), font_family="Broken font", font_file=broken_font,
    )
    assert preview.mode == "RGBA" and preview.size == canvas.size
    assert preview.tobytes() == canvas.tobytes()
    assert metadata["typography"]["requested_arc"] == "up"
    assert metadata["typography"]["applied_arc"] == "none"
    assert metadata["typography"]["error"]
    assert [(issue.code, issue.severity) for issue in issues] == [("TYPOGRAPHY_LAYOUT", "error")]
    assert issues[0].recommended_fix


@pytest.mark.parametrize("scope", ["text", "design"])
def test_distress_rendering_failure_preserves_clean_preview_and_reports_failed_qa(
    monkeypatch: pytest.MonkeyPatch, effects_font: tuple[str, Path], scope: str,
) -> None:
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: None)
    canvas = _solid_design()
    family, font = effects_font
    spec = _typography(lines=["TRAIL DAYS"])
    clean, _, clean_issues = apply_design_effects(
        canvas, spec, font_family=family, font_file=font,
    )
    assert not [issue for issue in clean_issues if issue.severity == "error"]

    def failed_distress(image: Image.Image, level: int, *, seed: str):
        raise OSError("Distress mask could not be rendered")

    monkeypatch.setattr("merch.domain.design_effects.distress_layer", failed_distress)
    preview, metadata, issues = apply_design_effects(
        canvas, spec.model_copy(update={"distress_level": 3}),
        artwork_distress_level=3 if scope == "design" else 0,
        font_family=family, font_file=font,
    )
    assert preview.tobytes() == clean.tobytes()
    assert metadata["distress"]["scope"] == scope
    assert metadata["distress"]["requested_level"] == 3
    assert metadata["distress"]["applied_level"] == 0
    assert metadata["distress"]["removed_fraction"] == 0
    assert metadata["distress"]["error"] == "Distress mask could not be rendered"
    assert [(issue.code, issue.severity) for issue in issues] == [("DISTRESS_PRINTABILITY", "error")]
    assert issues[0].recommended_fix


def test_distress_is_deterministic_and_levels_remove_bounded_increasing_ink() -> None:
    image = _solid_design()
    original_ink = _ink(image)
    removals = []
    for level in range(1, 6):
        distressed, metadata = distress_layer(image, level, seed="repeatable-seed")
        repeated, repeated_metadata = distress_layer(image, level, seed="repeatable-seed")
        assert repeated.tobytes() == distressed.tobytes()
        assert repeated_metadata == metadata
        measured = 1 - _ink(distressed) / original_ink
        assert metadata["removed_fraction"] == pytest.approx(measured, abs=0.001)
        assert metadata["target_fraction"] == pytest.approx(level * 0.02)
        assert 0 < measured <= level * 0.02 + 0.003
        assert metadata["seed"] == "repeatable-seed"
        removals.append(measured)
    assert all(later > earlier for earlier, later in pairwise(removals))
    different, _ = distress_layer(image, 5, seed="different-seed")
    assert different.tobytes() != distressed.tobytes()


def test_distress_protects_thin_strokes_and_small_details() -> None:
    image = _solid_design()
    draw = ImageDraw.Draw(image)
    draw.line((5, 5, 354, 5), width=1, fill="white")
    draw.rectangle((5, 12, 6, 13), fill="white")
    distressed, metadata = distress_layer(image, 5, seed="thin-stroke-seed")
    assert distressed.crop((0, 0, 360, 20)).tobytes() == image.crop((0, 0, 360, 20)).tobytes()
    assert metadata["removed_fraction"] > 0
    thin_only = Image.new("RGBA", (200, 100))
    ImageDraw.Draw(thin_only).line((10, 50, 190, 50), fill="white", width=1)
    protected, metadata = distress_layer(thin_only, 5, seed="thin-only")
    assert protected.tobytes() == thin_only.tobytes()
    assert metadata["applied_level"] < metadata["requested_level"]


@pytest.mark.parametrize("backend", ["pillow", "svg"])
@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("lines", [
    ["Café, trails & starlight — let's wander!"],
    ["Café, trails & starlight", "Let's wander together!"],
])
def test_outlined_unicode_slogans_fit_their_bounds(
    monkeypatch: pytest.MonkeyPatch,
    effects_font: tuple[str, Path],
    backend: str,
    direction: Literal["up", "down"],
    lines: list[str],
) -> None:
    executable = shutil.which("rsvg-convert")
    if backend == "svg" and executable is None:
        pytest.skip("librsvg is installed in the production image")
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: executable if backend == "svg" else None)
    canvas = Image.new("RGBA", (1000, 850))
    spec = _typography(lines=lines, arc=direction)
    family, font = effects_font
    rendered, metadata, issues = apply_design_effects(
        canvas, spec, font_family=family, font_file=font,
    )
    assert not [issue for issue in issues if issue.severity == "error"]
    assert metadata["typography"]["backend"] == backend
    assert metadata["typography"]["applied_arc"] == direction
    bounds = rendered.getchannel("A").getbbox()
    assert bounds is not None
    left, top, right, bottom = bounds
    assert 0 < left < right < canvas.width
    assert 0 < top < bottom < canvas.height
    assert right - left <= canvas.width * spec.relative_width + 1
    assert bottom - top <= canvas.height * spec.relative_height + 1
    assert rendered.mode == "RGBA"


def test_text_distress_preserves_illustration_and_whole_design_distress_is_applied_once(
    monkeypatch: pytest.MonkeyPatch, effects_font: tuple[str, Path],
) -> None:
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: None)
    canvas = Image.new("RGBA", (800, 800))
    ImageDraw.Draw(canvas).rectangle((15, 15, 200, 110), fill="#599163")
    family, font = effects_font
    spec = _typography(lines=["TRAIL DAYS"], distress=5)
    text_distressed, text_metadata, issues = apply_design_effects(
        canvas, spec, font_family=family, font_file=font,
    )
    assert not [issue for issue in issues if issue.severity == "error"]
    assert text_metadata["distress"]["scope"] == "text"
    assert text_distressed.crop((0, 0, 220, 120)).tobytes() == canvas.crop((0, 0, 220, 120)).tobytes()
    clean, _, _ = apply_design_effects(
        canvas, spec.model_copy(update={"distress_level": 0}), font_family=family, font_file=font,
    )
    whole_distressed, metadata, issues = apply_design_effects(
        canvas, spec, artwork_distress_level=2, font_family=family, font_file=font,
    )
    assert not [issue for issue in issues if issue.severity == "error"]
    assert metadata["distress"]["scope"] == "design"
    assert metadata["distress"]["requested_level"] == 2
    assert 0 < 1 - _ink(whole_distressed) / _ink(clean) <= 0.043
    assert whole_distressed.crop((0, 0, 220, 120)).tobytes() != canvas.crop((0, 0, 220, 120)).tobytes()


@pytest.mark.parametrize("with_typography", [False, True])
def test_effected_prepress_output_keeps_transparency_300dpi_and_srgb(
    monkeypatch: pytest.MonkeyPatch, effects_font: tuple[str, Path], with_typography: bool,
) -> None:
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: None)
    family, font = effects_font
    prepared = prepare_artwork(
        make_fixture_art(500, 600), 800, 960,
        typography=_typography(arc="up", distress=3) if with_typography else None,
        artwork_distress_level=2, font_family=family, font_file=font,
    )
    image = Image.open(io.BytesIO(prepared.data))
    assert image.size == (800, 960)
    assert image.mode == "RGBA"
    assert image.getchannel("A").getextrema() == (0, 255)
    assert image.info["dpi"] == pytest.approx((300, 300), abs=0.1)
    profile = ImageCms.ImageCmsProfile(io.BytesIO(image.info["icc_profile"]))
    assert "sRGB" in ImageCms.getProfileDescription(profile)
    assert prepared.effects["distress"]["scope"] == "design"


def test_prepress_png_and_hash_ignore_icc_profile_creation_time(
    monkeypatch: pytest.MonkeyPatch, effects_font: tuple[str, Path],
) -> None:
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: None)
    original_tobytes = ImageCms.ImageCmsProfile.tobytes
    dates = iter([
        struct.pack(">6H", 2026, 9, 18, 9, 30, 0),
        struct.pack(">6H", 2027, 1, 2, 18, 45, 59),
    ])

    def profile_with_changing_date(profile: ImageCms.ImageCmsProfile) -> bytes:
        serialized = bytearray(original_tobytes(profile))
        serialized[24:36] = next(dates)
        return bytes(serialized)

    monkeypatch.setattr(ImageCms.ImageCmsProfile, "tobytes", profile_with_changing_date)
    family, font = effects_font
    source = make_fixture_art(400, 480)
    typography = _typography(arc="up", distress=2)
    prepared = [
        prepare_artwork(
            source, 400, 480, typography=typography, artwork_distress_level=2,
            font_family=family, font_file=font,
        )
        for _ in range(2)
    ]
    assert prepared[0].data == prepared[1].data
    assert prepared[0].sha256 == prepared[1].sha256
    image = Image.open(io.BytesIO(prepared[0].data))
    assert image.info["icc_profile"][24:36] == struct.pack(">6H", 2000, 1, 1, 0, 0, 0)
