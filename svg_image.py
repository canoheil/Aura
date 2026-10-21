"""SVG authoring for Aura: validate model-written SVG and rasterise it to PNG.

Gemini writes the SVG markup; this module owns everything that must not be
trusted to the model: markup clean-up, a small safety pass, dimension handling,
and rasterisation. Rendering happens entirely offline through ``resvg-py``
(the Rust ``resvg`` engine), so this path never calls an external service and
never consumes an image-generation quota.

The module deliberately has no Telegram or Gemini imports so it can be
unit-tested offline and reused from the tool runtime and the ``/image`` paths.

Notes on text:
- ``resvg`` performs Arabic/Persian shaping and the bidi algorithm itself, so
  SVG text must stay logical (never pre-reversed or pre-shaped).
- The bundled Vazirmatn font is registered with the renderer and applied to
  every ``<text>``/``<tspan>`` through a stylesheet, because a bare server has
  no Persian font installed and would otherwise render empty text.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from pathlib import Path
import re
import secrets
import time
from typing import Optional

logger = logging.getLogger("aura-svg")

try:  # Optional runtime dependency: Aura degrades gracefully without it.
    import resvg_py
except Exception as exc:  # pragma: no cover - depends on the host
    resvg_py = None
    _RESVG_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _RESVG_IMPORT_ERROR = ""

DEFAULT_MAX_SVG_CHARS = 200_000
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_SIDE = 1536
DEFAULT_MIN_LONG_SIDE = 768
DEFAULT_BACKGROUND = ""
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024

_FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
_FONT_FILENAMES = ("Vazirmatn-Regular.ttf", "Vazirmatn-Bold.ttf")
# A stylesheet beats presentation attributes, so Persian text renders even when
# the model asks for a font the server does not have.
TEXT_STYLE_SHEET = "text, tspan { font-family: 'Vazirmatn', 'DejaVu Sans', sans-serif; }"

_GENERIC_ERROR = "❌ ساخت تصویر SVG انجام نشد. دوباره امتحان کن."

_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)"
_UNIT_SCALE = {"px": 1.0, "pt": 96.0 / 72.0, "pc": 16.0, "mm": 96.0 / 25.4, "cm": 96.0 / 2.54, "in": 96.0}
_WIDTH_ATTR = re.compile(
    rf"\bwidth\s*=\s*[\"']\s*({_NUM})\s*(px|pt|pc|mm|cm|in)?\s*[\"']",
    re.IGNORECASE,
)
_HEIGHT_ATTR = re.compile(
    rf"\bheight\s*=\s*[\"']\s*({_NUM})\s*(px|pt|pc|mm|cm|in)?\s*[\"']",
    re.IGNORECASE,
)
_VIEWBOX = re.compile(
    rf"\bviewBox\s*=\s*[\"']\s*({_NUM})[\s,]+({_NUM})[\s,]+({_NUM})[\s,]+({_NUM})\s*[\"']",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", re.MULTILINE)
_XML_PROLOG = re.compile(r"<\?xml[^>]*\?>", re.IGNORECASE)
_DOCTYPE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)
_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_SCRIPT_SELF_CLOSING = re.compile(r"<script\b[^>]*/>", re.IGNORECASE)
_FOREIGN_OBJECT = re.compile(
    r"<foreignObject\b[^>]*>.*?</foreignObject\s*>", re.IGNORECASE | re.DOTALL
)
_STYLE_ATTR = re.compile(r"(style\s*=\s*)(\"[^\"]*\"|'[^']*')", re.IGNORECASE)
_INLINE_FONT_FAMILY = re.compile(r"font-family\s*:[^;\"']*;?", re.IGNORECASE)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL)
_CSS_FONT_FAMILY = re.compile(r"font-family\s*:[^;}]*;?", re.IGNORECASE)
_EMPTY_CSS_RULE = re.compile(r"\{[\s;]*\}")
_CSS_RULE_WITH_BODY = re.compile(r"\{[^}]*\S[^}]*\}")
_EXTERNAL_REF = re.compile(
    r"(?:href|src|xlink:href)\s*=\s*[\"']\s*(?:https?:)?//"
    r"|@import\b"
    r"|url\(\s*[\"']?\s*(?:https?:)?//",
    re.IGNORECASE,
)
_SVG_ROOT = re.compile(r"<svg\b", re.IGNORECASE)
_SVG_CLOSE = re.compile(r"</svg\s*>", re.IGNORECASE)


class SvgError(Exception):
    """A safe, classified SVG authoring failure.

    ``message`` is technical and safe to log. ``user_message`` is the short
    Persian text a caller can show to the Telegram user; the tool handler
    instead forwards the technical detail so the model can repair its markup.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        user_message: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.user_message = user_message or _GENERIC_ERROR


