import asyncio

import pytest
from test_agent_tool_response import _telegram_update, _text_response, _tool_response
from test_weather_api import _FakeSession, make_forecast_response, make_geo_response

import weather_api

PLACES = [
    {"name": "Springfield", "admin1": "Illinois", "country": "United States", "country_code": "US", "latitude": 39.8, "longitude": -89.6, "timezone": "America/Chicago"},
    {"name": "Springfield", "admin1": "Massachusetts", "country": "United States", "country_code": "US", "latitude": 42.1, "longitude": -72.6, "timezone": "America/New_York"},
    {"name": "Springfield Township", "admin1": "Ohio", "country": "United States", "country_code": "US", "latitude": 40, "longitude": -83},
]


def _client():
    session = _FakeSession({
        "https://geocoding": make_geo_response(PLACES),
        "https://api.open-meteo": make_forecast_response(),
    })
    return weather_api.WeatherClient(session=session), session


def test_location_extraction_keeps_explicit_country_or_region():
    assert weather_api.extract_weather_location("weather in Springfield, Massachusetts") == "Springfield, Massachusetts"
    assert weather_api.extract_weather_location("هوای تهران، ایران چطوره؟") == "تهران, ایران"
    assert weather_api.extract_weather_location("weather in Berlin, please") == "Berlin"


def test_namesakes_require_choice_instead_of_fetching_first_forecast():
    client, session = _client()
    with pytest.raises(weather_api.WeatherError) as exc:
        client.get_weather("Springfield")
    assert exc.value.code == "ambiguous"
    assert len(exc.value.choices) == 2
    assert "Illinois" in exc.value.user_message and "Massachusetts" in exc.value.user_message
    assert all("geocoding" in url for url, _, _ in session.calls)


def test_explicit_region_selects_matching_city():
    client, session = _client()
    report = client.get_weather("Springfield, Massachusetts")
    assert report.location.latitude == 42.1
    assert session.calls[0][1]["name"] == "Springfield"
    assert session.calls[-1][1]["latitude"] == 42.1


def test_unknown_region_does_not_fall_back_to_first():
    client, session = _client()
    with pytest.raises(weather_api.WeatherError) as exc:
        client.get_weather("Springfield, NotReal")
    assert exc.value.code == "not_found"
    assert all("geocoding" in url for url, _, _ in session.calls)


def test_handler_uses_user_supplied_region_without_an_ambiguity_prompt(isolated_bot, monkeypatch):
    bot, gemini = isolated_bot
    weather_client, session = _client()
    monkeypatch.setattr(bot, "weather_client", weather_client)
    gemini.models.responses = [_text_response("هوای Springfield, Massachusetts")]
    update, context, _ = _telegram_update()
    update.message.text = "weather in Springfield, Massachusetts"
    asyncio.run(bot.message_handler(update, context))
    assert not bot._weather_choice_context.get((123, 123))
    assert len(gemini.models.requests) == 1
    forecast = [call for call in session.calls if "forecast" in call[0]][0]
    assert forecast[1]["latitude"] == 42.1


def test_unambiguous_weather_is_prefetched_before_model_answer(isolated_bot, monkeypatch):
    bot, gemini = isolated_bot
    session = _FakeSession({
        "https://geocoding": make_geo_response(PLACES[:1]),
        "https://api.open-meteo": make_forecast_response(temperature=22.5),
    })
    monkeypatch.setattr(bot, "weather_client", weather_api.WeatherClient(session=session))
    gemini.models.responses = [_text_response("دمای فعلی ۲۲٫۵ درجه است.")]
    update, context, _ = _telegram_update()
    update.message.text = "weather in Springfield"
    asyncio.run(bot.message_handler(update, context))
    assert len(gemini.models.requests) == 1
    system = gemini.models.requests[0][2].system_instruction
    assert "22.5" in system and "Springfield" in system
    assert any("forecast" in url for url, _, _ in session.calls)


def test_real_handler_asks_then_uses_exact_selected_coordinates(isolated_bot, monkeypatch):
    bot, gemini = isolated_bot
    weather_client, session = _client()
    monkeypatch.setattr(bot, "weather_client", weather_client)
    registry = bot._create_tool_registry()
    monkeypatch.setattr(bot, "TOOL_REGISTRY", registry)
    monkeypatch.setattr(bot, "AGENT_RUNNER", bot.AgentRunner(registry))
    gemini.models.responses = [_tool_response("get_weather", {"location": "Springfield"})]
    update, context, _ = _telegram_update()
    update.message.text = "weather in Springfield"
    asyncio.run(bot.message_handler(update, context))
    question = update.message.reply_text.await_args.args[0]
    assert "Illinois" in question and "Massachusetts" in question
    assert len(gemini.models.requests) == 0  # ambiguity is resolved before the model can guess
    assert bot._weather_choice_context.get((123, 123))

    followup, followup_context, _ = _telegram_update()
    followup.message.text = "۲"
    asyncio.run(bot.message_handler(followup, followup_context))
    assert "Massachusetts" in followup.message.reply_text.await_args.args[0]
    forecast_call = [call for call in session.calls if "forecast" in call[0]][0]
    assert forecast_call[1]["latitude"] == 42.1
    assert bot._weather_choice_context.get((123, 123)) is None
    assert len(gemini.models.requests) == 0
