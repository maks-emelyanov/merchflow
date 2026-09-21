from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from openai.lib._pydantic import to_strict_json_schema
from PIL import Image, ImageDraw
from pydantic import ValidationError

from merch.config import Settings
from merch.defaults import fixture_product_template
from merch.domain.ip_screening import ip_report_eligible, screen_concept, weighted_concept_score
from merch.domain.prepress import (
    deterministic_qa,
    flatten_print_colors,
    largest_generation_size,
    make_fixture_art,
    prepare_artwork,
)
from merch.domain.pricing import quote_price, round_up_to_99
from merch.domain.product_options import exclude_low_contrast_colors, publication_template
from merch.schemas import (
    Channel,
    CreativeBrief,
    Evidence,
    IPMatch,
    IPScreeningReport,
    MarketplaceListingSet,
    NewResearchReport,
    QAIssue,
    QAReport,
    ResearchReport,
    SelectionDecision,
    TypographySpec,
    VariantConfig,
)
from merch.services.credentials import CredentialCipher, redact
from merch.services.openai_service import OpenAIService
from merch.services.storage import ArtifactStorage


@pytest.mark.asyncio
async def test_new_research_has_25_candidates_and_legacy_reports_remain_readable() -> None:
    report = (
        await OpenAIService(Settings()).research(__import__("datetime").date.today(), "none")
    ).value
    assert len(report.candidates) == 25
    legacy = {**report.model_dump(), "candidates": report.candidates[:10]}
    assert len(ResearchReport.model_validate(legacy).candidates) == 10
    with pytest.raises(ValidationError):
        NewResearchReport.model_validate(legacy)
    with pytest.raises(ValidationError):
        ResearchReport.model_validate({**report.model_dump(), "candidates": report.candidates[:9]})


def test_evidence_url_is_validated_without_unsupported_uri_schema() -> None:
    assert "format" not in Evidence.model_json_schema()["properties"]["url"]
    assert Evidence(claim="Example", title="Example", url="https://example.com").url.startswith(
        "https://"
    )
    with pytest.raises(ValidationError):
        Evidence(claim="Invalid", title="Invalid", url="javascript:alert(1)")


def test_openai_structured_schemas_avoid_dynamic_objects_and_uri_format() -> None:
    for schema in (
        ResearchReport,
        NewResearchReport,
        SelectionDecision,
        CreativeBrief,
        TypographySpec,
        IPScreeningReport,
        QAReport,
        MarketplaceListingSet,
    ):
        strict = json.dumps(to_strict_json_schema(schema))
        assert '"format": "uri"' not in strict
        assert '"additionalProperties": {' not in strict
    assert (
        to_strict_json_schema(SelectionDecision)["properties"]["rejected_concepts"]["type"]
        == "array"
    )
    with pytest.raises(ValidationError):
        IPMatch(
            source="web", term="bad", url="javascript:alert(1)", explanation="bad", blocking=True
        )


@pytest.mark.asyncio
async def test_ip_rules_and_ranking() -> None:
    report = (
        await OpenAIService(Settings()).research(__import__("datetime").date.today(), "none")
    ).value
    safe = report.candidates[0]
    blocked = safe.model_copy(
        update={
            "concept_name": "Taylor Swift Eras Tour",
            "scores": safe.scores.model_copy(update={"ip_risk": 0}),
        }
    )
    assert screen_concept(safe).status == "pass"
    assert screen_concept(blocked).status == "block"
    assert weighted_concept_score(safe) > 0
    risky = safe.model_copy(update={"scores": safe.scores.model_copy(update={"ip_risk": 90})})
    assert weighted_concept_score(risky, include_ip_risk=False) == weighted_concept_score(safe)
    assert weighted_concept_score(risky) < weighted_concept_score(safe)


def test_enhanced_ip_risk_and_blocking_matches_override_review_status() -> None:
    report = IPScreeningReport(
        status="review",
        risk_score=48,
        searched_terms=["example"],
        matches=[],
        uspto_search_url="https://tmsearch.uspto.gov/search?query=example",
        notes=[],
    )
    assert not ip_report_eligible(report, threshold=20)
    assert ip_report_eligible(report.model_copy(update={"risk_score": 20}), threshold=20)
    blocking = IPMatch(
        source="marketplace",
        term="example",
        explanation="Existing near-identical shirt",
        blocking=True,
    )
    assert not ip_report_eligible(
        report.model_copy(update={"risk_score": 5, "matches": [blocking]}), threshold=20
    )


