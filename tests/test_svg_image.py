"""Offline tests for SVG authoring and rasterisation."""

from __future__ import annotations

from pathlib import Path

import pytest

import svg_image
from svg_image import (
    SvgError,
    intrinsic_size,
    normalise_svg,
    render_scale,
    render_svg_image,
)

FONT_DIR = Path(svg_image.__file__).resolve().parent / "assets" / "fonts"

SIMPLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="240" '
    'viewBox="0 0 400 240">'
    '<rect width="400" height="240" fill="#0b1020"/>'
    '<circle cx="120" cy="120" r="70" fill="#f5b301"/>'
    '<text x="200" y="210" text-anchor="middle" font-size="24" fill="#ffffff">'
    "نمودار فروش 1404"
    "</text>"
    "</svg>"
)


# ---------- normalisation ----------


def test_bundled_fonts_are_present():
    names = {Path(path).name for path in svg_image.font_files()}
    assert {"Vazirmatn-Regular.ttf", "Vazirmatn-Bold.ttf"} <= names
    assert (FONT_DIR / "OFL.txt").is_file()


def test_code_fence_and_prolog_are_stripped():
    wrapped = "```svg\n<?xml version=\"1.0\"?>\n" + SIMPLE_SVG + "\n```"
    cleaned = normalise_svg(wrapped)
    assert cleaned.startswith("<svg")
    assert cleaned.endswith("</svg>")
    assert "```" not in cleaned
    assert "<?xml" not in cleaned


def test_xmlns_is_added_when_missing():
    cleaned = normalise_svg('<svg width="10" height="10"><rect width="10" height="10"/></svg>')
    assert 'xmlns="http://www.w3.org/2000/svg"' in cleaned


def test_surrounding_chatter_is_trimmed():
    cleaned = normalise_svg("Sure! Here is your image:\n" + SIMPLE_SVG + "\nHope you like it.")
    assert cleaned.startswith("<svg")
    assert cleaned.endswith("</svg>")
    assert "Hope" not in cleaned


def test_scripts_and_foreign_objects_are_removed():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        "<script>alert(1)</script>"
        "<foreignObject><div>hi</div></foreignObject>"
        '<rect width="10" height="10"/></svg>'
    )
    cleaned = normalise_svg(svg)
    assert "<script" not in cleaned
    assert "foreignObject" not in cleaned
    assert "<rect" in cleaned


def test_stylesheet_font_family_is_dropped():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        "<style>text { font-family: IRANSans, sans-serif; fill: #fff; }</style>"
        "<text x=\"1\" y=\"5\">سلام</text></svg>"
    )
    cleaned = normalise_svg(svg)
    assert "font-family" not in cleaned
    assert "fill: #fff" in cleaned


def test_empty_stylesheet_is_removed():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        "<style>text { font-family: IRANSans; }</style>"
        "<text x=\"1\" y=\"5\">سلام</text></svg>"
    )
    cleaned = normalise_svg(svg)
    assert "<style" not in cleaned


def test_inline_font_family_is_dropped():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<text x="1" y="5" style="font-family: IRANSans; fill: #fff; font-size: 12px">سلام</text>'
        "</svg>"
    )
    cleaned = normalise_svg(svg)
    assert "font-family" not in cleaned
    assert "fill: #fff" in cleaned


@pytest.mark.parametrize(
    "svg",
    [
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<image href="https://example.com/a.png"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<image xlink:href="//example.com/a.png"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        "<style>@import url('https://example.com/a.css');</style></svg>",
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<rect fill="url(https://example.com/p.svg#g)"/></svg>',
    ],
)
def test_external_resources_are_rejected(svg):
    with pytest.raises(SvgError) as excinfo:
        normalise_svg(svg)
    assert excinfo.value.code == "external_resources"


def test_empty_svg_is_rejected():
    with pytest.raises(SvgError) as excinfo:
        normalise_svg("   ")
    assert excinfo.value.code == "empty_svg"