@dataclass(frozen=True)
class SvgImage:
    """One rasterised SVG, ready for Telegram delivery.

    It intentionally mirrors the attributes of ``pollinations_image.GeneratedImage``
    so the existing image delivery path can send either kind without branching.
    """

    data: bytes
    svg: str
    width: int
    height: int
    mime_type: str = "image/png"

    @property
    def extension(self) -> str:
        return "png"

    @property
    def filename(self) -> str:
        return f"aura-svg-{int(time.time())}-{secrets.token_hex(3)}.png"

    @property
    def telegram_safe_photo(self) -> bool:
        """Telegram sendPhoto accepts PNG under 10 MB."""
        return len(self.data) <= TELEGRAM_PHOTO_MAX_BYTES


@lru_cache(maxsize=1)
def font_files() -> tuple[str, ...]:
    """Absolute paths of the bundled fonts that actually exist on disk."""
    return tuple(
        str(path)
        for path in (_FONT_DIR / name for name in _FONT_FILENAMES)
        if path.is_file()
    )


def is_available() -> bool:
    """True when SVG authoring can actually render on this host."""
    return resvg_py is not None


def unavailable_reason() -> str:
    """Short diagnostic used in logs when the feature is disabled."""
    return _RESVG_IMPORT_ERROR or "resvg-py is not installed"


def _strip_inline_font_family(match: re.Match) -> str:
    """Drop ``font-family`` from inline styles so the bundled font always wins."""
    prefix, value = match.group(1), match.group(2)
    quote = value[0]
    cleaned = _INLINE_FONT_FAMILY.sub("", value[1:-1]).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip().strip(";").strip()
    if not cleaned:
        return ""
    return f"{prefix}{quote}{cleaned}{quote}"


def _clean_style_block(block: str) -> str:
    """Remove ``font-family`` from an embedded stylesheet.

    An author stylesheet beats the stylesheet injected with the bundled font,
    so a model-chosen family would render Persian text as blank boxes. Rules
    left without any declaration are dropped, and a stylesheet with no rules
    left is removed entirely.
    """
    open_tag = re.match(r"<style\b[^>]*>", block, re.IGNORECASE)
    body = block[open_tag.end():] if open_tag else block
    body = _CSS_FONT_FAMILY.sub("", body)
    body = _EMPTY_CSS_RULE.sub("", body)
    if not _CSS_RULE_WITH_BODY.search(body):
        return ""
    return (open_tag.group(0) if open_tag else "<style>") + body


def normalise_svg(source: object, *, max_chars: int = DEFAULT_MAX_SVG_CHARS) -> str:
    """Return clean, standalone SVG markup or raise :class:`SvgError`.

    The model may wrap markup in a code fence, keep an XML prolog, or add
    scripts and external references. All of that is normalised here so the
    renderer only ever sees a self-contained document.
    """
    text = str(source or "").strip()
    if not text:
        raise SvgError(
            "The SVG markup is empty.",
            code="empty_svg",
            user_message="❌ کد SVG خالی بود؛ تصویری ساخته نشد.",
        )
    if len(text) > max_chars:
        raise SvgError(
            f"SVG markup is {len(text)} characters, above the {max_chars} limit.",
            code="svg_too_large",
            user_message="❌ کد SVG بیش از حد بزرگ بود؛ تصویری ساخته نشد.",
        )

    text = _CODE_FENCE.sub("", text).strip()
    text = _XML_PROLOG.sub("", text).strip()
    text = _DOCTYPE.sub("", text).strip()

    start = _SVG_ROOT.search(text)
    if start is None:
        raise SvgError(
            "The markup has no <svg> root element.",
            code="missing_svg_root",
            user_message="❌ کد ارسالی یک تصویر SVG معتبر نبود.",
        )
    closes = list(_SVG_CLOSE.finditer(text))
    end = closes[-1].end() if closes else len(text)
    svg = text[start.start():end].strip()

    svg = _SCRIPT_BLOCK.sub("", svg)
    svg = _SCRIPT_SELF_CLOSING.sub("", svg)
    svg = _FOREIGN_OBJECT.sub("", svg)

    # Checked before any CSS clean-up so a remote reference is always reported
    # instead of being silently dropped along with its stylesheet.
    if _EXTERNAL_REF.search(svg):
        raise SvgError(
            "The SVG references external or remote resources.",
            code="external_resources",
            user_message="❌ این SVG به منابع بیرونی وابسته بود؛ تصویری ساخته نشد.",
        )

    svg = _STYLE_ATTR.sub(_strip_inline_font_family, svg)
    svg = _STYLE_BLOCK.sub(lambda match: _clean_style_block(match.group(0)), svg)

    if not re.search(r"\sxmlns\s*=", svg, re.IGNORECASE):
        svg = _SVG_ROOT.sub('<svg xmlns="http://www.w3.org/2000/svg"', svg, count=1)

    return svg