def test_pricing_formula_and_99_rounding() -> None:
    assert round_up_to_99(1999) == 1999
    assert round_up_to_99(2000) == 2099
    quote = quote_price(
        channel=Channel.ETSY,
        variant_id=1,
        production_cost_cents=1000,
        percent_fee=0.10,
        fixed_fee_cents=45,
    )
    assert quote.retail_price_cents == 2099
    assert quote.retail_price_cents % 100 == 99
    assert quote.estimated_margin >= 0.40


def test_blank_optional_environment_values_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERCH_ETSY_SHOP_ID", "")
    settings = Settings(_env_file=None)
    assert settings.etsy_shop_id is None


def test_typography_preserves_exact_text() -> None:
    values = {
        "exact_text": "TAKE THE SCENIC ROUTE",
        "line_breaks": ["TAKE THE", "SCENIC ROUTE"],
        "letter_spacing": 0,
        "line_spacing": 1,
        "text_alignment": "center",
        "text_arc_or_shape": "none",
        "outline": None,
        "shadow": None,
        "distress_level": 0,
        "primary_color": "#FFFFFF",
        "secondary_color": None,
        "interaction_with_illustration": "below",
        "relative_width": 0.8,
        "relative_height": 0.2,
    }
    typography = TypographySpec.model_validate(values)
    assert typography.exact_text == "TAKE THE SCENIC ROUTE"
    assert typography.vertical_placement == "center"
    with pytest.raises(ValidationError):
        TypographySpec.model_validate({**values, "line_breaks": ["TAKE A SHORTCUT"]})
    with pytest.raises(ValidationError):
        TypographySpec.model_validate({**values, "line_breaks": []})
    with pytest.raises(ValidationError):
        TypographySpec.model_validate({**values, "line_breaks": ["TAKE THE", " "]})


def test_typography_normalizes_opaque_colors_and_drops_invalid_optional_colors() -> None:
    values = {
        "exact_text": "KILN WEATHER BUREAU",
        "line_breaks": ["KILN WEATHER BUREAU"],
        "letter_spacing": 0,
        "line_spacing": 1,
        "text_alignment": "center",
        "text_arc_or_shape": "none",
        "outline": "navy",
        "shadow": "#123456FF",
        "distress_level": 0,
        "primary_color": "#f4e6cc on dark garments; #1a1f35 on light garments",
        "secondary_color": "transparent",
        "interaction_with_illustration": "below",
        "relative_width": 0.8,
        "relative_height": 0.2,
    }
    typography = TypographySpec.model_validate(values)
    assert typography.primary_color == "#F4E6CC"
    assert typography.outline == "#000080"
    assert typography.shadow == "#123456"
    assert typography.secondary_color is None

    optional_invalid = TypographySpec.model_validate(
        {**values, "outline": "not a color", "shadow": "#12345680"}
    )
    assert optional_invalid.outline is None
    assert optional_invalid.shadow is None
    with pytest.raises(ValidationError, match="concrete CSS color"):
        TypographySpec.model_validate({**values, "primary_color": "not a color"})
    with pytest.raises(ValidationError, match="fully opaque"):
        TypographySpec.model_validate({**values, "primary_color": "#12345680"})


def test_creative_brief_normalizes_blank_slogan_lines_before_approval() -> None:
    values = {
        "concept_name": "Kiln Weather Bureau",
        "target_customer": "Ceramicists",
        "customer_motivation": "Studio humor",
        "slogan": "  KILN WEATHER BUREAU\r\n\r\n HEAT ADVISORY IN EFFECT  ",
        "design_mode": "hybrid",
        "visual_concept": "A simple kiln gauge",
        "composition": "Gauge above exact text",
        "graphic_style": "flat vintage utility graphic",
        "palette": ["#F4E6CC", "#1A1F35"],
        "shirt_colors": ["Black"],
        "typography_style": "bold slab",
        "generation_brief": "Draw only one isolated kiln gauge.",
    }
    brief = CreativeBrief.model_validate(values)
    assert brief.slogan == "KILN WEATHER BUREAU\nHEAT ADVISORY IN EFFECT"
    assert CreativeBrief.model_validate({**values, "slogan": " \r\n "}).slogan is None


