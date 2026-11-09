"""A weather card Aura can draw next to her answer.

The Telegram clients Aura talks to have no weather widget: a bot can send text,
a photo, a document, a sticker. So the closest honest thing to the weather card
in Telegram's own promo material is a PNG Aura renders herself, with no image
service, no quota and no network:

* :func:`draw_weather_card` paints one card (current conditions and the next
  hours) as RGBA bytes, with a small hand-written rasteriser on top of
  ``resvg-py`` — the same engine the SVG path already uses,
* :func:`render_weather_card` is the Telegram-facing wrapper: it sizes the PNG
  for a phone screen, encodes it, and reports its own dimensions,
* :class:`WeatherCard` mirrors ``SvgImage``/``GeneratedImage`` so the existing
  image delivery path can send it without branching.

Everything here is offline and storage-free: a report goes in, bytes come out.
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import weather_api
from svg_image import SvgError, render_svg_image

try:  # Same optional dependency as the SVG renderer.
    import resvg_py
except Exception as exc:  # pragma: no cover - depends on the host
    resvg_py = None
    _RESVG_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _RESVG_IMPORT_ERROR = ""

logger = logging.getLogger("aura-weather-card")

# The card is a phone-width picture: 2x the width of the bubble it lands in.
CARD_WIDTH = 720
CARD_HEIGHT = 470
CARD_RADIUS = 34
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024

_BG = "#1d2b3a"
_BG_DEEP = "#0f1922"
_TEXT = "#f4f7fa"
_HINT = "#a9bacb"
_PANEL = "#2a3a4d"
_ACCENT = "#4fa3f7"
_DONE = "#5ac8a8"

_CONDITION_UNKNOWN = "کد هوا {code}"


class WeatherCardError(Exception):
    """A card that could not be drawn, with a safe message for the log."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def is_available() -> bool:
    """True when the rasteriser is importable on this host."""
    return resvg_py is not None


def unavailable_reason() -> str:
    return "" if resvg_py is not None else (_RESVG_IMPORT_ERROR or "resvg-py is not installed")


def _escape(text: object) -> str:
    """Escape text for SVG; the renderer receives markup, not raw user text."""
    value = str(text or "")
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fa(value: object) -> str:
    """Latin digits as Persian ones for a Persian card."""
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _round(value: float) -> int:
    return int(round(float(value)))


def _sun_icon(x: float, y: float, scale: float = 1.0) -> str:
    rays = []
    for step in range(8):
        angle = step * 45
        rays.append(
            f'<rect x="{x - 2 * scale:.1f}" y="{y - 30 * scale:.1f}" width="{4 * scale:.1f}" '
            f'height="{12 * scale:.1f}" rx="{2 * scale:.1f}" fill="#ffd479" '
            f'transform="rotate({angle} {x:.1f} {y:.1f})"/>'
        )
    return (
        f'<g>{"".join(rays)}'
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{17 * scale:.1f}" fill="#ffd479"/></g>'
    )


def _moon_icon(x: float, y: float, scale: float = 1.0, backdrop: str = _BG) -> str:
    """A crescent, cut out with the colour of whatever it is drawn on."""
    return (
        f'<g><circle cx="{x:.1f}" cy="{y:.1f}" r="{18 * scale:.1f}" fill="#dfe8f3"/>'
        f'<circle cx="{x + 8 * scale:.1f}" cy="{y - 6 * scale:.1f}" r="{15 * scale:.1f}" '
        f'fill="{backdrop}"/></g>'
    )


def _cloud_icon(x: float, y: float, scale: float = 1.0, *, rain: bool = False, snow: bool = False) -> str:
    drops = ""
    if rain:
        drops = "".join(
            f'<rect x="{x - 22 * scale + i * 20 * scale:.1f}" y="{y + 22 * scale:.1f}" '
            f'width="{5 * scale:.1f}" height="{16 * scale:.1f}" rx="{2.5 * scale:.1f}" '
            f'fill="{_ACCENT}" transform="rotate(14 {x:.1f} {y + 22 * scale:.1f})"/>'
            for i in range(3)
        )
    if snow:
        drops = "".join(
            f'<circle cx="{x - 20 * scale + i * 20 * scale:.1f}" cy="{y + 28 * scale:.1f}" '
            f'r="{4 * scale:.1f}" fill="{_TEXT}"/>'
            for i in range(3)
        )
    return (
        f'<g><circle cx="{x - 18 * scale:.1f}" cy="{y + 6 * scale:.1f}" r="{14 * scale:.1f}" fill="#eef3f8"/>'
        f'<circle cx="{x + 18 * scale:.1f}" cy="{y + 4 * scale:.1f}" r="{16 * scale:.1f}" fill="#eef3f8"/>'
        f'<rect x="{x - 26 * scale:.1f}" y="{y + 4 * scale:.1f}" width="{52 * scale:.1f}" '
        f'height="{20 * scale:.1f}" rx="{10 * scale:.1f}" fill="#eef3f8"/>'
        f'<circle cx="{x:.1f}" cy="{y - 6 * scale:.1f}" r="{20 * scale:.1f}" fill="#ffffff"/>'
        f"{drops}</g>"
    )