def test_missing_svg_root_is_rejected():
    with pytest.raises(SvgError) as excinfo:
        normalise_svg("<div>not an image</div>")
    assert excinfo.value.code == "missing_svg_root"


def test_oversized_markup_is_rejected():
    with pytest.raises(SvgError) as excinfo:
        normalise_svg(SIMPLE_SVG, max_chars=50)
    assert excinfo.value.code == "svg_too_large"


# ---------- geometry ----------


def test_intrinsic_size_prefers_attributes():
    assert intrinsic_size(SIMPLE_SVG) == (400.0, 240.0)


def test_intrinsic_size_reads_units():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1in" height="96px"></svg>'
    assert intrinsic_size(svg) == (96.0, 96.0)


def test_intrinsic_size_falls_back_to_viewbox():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180"></svg>'
    assert intrinsic_size(svg) == (320.0, 180.0)


def test_intrinsic_size_ignores_percentages():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%"></svg>'
    assert intrinsic_size(svg) is None


def test_render_scale_enlarges_small_and_caps_large():
    assert render_scale((200.0, 200.0), max_side=1536, min_long_side=768) == (768, 768)
    assert render_scale((4000.0, 2000.0), max_side=1536, min_long_side=768) == (1536, 768)
    assert render_scale((1200.0, 630.0), max_side=1536, min_long_side=768) == (1200, 630)


# ---------- rendering ----------


requires_renderer = pytest.mark.skipif(
    not svg_image.is_available(), reason="resvg-py is not installed"
)


@requires_renderer
def test_render_produces_png():
    image = render_svg_image(SIMPLE_SVG)
    assert image.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert image.mime_type == "image/png"
    assert image.filename.endswith(".png")
    assert image.telegram_safe_photo is True
    # 400x240 is smaller than the readable minimum, so it is scaled up.
    assert (image.width, image.height) == (768, 461)
    assert image.svg.startswith("<svg")


@requires_renderer
def test_render_scales_down_huge_canvases():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="6000" height="3000">'
        '<rect width="6000" height="3000" fill="#123456"/></svg>'
    )
    image = render_svg_image(svg, max_side=1536)
    assert max(image.width, image.height) == 1536


@requires_renderer
def test_render_rejects_missing_dimensions():
    with pytest.raises(SvgError) as excinfo:
        render_svg_image('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
    assert excinfo.value.code == "missing_dimensions"


@requires_renderer
def test_render_reports_invalid_markup_to_the_model():
    with pytest.raises(SvgError) as excinfo:
        render_svg_image(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
            '<rect width="100"</svg>'
        )
    assert excinfo.value.code == "invalid_svg"
    assert excinfo.value.user_message


@requires_renderer
def test_render_honours_output_size_limit():
    with pytest.raises(SvgError) as excinfo:
        render_svg_image(SIMPLE_SVG, max_output_bytes=64)
    assert excinfo.value.code == "output_too_large"


@requires_renderer
def test_bot_wires_the_svg_tool_independently_of_the_photo_model():
    source = (Path(svg_image.__file__).resolve().parent / "bot.py").read_text(encoding="utf-8")
    assert 'name="create_svg_image"' in source
    assert "if SVG_IMAGE_AVAILABLE:" in source
    assert "if pollinations_client.is_configured or SVG_IMAGE_AVAILABLE:" in source
    assert "_svg_image_prompt_section()" in source
    assert "from svg_image import" in source


@requires_renderer
def test_persian_text_is_rendered():
    """A missing Persian font renders an empty canvas, so require visible ink."""
    if not svg_image.font_files():
        pytest.skip("bundled Persian font is missing")
    canvas = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="140">'
        '<rect width="600" height="140" fill="#ffffff"/>'
    )
    blank = render_svg_image(canvas + "</svg>")
    with_text = render_svg_image(
        canvas + '<text x="20" y="90" font-size="48" fill="#000000">سلام دنیا 1404</text></svg>'
    )
    assert with_text.data != blank.data
    assert (with_text.width, with_text.height) == (blank.width, blank.height)