def test_prepress_dimensions_alpha_profile_and_upscale_warning(tmp_path: Path) -> None:
    prepared = prepare_artwork(
        make_fixture_art(80, 100),
        400,
        500,
        font_file=tmp_path / "missing.ttf",
    )
    image = Image.open(io.BytesIO(prepared.data))
    assert image.size == (400, 500)
    assert image.mode == "RGBA"
    assert image.info["dpi"][0] == pytest.approx(300, abs=0.1)
    assert image.info.get("icc_profile")
    bounds = image.getchannel("A").getbbox()
    assert bounds is not None
    assert (bounds[2] - bounds[0]) / image.width >= 0.6
    assert prepared.source_scale > 1.5
    report = deterministic_qa(
        prepared.data,
        expected_width=400,
        expected_height=500,
        shirt_colors=["#111827"],
        revision=1,
        max_bytes=5_000_000,
        source_scale=5,
        used_realesrgan=False,
    )
    assert report.has_alpha
    assert any(item.code == "upscale_fallback" for item in report.issues)


def test_commercial_scale_qa_rejects_a_tiny_horizontal_word_strip() -> None:
    image = Image.new("RGBA", (400, 500), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((24, 237, 375, 262), fill=(255, 255, 255, 255))
    output = io.BytesIO()
    image.save(output, "PNG")

    report = deterministic_qa(
        output.getvalue(),
        expected_width=400,
        expected_height=500,
        shirt_colors=["#000000"],
        revision=1,
        max_bytes=5_000_000,
        enforce_composition_scale=True,
    )

    finding = next(item for item in report.issues if item.code == "COMPOSITION_SCALE")
    assert finding.severity == "error"
    assert "88.0% wide by 5.2% tall" in finding.message
    assert not report.passed


def test_commercial_scale_qa_accepts_a_confident_composition_footprint() -> None:
    image = Image.new("RGBA", (400, 500), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((40, 100, 359, 399), fill=(255, 255, 255, 255))
    output = io.BytesIO()
    image.save(output, "PNG")

    report = deterministic_qa(
        output.getvalue(),
        expected_width=400,
        expected_height=500,
        shirt_colors=["#000000"],
        revision=1,
        max_bytes=5_000_000,
        enforce_composition_scale=True,
    )

    assert not any(item.code == "COMPOSITION_SCALE" for item in report.issues)
    assert report.passed


def test_flat_print_cleanup_removes_translucent_debris_and_gradients() -> None:
    image = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    image.putpixel((0, 0), (255, 255, 255, 40))
    ImageDraw.Draw(image).rectangle((5, 5, 14, 14), fill=(244, 147, 74, 220))
    cleaned = flatten_print_colors(image, ["Burnt orange #C7662D", "Cream #FFF0D6"])
    assert cleaned.getpixel((0, 0))[3] == 0
    assert cleaned.getpixel((10, 10))[3] == 255
    assert cleaned.getpixel((10, 10))[:3] in {(199, 102, 45), (255, 240, 214)}


def test_flat_print_cleanup_fills_small_palette_flecks() -> None:
    image = Image.new("RGBA", (100, 100), (199, 102, 45, 255))
    ImageDraw.Draw(image).rectangle((48, 48, 52, 52), fill=(255, 240, 214, 255))
    cleaned = flatten_print_colors(image, ["#C7662D", "#FFF0D6"])
    assert cleaned.getpixel((50, 50)) == (199, 102, 45, 255)


def test_contrast_checks_artwork_pixels_not_mean_color() -> None:
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 255))
    ImageDraw.Draw(image).rectangle((50, 0, 99, 99), fill=(255, 255, 255, 255))
    output = io.BytesIO()
    image.save(output, "PNG")
    report = deterministic_qa(
        output.getvalue(),
        expected_width=100,
        expected_height=100,
        shirt_colors=["#808080"],
        revision=1,
        max_bytes=5_000_000,
    )
    assert not any(item.code == "contrast" for item in report.issues)