def condition_icon(code: int, *, is_day: Optional[int] = 1, x: float = 0, y: float = 0,
                   scale: float = 1.0, backdrop: str = _BG) -> str:
    """One icon per WMO group; unknown codes get a neutral cloud."""
    code = int(code) if isinstance(code, (int, float)) else -1
    if code == 0:
        return _sun_icon(x, y, scale) if is_day != 0 else _moon_icon(x, y, scale, backdrop)
    if code in {1, 2}:
        # Partly cloudy: the sun by day, the moon at night.
        above = (
            _sun_icon(x - 12 * scale, y - 12 * scale, scale * 0.7) if is_day != 0
            else _moon_icon(x - 12 * scale, y - 12 * scale, scale * 0.7, backdrop)
        )
        return above + _cloud_icon(x + 4 * scale, y + 8 * scale, scale * 0.9)
    if code == 3:
        return _cloud_icon(x, y, scale)
    if code in {45, 48}:
        cloud = _cloud_icon(x, y - 4 * scale, scale * 0.9)
        fog = "".join(
            f'<rect x="{x - 30 * scale:.1f}" y="{y + (14 + i * 9) * scale:.1f}" '
            f'width="{60 * scale:.1f}" height="{4 * scale:.1f}" rx="{2 * scale:.1f}" fill="{_HINT}" '
            f'opacity="{0.8 - i * 0.2:.1f}"/>'
            for i in range(3)
        )
        return cloud + fog
    if 51 <= code <= 67 or 80 <= code <= 82:
        return _cloud_icon(x, y - 4 * scale, scale * 0.9, rain=True)
    if 71 <= code <= 77 or 85 <= code <= 86:
        return _cloud_icon(x, y - 4 * scale, scale * 0.9, snow=True)
    if 95 <= code <= 99:
        cloud = _cloud_icon(x, y - 4 * scale, scale * 0.9)
        bolt = (
            f'<path d="M {x - 4 * scale:.1f} {y + 16 * scale:.1f} L {x + 8 * scale:.1f} {y + 16 * scale:.1f} '
            f'L {x:.1f} {y + 34 * scale:.1f} L {x + 12 * scale:.1f} {y + 30 * scale:.1f} '
            f'L {x - 8 * scale:.1f} {y + 46 * scale:.1f} L {x - 2 * scale:.1f} {y + 30 * scale:.1f} '
            f'L {x - 12 * scale:.1f} {y + 32 * scale:.1f} Z" fill="#ffd479"/>'
        )
        return cloud + bolt
    return _cloud_icon(x, y, scale)


def _short_hour(iso_time: str) -> str:
    """``2026-11-09T13:00`` -> ``13`` (the hour, as the card prints it)."""
    text = str(iso_time or "")
    if "T" in text:
        return text.split("T")[-1][:2]
    return text[:2]