def intrinsic_size(svg: str) -> Optional[tuple[float, float]]:
    """Return the declared (width, height) of the SVG, in CSS pixels.

    ``width``/``height`` win; ``viewBox`` is the fallback. Percentages are
    ignored because they carry no intrinsic size.
    """
    width = _attribute_px(_WIDTH_ATTR, svg)
    height = _attribute_px(_HEIGHT_ATTR, svg)
    if width and height:
        return width, height

    viewbox = _VIEWBOX.search(svg)
    if viewbox:
        try:
            box_width = abs(float(viewbox.group(3)))
            box_height = abs(float(viewbox.group(4)))
        except ValueError:
            box_width = box_height = 0.0
        if box_width and box_height:
            return box_width, box_height

    if width or height:
        side = width or height
        return side, side  # type: ignore[return-value]
    return None


def _attribute_px(pattern: re.Pattern, svg: str) -> Optional[float]:
    match = pattern.search(svg)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    unit = (match.group(2) or "px").lower()
    return value * _UNIT_SCALE.get(unit, 1.0)


def render_scale(
    size: tuple[float, float],
    *,
    max_side: int = DEFAULT_MAX_SIDE,
    min_long_side: int = DEFAULT_MIN_LONG_SIDE,
) -> tuple[int, int]:
    """Pick PNG dimensions: enlarge tiny drawings, cap huge ones."""
    width, height = size
    long_side = max(width, height)
    if long_side <= 0:
        raise SvgError("The SVG has a zero-sized canvas.", code="zero_size")
    scale = 1.0
    if long_side < min_long_side:
        scale = min_long_side / long_side
    if long_side * scale > max_side:
        scale = max_side / long_side
    return max(1, round(width * scale)), max(1, round(height * scale))


def render_svg_image(
    source: object,
    *,
    max_side: int = DEFAULT_MAX_SIDE,
    min_long_side: int = DEFAULT_MIN_LONG_SIDE,
    background: str = DEFAULT_BACKGROUND,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_svg_chars: int = DEFAULT_MAX_SVG_CHARS,
) -> SvgImage:
    """Rasterise model-written SVG markup to a Telegram-ready PNG."""
    if resvg_py is None:
        raise SvgError(
            f"SVG rendering is unavailable ({unavailable_reason()}).",
            code="renderer_unavailable",
            user_message="❌ موتور ساخت تصویر SVG روی Aura فعال نیست.",
        )

    svg = normalise_svg(source, max_chars=max_svg_chars)
    size = intrinsic_size(svg)
    if size is None:
        raise SvgError(
            "The SVG declares no usable width/height or viewBox.",
            code="missing_dimensions",
            user_message="❌ اندازه‌ی تصویر SVG مشخص نبود؛ تصویری ساخته نشد.",
        )
    width, height = render_scale(size, max_side=max_side, min_long_side=min_long_side)

    fonts = list(font_files())
    if not fonts:
        logger.warning("Bundled SVG fonts are missing; text may not render")

    try:
        png = resvg_py.svg_to_bytes(
            svg_string=svg,
            width=width,
            height=height,
            background=(background or None),
            font_files=fonts or None,
            style_sheet=TEXT_STYLE_SHEET,
        )
    except ValueError as exc:
        detail = re.sub(r"\s+", " ", str(exc)).strip()[:300]
        raise SvgError(
            f"resvg rejected the SVG: {detail}",
            code="invalid_svg",
            user_message="❌ این SVG در موتور رندر خطا داد؛ تصویری ساخته نشد.",
        ) from exc
    except Exception as exc:  # pragma: no cover - unexpected native failure
        logger.exception("Unexpected SVG rendering failure")
        raise SvgError(
            f"SVG rendering failed ({type(exc).__name__}).",
            code="render_failed",
            user_message="❌ رندر تصویر SVG ناموفق بود.",
        ) from exc

    data = bytes(png or b"")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SvgError(
            "resvg did not return PNG data.",
            code="bad_output",
            user_message="❌ خروجی رندر معتبر نبود.",
        )
    if len(data) > max_output_bytes:
        raise SvgError(
            f"Rendered PNG is {len(data)} bytes, above the {max_output_bytes} limit.",
            code="output_too_large",
            user_message="❌ تصویر تولیدشده بیش از حد بزرگ بود.",
        )

    logger.info(
        "SVG rendered | width=%s height=%s bytes=%s svg_chars=%s",
        width, height, len(data), len(svg),
    )
    return SvgImage(data=data, svg=svg, width=width, height=height)