def test_contrast_qa_excludes_only_the_affected_product_color() -> None:
    base = fixture_product_template()
    template = base.model_copy(
        update={
            "featured_variant_id": 1001,
            "variants": [
                base.variants[0].model_copy(update={"color": "Black", "color_hex": "#000000"}),
                base.variants[1].model_copy(update={"color": "Forest", "color_hex": "#808080"}),
            ],
        }
    )
    image = Image.new("RGBA", (100, 100), (128, 128, 128, 255))
    output = io.BytesIO()
    image.save(output, "PNG")
    report = deterministic_qa(
        output.getvalue(),
        expected_width=100,
        expected_height=100,
        shirt_colors=template.qa_shirt_colors(),
        revision=1,
        max_bytes=5_000_000,
    )
    assert [item.affected_shirt_colors for item in report.issues if item.code == "contrast"] == [
        ["#808080"]
    ]
    accepted, excluded = exclude_low_contrast_colors(report, template)
    assert accepted.passed and excluded == ["Forest"]
    assert any(item.code == "contrast" and item.severity == "warning" for item in accepted.issues)
    product = publication_template(template, excluded)
    assert [item.variant_id for item in product.variants if item.enabled] == [1001]
    assert template.variants[1].enabled  # The shared catalog is unchanged.


def test_visual_contrast_exclusion_keeps_other_defects_blocking() -> None:
    base = fixture_product_template()
    template = base.model_copy(
        update={
            "featured_variant_id": 1002,
            "variants": [
                base.variants[0].model_copy(update={"color": "Black"}),
                base.variants[1].model_copy(update={"color": "Forest"}),
            ],
        }
    )
    report = QAReport(
        passed=False,
        revision=1,
        issues=[
            QAIssue(
                code="GARMENT_CONTRAST",
                severity="error",
                message="Artwork disappears on Forest",
                affected_shirt_colors=["Forest"],
            ),
            QAIssue(code="stray_pixels", severity="error", message="Visible specks"),
        ],
        width=100,
        height=100,
        has_alpha=True,
        color_profile="sRGB",
    )
    accepted, excluded = exclude_low_contrast_colors(report, template)
    assert not accepted.passed and excluded == ["Forest"]
    assert accepted.issues[1].severity == "error"
    assert publication_template(template, excluded).featured_variant().color == "Black"
    all_contrast = report.model_copy(
        update={
            "issues": [
                report.issues[0],
                QAIssue(
                    code="GARMENT_CONTRAST",
                    severity="error",
                    message="Artwork disappears on Black",
                    affected_shirt_colors=["Black"],
                ),
            ]
        }
    )
    still_failed, none_excluded = exclude_low_contrast_colors(all_contrast, template)
    assert not still_failed.passed and not none_excluded


def test_generation_size_respects_sunburst_edge_limits() -> None:
    width, height = largest_generation_size(3692, 4800)
    assert width % 16 == height % 16 == 0
    assert min(width, height) <= 2160
    assert max(width, height) <= 3840
    assert width * height <= 8_294_400


def test_all_enabled_printify_swatch_colors_are_sent_to_qa() -> None:
    base = fixture_product_template()
    variants = [
        VariantConfig(
            variant_id=2000 + index,
            title=f"Color {index} / M",
            color=f"Color {index}",
            color_hex=f"#{index:06X}",
            size="M",
            production_cost_cents=1175,
            enabled=index != 13,
        )
        for index in range(14)
    ]
    variants.append(variants[0].model_copy(update={"variant_id": 3000, "size": "L"}))
    template = base.model_copy(update={"variants": variants})
    assert len(template.qa_shirt_colors()) == 13
    assert "#000000" in template.qa_shirt_colors()
    assert "#00000D" not in template.qa_shirt_colors()
    with pytest.raises(ValidationError):
        VariantConfig.model_validate({**variants[0].model_dump(), "color_hex": "not-a-hex-color"})


def test_artifact_hashing_and_secret_protection(tmp_path: Path) -> None:
    settings = Settings(local_storage_path=tmp_path)
    storage = ArtifactStorage(settings)
    storage.ensure_bucket()
    key1, digest1 = storage.put(b"immutable", suffix="bin", content_type="application/octet-stream")
    key2, digest2 = storage.put(b"immutable", suffix="bin", content_type="application/octet-stream")
    assert (key1, digest1) == (key2, digest2)
    assert storage.get(key1) == b"immutable"
    cipher = CredentialCipher("master-key-for-test")
    encrypted = cipher.encrypt("refresh-token")
    assert encrypted != "refresh-token"
    assert cipher.decrypt(encrypted) == "refresh-token"
    assert redact("token=refresh-token", ["refresh-token"]) == "token=[REDACTED]"
