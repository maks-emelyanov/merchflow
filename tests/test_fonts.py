from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from merch.domain import fonts
from merch.domain.design_effects import apply_design_effects
from merch.domain.fonts import FONT_FAMILIES, ResolvedFont, resolve_font
from merch.domain.prepress import make_fixture_art, prepare_artwork
from merch.schemas import TypographySpec


def _spec(**updates: object) -> TypographySpec:
    return TypographySpec.model_validate({
        "exact_text": "Café Society", "line_breaks": ["Café Society"],
        "font_category": "sans", "font_weight": 700,
        "letter_spacing": 0, "line_spacing": 1.2, "text_alignment": "center",
        "text_arc_or_shape": "none", "outline": None, "shadow": None,
        "distress_level": 0, "primary_color": "#D97148", "secondary_color": None,
        "interaction_with_illustration": "centered lettering",
        "relative_width": 0.85, "relative_height": 0.7,
        **updates,
    })


@pytest.fixture
def custom_font() -> tuple[str, Path]:
    for family, path in (
        ("DejaVu Sans", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        ("Noto Sans", fonts.DEFAULT_FONT_FILE),
    ):
        if path.is_file():
            return family, path
    pytest.skip("Install the documented production fonts to test rendering")


@pytest.fixture
def registry_fonts() -> dict[str, ResolvedFont]:
    fonts._registry_font.cache_clear()
    try:
        return {category: resolve_font(category, 700) for category in FONT_FAMILIES}
    except (OSError, ValueError) as exc:
        if os.environ.get("CI"):
            pytest.fail(f"Production fonts must be installed in CI: {exc}")
        pytest.skip(f"Install the documented production fonts: {exc}")
    finally:
        fonts._registry_font.cache_clear()


@pytest.mark.parametrize("requested,applied", [(100, 400), (400, 400), (549, 400), (550, 700), (900, 700)])
def test_registry_resolves_supported_weight(
    monkeypatch: pytest.MonkeyPatch, requested: int, applied: int,
) -> None:
    def registry(category: str, weight: int) -> ResolvedFont:
        assert category == "slab"
        return ResolvedFont("Roboto Slab", Path("/fonts/slab.otf"), weight, "registry")

    monkeypatch.setattr(fonts, "_registry_font", registry)
    assert resolve_font("slab", requested).weight == applied


def test_registry_rejects_fontconfig_substitution(
    monkeypatch: pytest.MonkeyPatch, custom_font: tuple[str, Path],
) -> None:
    _, file = custom_font
    fonts._registry_font.cache_clear()
    monkeypatch.setattr(fonts, "_fontconfig_match", lambda family, weight: (["Wrong family"], file))
    with pytest.raises(ValueError, match="Required font Roboto Slab is not installed"):
        resolve_font("slab", 700)


def test_custom_font_overrides_category_and_requested_weight(
    monkeypatch: pytest.MonkeyPatch, custom_font: tuple[str, Path],
) -> None:
    family, file = custom_font
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: None)
    spec = _spec(font_category="slab", font_weight=400)
    result = prepare_artwork(
        make_fixture_art(300, 360), 300, 360,
        typography=spec, font_family=family, font_file=file,
    )
    metadata = result.effects["typography"]
    assert not result.issues
    assert metadata["font_family"] == family
    assert metadata["font_source"] == "custom"
    assert metadata["requested_font_category"] == "slab"
    assert metadata["requested_font_weight"] == 400
    assert metadata["applied_font_weight"] == 700
    assert metadata["font_hash"] == hashlib.sha256(file.read_bytes()).hexdigest()


def test_default_prepress_settings_select_the_requested_category(
    monkeypatch: pytest.MonkeyPatch, custom_font: tuple[str, Path],
) -> None:
    family, file = custom_font
    seen = []

    def registry(category: str, weight: int) -> ResolvedFont:
        seen.append((category, weight))
        return ResolvedFont(family, file, weight, "registry")

    monkeypatch.setattr(fonts, "_registry_font", registry)
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: None)
    result = prepare_artwork(
        make_fixture_art(300, 360), 300, 360, typography=_spec(font_category="display"),
        font_family=fonts.DEFAULT_FONT_FAMILY, font_file=fonts.DEFAULT_FONT_FILE,
    )
    assert seen == [("display", 700)]
    assert result.effects["typography"]["font_source"] == "registry"
    assert not result.issues