def build_weather_card_svg(report: weather_api.WeatherReport, *, lang: str = "fa",
                           width: int = CARD_WIDTH) -> str:
    """Return the card as one self-contained SVG document.

    Kept separate from the PNG step so tests can assert on the markup without a
    rasteriser, and so a future Telegram client with real widgets could draw the
    same card as HTML instead.
    """
    current = report.current
    location = report.location.name or ""
    description = weather_api.wmo_description(current.weather_code, lang)
    hours = list(report.hourly[:6])
    fa = lang != "en"
    digits = _fa if fa else str
    now_label = "الان" if fa else "Now"
    hours_label = "ساعت به ساعت" if fa else "Hour by hour"

    height = CARD_HEIGHT
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<defs>",
        f'<linearGradient id="bg" gradientUnits="userSpaceOnUse" '
        f'x1="0" y1="0" x2="0" y2="{height}">'
        f'<stop offset="0" stop-color="{_BG}"/><stop offset="1" stop-color="{_BG_DEEP}"/>'
        f"</linearGradient>",
        "</defs>",
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="{CARD_RADIUS}" fill="url(#bg)"/>',
    ]

    # Header: the place, then the big number and its condition.
    svg.append(
        f'<text x="44" y="78" font-size="34" font-weight="bold" fill="{_TEXT}">'
        f"{_escape(location)}</text>"
    )
    svg.append(
        f'<text x="44" y="196" font-size="104" font-weight="bold" fill="{_TEXT}">'
        f"{_escape(digits(_round(current.temperature)))}°</text>"
    )
    svg.append(
        f'<text x="44" y="248" font-size="28" fill="{_HINT}">{_escape(description)}</text>'
    )

    # The big icon sits on the right, clear of the number. Its cut-outs use the
    # card's own gradient, so a crescent never shows as a disc on the dark card.
    svg.append(condition_icon(current.weather_code, is_day=current.is_day,
                              x=width - 170, y=170, scale=2.1, backdrop="url(#bg)"))

    # Details under the number: feels-like, humidity, wind.
    details = []
    if fa:
        # Persian reads right to left: the label follows its own number.
        details.append(f"احساس‌شده {digits(_round(current.apparent_temperature))}°")
        if current.humidity is not None:
            details.append(f"رطوبت {digits(_round(current.humidity))}٪")
        if current.wind_speed is not None:
            details.append(f"باد {digits(_round(current.wind_speed))}")
    else:
        details.append(f"Feels like {_round(current.apparent_temperature)}°")
        if current.humidity is not None:
            details.append(f"Humidity {_round(current.humidity)}%")
        if current.wind_speed is not None:
            details.append(f"Wind {_round(current.wind_speed)}")
    svg.append(
        f'<text x="44" y="292" font-size="26" fill="{_HINT}">'
        f"{_escape(' · '.join(details))}</text>"
    )

    # The strip of hours, in one rounded panel at the bottom.
    panel_top = 350
    svg.append(
        f'<rect x="32" y="{panel_top}" width="{width - 64}" height="96" rx="26" fill="{_PANEL}"/>'
    )
    svg.append(
        f'<text x="52" y="{panel_top - 26}" font-size="24" fill="{_HINT}">'
        f"{_escape(hours_label)}</text>"
    )
    if hours:
        slot = (width - 96) / len(hours)
        for index, hour in enumerate(hours):
            center = 48 + slot * (index + 0.5)
            label = now_label if index == 0 else digits(_short_hour(hour.time))
            svg.append(
                f'<text x="{center:.1f}" y="{panel_top + 32}" font-size="22" text-anchor="middle" '
                f'fill="{_DONE if index == 0 else _HINT}">{_escape(label)}</text>'
            )
            svg.append(condition_icon(
                hour.weather_code, is_day=hour.is_day,
                x=center, y=panel_top + 58, scale=0.42, backdrop=_PANEL,
            ))
            svg.append(
                f'<text x="{center:.1f}" y="{panel_top + 88}" font-size="24" text-anchor="middle" '
                f'fill="{_TEXT}">{_escape(digits(_round(hour.temperature)))}°</text>'
            )
    svg.append("</svg>")
    return "".join(svg)


@dataclass(frozen=True)
class WeatherCard:
    """One rendered weather card, ready for Telegram delivery.

    Mirrors ``svg_image.SvgImage`` and ``pollinations_image.GeneratedImage`` so
    ``_send_generated_image`` can send it without knowing what it is.
    """

    data: bytes
    svg: str
    width: int
    height: int
    location: str
    mime_type: str = "image/png"

    @property
    def extension(self) -> str:
        return "png"

    @property
    def filename(self) -> str:
        return f"aura-weather-{int(time.time())}-{secrets.token_hex(3)}.png"

    @property
    def telegram_safe_photo(self) -> bool:
        """Telegram accepts PNG photos below 10 MB; a card is a few KB."""
        return len(self.data) <= TELEGRAM_PHOTO_MAX_BYTES


def render_weather_card(report: weather_api.WeatherReport, *, lang: str = "fa",
                        width: int = CARD_WIDTH) -> WeatherCard:
    """Draw and encode the card; raises :class:`WeatherCardError` on failure."""
    if resvg_py is None:
        raise WeatherCardError(f"Weather cards are unavailable ({unavailable_reason()}).")
    svg = build_weather_card_svg(report, lang=lang, width=width)
    try:
        png = render_svg_image(
            svg,
            max_side=max(CARD_WIDTH, CARD_HEIGHT) * 2,
            min_long_side=CARD_WIDTH,
        )
    except SvgError as exc:
        raise WeatherCardError(f"Card rendering failed: {exc}") from exc
    return WeatherCard(
        data=png.data, svg=svg, width=png.width, height=png.height,
        location=report.location.name or "",
    )


def caption_for(report: weather_api.WeatherReport, *, lang: str = "fa") -> str:
    """The one-line caption Telegram shows in a chat list or a notification."""
    current = report.current
    if lang == "en":
        return (
            f"{report.location.name}: {weather_api.wmo_description(current.weather_code, 'en')}, "
            f"{_round(current.temperature)}°C"
        )
    return (
        f"{report.location.name}: {weather_api.wmo_description(current.weather_code)}، "
        f"{_fa(_round(current.temperature))}°C"
    )
