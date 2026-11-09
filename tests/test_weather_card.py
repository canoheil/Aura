"""The weather card: one picture, drawn locally from the same report.

The card has no network, no quota and no model: a report goes in, RGBA bytes
come out. These tests pin the parts a wrong answer would break — the Persian
text and digits, the ICON per weather code, the report's hourly rows, and the
promise that a card failure never removes the weather answer itself.
"""
import zlib
from types import SimpleNamespace

import pytest

import weather_api
import weather_card


def _report(*, hours=6, name="Barcelona", code=61, is_day=1, hourly_codes=None):
    hourly = tuple(
        weather_api.HourlyWeather(
            time=f"2026-11-09T{13 + index:02d}:00",
            weather_code=(hourly_codes[index] if hourly_codes else (61 if is_day else 0)),
            temperature=18.0 + index,
            precipitation_probability=40.0,
            is_day=is_day,
        )
        for index in range(hours)
    )
    return weather_api.WeatherReport(
        location=weather_api.ResolvedLocation(
            name=name, country="Spain", admin1="Catalonia", latitude=41.4,
            longitude=2.1, timezone="Europe/Madrid", country_code="ES",
        ),
        current=weather_api.CurrentWeather(
            temperature=18.4, apparent_temperature=17.9, humidity=72,
            precipitation=0.2, wind_speed=11, wind_direction=200, cloud_cover=80,
            weather_code=code, is_day=is_day, time="2026-11-09T13:00",
        ),
        daily=(),
        fetched_at=0.0,
        hourly=hourly,
    )


# --- the report's hourly rows -----------------------------------------------


def _hourly_payload(**overrides):
    payload = {
        "time": ["2026-11-09T13:00", "2026-11-09T14:00", "2026-11-09T15:00"],
        "weather_code": [61, 3, 0],
        "temperature_2m": [18.4, 19.1, 20.0],
        "precipitation_probability": [80, 40, 5],
        "is_day": [1, 1, 0],
    }
    payload.update(overrides)
    return {"hourly": payload}


def test_hourly_rows_are_parsed_and_filtered_to_the_future():
    rows = weather_api._hours_from_payload(_hourly_payload(), "2026-11-09T14:00")
    assert [row.time[-5:] for row in rows] == ["14:00", "15:00"]
    assert rows[0].weather_code == 3
    assert rows[0].temperature == 19.1
    assert rows[0].precipitation_probability == 40.0


def test_hourly_rows_tolerate_missing_fields_and_ragged_arrays():
    payload = _hourly_payload()
    payload["hourly"]["temperature_2m"] = [18.4]  # shorter than the time list
    payload["hourly"].pop("is_day")
    rows = weather_api._hours_from_payload(payload)
    assert len(rows) == 3
    assert rows[1].temperature == 0.0
    assert rows[1].is_day is None


def test_a_report_without_hourly_data_still_loads():
    assert weather_api._hours_from_payload({"current": {}}) == ()
    assert weather_api._hours_from_payload({}) == ()


def test_the_forecast_call_requests_hours(isolated_bot, monkeypatch):
    from test_weather_api import _FakeSession, make_forecast_response, make_geo_response

    bot, _ = isolated_bot
    session = _FakeSession({
        "https://geocoding": make_geo_response([
            {"name": "Berlin", "country": "Germany", "latitude": 52.5, "longitude": 13.4,
             "timezone": "Europe/Berlin", "country_code": "DE"},
        ]),
        "https://api.open-meteo": make_forecast_response(),
    })
    client = weather_api.WeatherClient(session=session)
    report = client.get_weather("Berlin")
    forecast_params = [call[1] for call in session.calls if "forecast" in call[0]][-1]
    assert "weather_code" in forecast_params["hourly"]
    assert forecast_params["forecast_hours"] >= 6
    assert report.hourly == ()  # this fixture carries no hourly block


# --- the SVG itself ---------------------------------------------------------


def test_card_is_one_self_contained_svg():
    svg = weather_card.build_weather_card_svg(_report())
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "http://www.w3.org/2000/svg" in svg
    assert "xlink:href" not in svg and "<image" not in svg
    assert "<script" not in svg


def test_persian_card_uses_persian_digits_and_right_to_left_labels():
    svg = weather_card.build_weather_card_svg(_report(), lang="fa")
    assert "۱۸°" in svg
    assert "باران" in svg
    assert "رطوبت ۷۲٪" in svg
    assert "الان" in svg
    assert "18°" not in svg