def test_missing_font_preserves_reviewable_artwork_and_fails_qa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(category: str, weight: int) -> ResolvedFont:
        raise ValueError("Required font Roboto Slab is not installed")

    monkeypatch.setattr(fonts, "_registry_font", unavailable)
    canvas = Image.new("RGBA", (300, 360), "#A56B40")
    image, metadata, issues = apply_design_effects(canvas, _spec(font_category="slab"))
    assert image.tobytes() == canvas.tobytes()
    assert metadata["typography"]["font_family"] is None
    assert "Roboto Slab" in metadata["typography"]["error"]
    assert [(issue.code, issue.severity) for issue in issues] == [("TYPOGRAPHY_LAYOUT", "error")]


def test_unregistered_custom_font_uses_the_exact_file_with_pillow(
    monkeypatch: pytest.MonkeyPatch, custom_font: tuple[str, Path],
) -> None:
    _, file = custom_font
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: "/unused/rsvg-convert")
    monkeypatch.setattr(fonts, "_fontconfig_match", lambda family, weight: (["Different"], file))
    image, metadata, issues = apply_design_effects(
        Image.new("RGBA", (300, 360)), _spec(), font_family="Private font", font_file=file,
    )
    assert not issues
    assert image.getchannel("A").getbbox()
    assert metadata["typography"]["backend"] == "pillow"
    assert metadata["typography"]["font_hash"] == hashlib.sha256(file.read_bytes()).hexdigest()


def test_svg_requests_the_weight_of_the_actual_font_file(
    monkeypatch: pytest.MonkeyPatch, custom_font: tuple[str, Path],
) -> None:
    family, file = custom_font
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: "/mock/rsvg-convert")
    monkeypatch.setattr("merch.domain.design_effects.svg_uses_font", lambda _: True)
    buffer = io.BytesIO()
    image = Image.new("RGBA", (240, 100))
    image.paste("#D97148", (10, 10, 230, 90))
    image.save(buffer, "PNG")
    svg_inputs = []

    def render(args, *, input, capture_output, check, timeout):
        svg_inputs.append(input.decode())
        return subprocess.CompletedProcess(args, 0, buffer.getvalue(), b"")

    monkeypatch.setattr("merch.domain.design_effects.subprocess.run", render)
    _, metadata, issues = apply_design_effects(
        Image.new("RGBA", (300, 360)), _spec(font_weight=400),
        font_family=family, font_file=file,
    )
    assert not issues
    assert len(svg_inputs) == 1
    assert 'font-weight="700"' in svg_inputs[0]
    assert metadata["typography"]["requested_font_weight"] == 400
    assert metadata["typography"]["applied_font_weight"] == 700
    assert metadata["typography"]["backend"] == "svg"


@pytest.mark.parametrize("backend", ["pillow", "svg"])
def test_categories_render_distinct_repeatable_faces(
    monkeypatch: pytest.MonkeyPatch, registry_fonts: dict[str, ResolvedFont], backend: str,
) -> None:
    rsvg = shutil.which("rsvg-convert")
    if backend == "svg" and not rsvg:
        if os.environ.get("CI"):
            pytest.fail("librsvg must be installed in CI")
        pytest.skip("Install librsvg to test SVG shaping")
    monkeypatch.setattr("merch.domain.design_effects.shutil.which", lambda _: rsvg if backend == "svg" else None)
    hashes = set()
    for category, font in registry_fonts.items():
        spec = _spec(font_category=category, text_arc_or_shape="up")
        first = prepare_artwork(make_fixture_art(300, 360), 600, 720, typography=spec)
        second = prepare_artwork(make_fixture_art(300, 360), 600, 720, typography=spec)
        assert not first.issues
        assert first.data == second.data
        metadata = first.effects["typography"]
        assert metadata["font_family"] == font.family
        assert metadata["backend"] == backend
        assert metadata["applied_font_weight"] == 700
        assert metadata["applied_arc"] == "up"
        assert metadata["font_hash"] == hashlib.sha256(font.file.read_bytes()).hexdigest()
        assert Image.open(io.BytesIO(first.data)).getchannel("A").getextrema() == (0, 255)
        hashes.add(first.sha256)
    assert len(hashes) == len(FONT_FAMILIES)


def test_registry_regular_and_bold_are_different_files(
    registry_fonts: dict[str, ResolvedFont],
) -> None:
    for category, bold in registry_fonts.items():
        regular = resolve_font(category, 400)
        assert regular.weight == 400
        assert regular.file != bold.file
        assert regular.file.read_bytes() != bold.file.read_bytes()