def test_english_card_uses_latin_digits_and_english_labels():
    svg = weather_card.build_weather_card_svg(_report(), lang="en")
    assert "18°" in svg
    assert "Slight rain" in svg
    assert "Humidity 72%" in svg
    assert "Now" in svg
    assert "۱۸" not in svg


def test_card_escapes_a_place_name_instead_of_injecting_markup():
    svg = weather_card.build_weather_card_svg(_report(name='<script>alert("x")</script>'))
    assert "<script" not in svg
    assert "&lt;script&gt;" in svg


@pytest.mark.parametrize("code", [0, 1, 2, 3, 45, 48, 51, 61, 71, 80, 85, 95, 99, -1])
def test_every_weather_code_gets_an_icon(code):
    svg = weather_card.build_weather_card_svg(_report(code=code, hourly_codes=[code] * 6))
    # An icon is a group of shapes; the strip alone always has some, so assert
    # the big one exists outside the panel too.
    assert svg.count("<g>") >= 2


def test_night_conditions_draw_a_moon_rather_than_a_sun():
    day = weather_card.build_weather_card_svg(_report(code=0, is_day=1))
    night = weather_card.build_weather_card_svg(_report(code=0, is_day=0))
    assert "#ffd479" in day
    assert "#dfe8f3" in night


def test_hour_strip_is_capped_at_six_columns():
    """More hours than the panel can hold must not widen the strip."""
    six = weather_card.build_weather_card_svg(_report(hours=6))
    nine = weather_card.build_weather_card_svg(_report(hours=9))
    assert nine == six


def test_hour_strip_shows_one_label_and_one_temperature_per_hour():
    svg = weather_card.build_weather_card_svg(_report(hours=4), lang="en")
    assert svg.count("text-anchor=\"middle\"") == 8  # label + temperature, four hours
    assert "Now" in svg


# --- the PNG ----------------------------------------------------------------


def test_rendered_card_is_a_telegram_ready_png():
    if not weather_card.is_available():
        pytest.skip(f"renderer unavailable: {weather_card.unavailable_reason()}")
    card = weather_card.render_weather_card(_report(), lang="en")
    assert card.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert card.width == weather_card.CARD_WIDTH
    assert card.height == weather_card.CARD_HEIGHT
    assert card.telegram_safe_photo is True
    assert card.mime_type == "image/png"
    assert card.filename.startswith("aura-weather-") and card.filename.endswith(".png")


def test_card_colours_are_drawn_not_left_transparent():
    """A fully transparent PNG would arrive as a blank bubble in Telegram."""
    if not weather_card.is_available():
        pytest.skip(f"renderer unavailable: {weather_card.unavailable_reason()}")
    card = weather_card.render_weather_card(_report())
    assert len(zlib.compress(card.data, 9)) > 1000  # real content, not an empty canvas


def test_a_failed_render_raises_the_card_error(monkeypatch):
    from svg_image import SvgError

    monkeypatch.setattr(weather_card, "resvg_py", None)
    with pytest.raises(weather_card.WeatherCardError):
        weather_card.render_weather_card(_report())
    assert weather_card.is_available() is False
    monkeypatch.undo()
    monkeypatch.setattr(weather_card, "resvg_py", SimpleNamespace())  # available again

    def bad_svg(*_args, **_kwargs):
        raise SvgError("nope", code="bad", user_message="bad")

    monkeypatch.setattr(weather_card, "render_svg_image", bad_svg)
    with pytest.raises(weather_card.WeatherCardError):
        weather_card.render_weather_card(_report())


def test_caption_is_one_short_line_the_chat_list_can_show():
    report = _report()
    persian = weather_card.caption_for(report, lang="fa")
    english = weather_card.caption_for(report, lang="en")
    assert persian == "Barcelona: باران سبک، ۱۸°C"
    assert english == "Barcelona: Slight rain, 18°C"
    assert "\n" not in persian and "\n" not in english


# --- wiring into the tool ---------------------------------------------------


def _queue():
    return []


def _fake_client():
    from test_weather_api import _FakeSession, make_forecast_response, make_geo_response

    return weather_api.WeatherClient(session=_FakeSession({
        "https://geocoding": make_geo_response([
            {"name": "Berlin", "country": "Germany", "latitude": 52.5, "longitude": 13.4,
             "timezone": "Europe/Berlin", "country_code": "DE"},
        ]),
        "https://api.open-meteo": make_forecast_response(),
    }))


def _namespace(monkeypatch, bot, *, available=True):
    """Everything ``_queue_weather_card`` reads, without importing bot.py."""
    monkeypatch.setattr(bot, "WEATHER_CARD_AVAILABLE", available)
    return {
        "WEATHER_CARD_AVAILABLE": available,
        "weather_card": bot.weather_card,
        "logger": __import__("logging").getLogger("test-weather-card"),
        "_turn_language": bot._turn_language,
        "i18n": bot.i18n,
    }


def _queue_card(namespace):
    return namespace


def test_the_tool_handler_itself_never_touches_the_picture_pipeline():
    """The weather tool answers with numbers; the picture is a separate step."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8"))
    handler = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_tool_get_weather")
    source = ast.unparse(handler)
    for token in ("image_delivery", "reply_photo", "WeatherCard", "delivery.append"):
        assert token not in source


def test_one_card_per_turn_is_queued_from_the_report(monkeypatch, isolated_bot):
    bot, _ = isolated_bot
    namespace = _namespace(monkeypatch, bot)
    report = _fake_client().get_weather("Berlin")
    queue = _queue()
    namespace["_queue_weather_card"] = bot._queue_weather_card
    bot._queue_weather_card({
        "_image_delivery": queue, "_weather_card_allowed": True, "_lang": "en",
    }, report)
    assert len(queue) == 1
    assert isinstance(queue[0], weather_card.WeatherCard)


def test_a_second_tool_call_does_not_add_a_second_card(monkeypatch, isolated_bot):
    bot, _ = isolated_bot
    _namespace(monkeypatch, bot)
    report = _fake_client().get_weather("Berlin")
    queue = _queue()
    for _ in range(2):
        bot._queue_weather_card({
            "_image_delivery": queue, "_weather_card_allowed": True, "_lang": "en",
        }, report)
    assert len(queue) == 1


def test_no_card_when_the_turn_is_not_a_weather_question(monkeypatch, isolated_bot):
    bot, _ = isolated_bot
    _namespace(monkeypatch, bot)
    queue = _queue()
    bot._queue_weather_card({
        "_image_delivery": queue, "_weather_card_allowed": False,
    }, _fake_client().get_weather("Berlin"))
    assert queue == []


def test_no_card_without_a_delivery_queue(monkeypatch, isolated_bot):
    bot, _ = isolated_bot
    _namespace(monkeypatch, bot)
    bot._queue_weather_card({"_weather_card_allowed": True}, _fake_client().get_weather("Berlin"))


def test_no_card_when_the_renderer_is_unavailable(monkeypatch, isolated_bot):
    bot, _ = isolated_bot
    _namespace(monkeypatch, bot, available=False)
    queue = _queue()
    bot._queue_weather_card({
        "_image_delivery": queue, "_weather_card_allowed": True,
    }, _fake_client().get_weather("Berlin"))
    assert queue == []


def test_a_card_failure_is_logged_and_never_propagates(monkeypatch, isolated_bot):
    bot, _ = isolated_bot
    _namespace(monkeypatch, bot)
    report = _fake_client().get_weather("Berlin")

    def broken(*_args, **_kwargs):
        raise weather_card.WeatherCardError("renderer said no")

    monkeypatch.setattr(bot.weather_card, "render_weather_card", broken)
    queue = _queue()
    bot._queue_weather_card({
        "_image_delivery": queue, "_weather_card_allowed": True,
    }, report)
    assert queue == []


def test_an_unexpected_card_failure_is_swallowed_too(monkeypatch, isolated_bot):
    bot, _ = isolated_bot
    _namespace(monkeypatch, bot)
    report = _fake_client().get_weather("Berlin")
    monkeypatch.setattr(
        bot.weather_card, "render_weather_card",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    queue = _queue()
    bot._queue_weather_card({"_image_delivery": queue, "_weather_card_allowed": True}, report)
    assert queue == []


def test_the_card_follows_the_users_language(monkeypatch, isolated_bot):
    bot, _ = isolated_bot
    _namespace(monkeypatch, bot)
    report = _fake_client().get_weather("Berlin")
    persian = _queue()
    bot._queue_weather_card(
        {"_image_delivery": persian, "_weather_card_allowed": True, "_lang": "fa"}, report,
    )
    # The fixture's forecast is «نیمه‌ابری» (code 1), so the Persian card must
    # carry Persian digits and the Persian hour-strip label, not the English one.
    assert "ساعت به ساعت" in persian[0].svg and "Hour by hour" not in persian[0].svg
    english = _queue()
    bot._queue_weather_card(
        {"_image_delivery": english, "_weather_card_allowed": True, "_lang": "en"}, report,
    )
    assert "Hour by hour" in english[0].svg and "ساعت به ساعت" not in english[0].svg
