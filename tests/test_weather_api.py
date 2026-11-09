"""Offline tests for the weather client and its integration into bot.py.

Everything is fake-HTTP: no network is ever touched. The bot-level checks
extract the wiring functions straight out of bot.py and exec them with fakes,
so they cover real behaviour without importing Telegram state or creating
bot.db in the project tree.
"""
import ast
import json
import logging
import os
import sys
import time
import types as pytypes
import unittest
from unittest import mock

# Tests live in tests/; the application modules stay at the repository root.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import weather_api  # noqa: E402


def make_geo_response(results, status=200):
    body = {"results": results} if results is not None else {}
    resp = mock.Mock()
    resp.status_code = status
    if status == 200:
        resp.json.return_value = body
    else:
        resp.json.side_effect = ValueError("invalid")
    resp.close.return_value = None
    return resp


def make_forecast_response(
    status=200,
    temperature=22.5,
    weather_code=1,
    humidity=55,
    precipitation=0.0,
    wind_speed=10,
    wind_direction=180,
    cloud_cover=40,
    is_day=1,
    current_time="2026-10-21T10:00",
    daily_dates=None,
    daily_codes=None,
    daily_max=None,
    daily_min=None,
    sunrise=None,
    sunset=None,
    precip_sum=None,
    precip_prob=None,
    wind_max=None,
):
    if daily_dates is None:
        daily_dates = ["2026-10-21", "2026-10-22", "2026-10-23"]
        daily_codes = [1, 61, 0]
        daily_max = [24, 19, 22]
        daily_min = [14, 12, 13]
        sunrise = ["2026-10-21T06:12", "2026-10-22T06:13", "2026-10-23T06:14"]
        sunset = ["2026-10-21T17:20", "2026-10-22T17:18", "2026-10-23T17:17"]
        precip_sum = [0.0, 4.2, 0.0]
        precip_prob = [10, 80, 5]
        wind_max = [12, 25, 9]
    payload = {
        "current": {
            "time": current_time,
            "temperature_2m": temperature,
            "relative_humidity_2m": humidity,
            "apparent_temperature": temperature + 1,
            "precipitation": precipitation,
            "weather_code": weather_code,
            "wind_speed_10m": wind_speed,
            "wind_direction_10m": wind_direction,
            "cloud_cover": cloud_cover,
            "is_day": is_day,
        },
        "daily": {
            "time": daily_dates,
            "weather_code": daily_codes,
            "temperature_2m_max": daily_max,
            "temperature_2m_min": daily_min,
            "sunrise": sunrise,
            "sunset": sunset,
            "precipitation_sum": precip_sum,
            "precipitation_probability_max": precip_prob,
            "wind_speed_10m_max": wind_max,
        },
    }
    resp = mock.Mock()
    resp.status_code = status
    if status == 200:
        resp.json.return_value = payload
    else:
        resp.json.side_effect = ValueError("invalid")
    resp.close.return_value = None
    return resp


class _FakeSession:
    """Tiny stand-in for requests.Session with scripted URL responses."""

    def __init__(self, responses_by_url):
        self._responses = responses_by_url
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        for prefix, result in self._responses.items():
            if url.startswith(prefix):
                if hasattr(result, "__code__"):
                    return result(url, params)
                return result
        raise AssertionError(f"unexpected URL: {url}")


class GeocodeTests(unittest.TestCase):
    def test_first_city_match_wins(self):
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response([{"name": "Tehran", "latitude": 35.7, "longitude": 51.4, "country": "Iran", "timezone": "Asia/Tehran"}]),
            "https://api.open-meteo": make_forecast_response(),
        }))
        report = client.get_weather("Tehran")
        self.assertEqual(report.location.name, "Tehran")
        self.assertAlmostEqual(report.location.latitude, 35.7, places=3)
        self.assertAlmostEqual(report.location.longitude, 51.4, places=3)
        self.assertEqual(report.location.timezone, "Asia/Tehran")

    def test_empty_query_raises(self):
        client = weather_api.WeatherClient(session=_FakeSession({}))
        with self.assertRaises(weather_api.WeatherError) as ctx:
            client.get_weather("   ")
        self.assertEqual(ctx.exception.code, "bad_location")

    def test_no_results_raises_not_found(self):
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response([]),
        }))
        with self.assertRaises(weather_api.WeatherError) as ctx:
            client.get_weather("Nonsenseville")
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertIn("پیدا نکردم", ctx.exception.user_message)

    def test_geocode_http_error_raises(self):
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response(None, status=502),
        }))
        with self.assertRaises(weather_api.WeatherError) as ctx:
            client.get_weather("Tehran")
        self.assertIn("Geocoding returned HTTP 502", str(ctx.exception))

    def test_geocode_rate_limit_raises(self):
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response(None, status=429),
        }))
        with self.assertRaises(weather_api.WeatherError) as ctx:
            client.get_weather("Tehran")
        self.assertEqual(ctx.exception.code, "rate_limited")

    def test_network_error_raises_unreachable(self):
        import requests
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError("down")
        session.headers = {}
        client = weather_api.WeatherClient(session=session)
        with self.assertRaises(weather_api.WeatherError) as ctx:
            client.get_weather("Tehran")
        self.assertEqual(ctx.exception.code, "unreachable")


    def test_persian_name_falls_back_to_latin_spelling(self):
        calls = []

        def geo(url, params):
            calls.append(params["name"])
            if params["name"] == "Tehran":
                return make_geo_response([{"name": "Tehran", "latitude": 35.7, "longitude": 51.4, "country": "Iran"}])
            return make_geo_response([])

        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": geo,
            "https://api.open-meteo": make_forecast_response(),
        }))
        report = client.get_weather("تهران")
        self.assertEqual(report.location.name, "Tehran")
        # The user's own wording is tried first; only then the Latin spelling.
        self.assertEqual(calls[0], "تهران")
        self.assertIn("Tehran", calls)

    def test_unknown_persian_name_does_not_retry_forever(self):
        calls = []

        def geo(url, params):
            calls.append(params["name"])
            return make_geo_response([])

        client = weather_api.WeatherClient(session=_FakeSession({"https://geocoding": geo}))
        with self.assertRaises(weather_api.WeatherError) as ctx:
            client.get_weather("قریه‌ناشناس")
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(len(calls), 2)  # fa + en, no invented extra lookups

    def test_rate_limit_surfaces_withoutTrying_variants(self):
        calls = []

        def geo(url, params):
            calls.append(params["name"])
            return make_geo_response(None, status=429)

        client = weather_api.WeatherClient(session=_FakeSession({"https://geocoding": geo}))
        with self.assertRaises(weather_api.WeatherError) as ctx:
            client.get_weather("تهران")
        self.assertEqual(ctx.exception.code, "rate_limited")
        self.assertEqual(len(calls), 1)


class TransliterationTests(unittest.TestCase):
    def test_known_persian_cities(self):
        self.assertEqual(weather_api.transliterate_place("تهران"), "Tehran")
        self.assertEqual(weather_api.transliterate_place("مشهد"), "Mashhad")
        self.assertEqual(weather_api.transliterate_place("  شیراز  "), "Shiraz")
        self.assertEqual(weather_api.transliterate_place("استانبول"), "Istanbul")

    def test_lookup_inside_phrase(self):
        self.assertEqual(weather_api.transliterate_place("هوای تبریز"), "Tabriz")

    def test_unknown_returns_none(self):
        self.assertIsNone(weather_api.transliterate_place("یوگوسلاوی"))
        self.assertIsNone(weather_api.transliterate_place(""))

    def test_maps_are_consistent(self):
        # Every key must survive normalize_text() so the lookup can hit.
        for key in weather_api.PERSIAN_PLACE_NAMES:
            self.assertEqual(weather_api.normalize_text(key).lower(), key, key)


class ForecastTests(unittest.TestCase):
    def test_current_values_map(self):
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response([{"name": "Tehran", "latitude": 35.7, "longitude": 51.4}]),
            "https://api.open-meteo": make_forecast_response(temperature=27.5, weather_code=61, humidity=40),
        }))
        report = client.get_weather("Tehran")
        self.assertAlmostEqual(report.current.temperature, 27.5)
        self.assertEqual(report.current.weather_code, 61)
        self.assertEqual(report.current.humidity, 40)
        self.assertEqual(weather_api.wmo_description(61), "باران سبک")
        self.assertEqual(weather_api.wmo_description(63), "باران")
        self.assertEqual(weather_api.wmo_description(0), "آسمان صاف")
        self.assertEqual(weather_api.wmo_description(1), "نیمه‌ابری")
        self.assertEqual(weather_api.wmo_description(95), "رعدوبرق")
        self.assertIn("1234", weather_api.wmo_description(1234))

    def test_daily_forecast_count(self):
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response([{"name": "Tehran", "latitude": 35.7, "longitude": 51.4}]),
            "https://api.open-meteo": make_forecast_response(),
        }))
        report = client.get_weather("Tehran")
        self.assertEqual(len(report.daily), 3)
        # 2nd day is rain with 80% chance
        self.assertEqual(report.daily[1].weather_code, 61)
        self.assertEqual(report.daily[1].precipitation_probability_max, 80)
        self.assertEqual(report.daily[1].sunrise, "2026-10-22T06:13")

    def test_bad_json_raises(self):
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response([{"name": "Tehran", "latitude": 35.7, "longitude": 51.4}]),
            "https://api.open-meteo": make_forecast_response(status=500),
        }))
        with self.assertRaises(weather_api.WeatherError) as ctx:
            client.get_weather("Tehran")
        self.assertEqual(ctx.exception.code, "upstream")

    def test_missing_current_raises(self):
        resp = mock.Mock()
        resp.status_code = 200
        resp.json.return_value = {"daily": {}}
        resp.close.return_value = None
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response([{"name": "Tehran", "latitude": 35.7, "longitude": 51.4}]),
            "https://api.open-meteo": resp,
        }))
        with self.assertRaises(weather_api.WeatherError) as ctx:
            client.get_weather("Tehran")
        self.assertEqual(ctx.exception.code, "bad_payload")

    def test_ragged_daily_array_skips_bad_entries(self):
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response([{"name": "Tehran", "latitude": 35.7, "longitude": 51.4}]),
            "https://api.open-meteo": make_forecast_response(
                daily_dates=["2026-10-21", "2026-10-22", "2026-10-23"],
                daily_codes=[0, None, 61],
                daily_max=[20, 21, 22],
                daily_min=[10, None, 12],
                sunrise=["2026-10-21T06:10", None, "2026-10-23T06:12"],
                sunset=["2026-10-21T17:00", None, "2026-10-23T17:10"],
                precip_sum=[0.0, None, 3],
                precip_prob=[10, None, 60],
                wind_max=[10, None, 15],
            ),
        }))
        report = client.get_weather("Tehran")
        # All three days are returned; the second is zeroed out rather than crashing.
        self.assertEqual(len(report.daily), 3)
        self.assertEqual(report.daily[1].weather_code, 0)


class CacheTests(unittest.TestCase):
    def test_cache_hit_for_same_city(self):
        calls = []
        def responder(url, params):
            calls.append((url, params))
            if "search" in url:
                return make_geo_response([{"name": "Tehran", "latitude": 35.7, "longitude": 51.4}])
            return make_forecast_response()
        client = weather_api.WeatherClient(session=_FakeSession({"": responder}), cache_ttl=600)
        r1 = client.get_weather("Tehran")
        r2 = client.get_weather("tehran")  # case-insensitive key
        self.assertIs(r1, r2)
        # exactly two HTTP calls: one geocode + one forecast
        self.assertEqual(len(calls), 2)

    def test_cache_expiry(self):
        calls = []
        def responder(url, params):
            calls.append(url)
            if "search" in url:
                return make_geo_response([{"name": "Tehran", "latitude": 35.7, "longitude": 51.4}])
            return make_forecast_response()
        client = weather_api.WeatherClient(session=_FakeSession({"": responder}), cache_ttl=60)
        client.get_weather("Tehran")
        client._cache["tehran"] = (time.monotonic() - 120, client._cache["tehran"][1])
        client.get_weather("Tehran")
        self.assertEqual(len([u for u in calls if "search" in u]), 2)


class DisabledTests(unittest.TestCase):
    def test_disabled_when_url_missing(self):
        client = weather_api.WeatherClient(enabled=True, geocode_url="", forecast_url="")
        self.assertFalse(client.is_enabled)
        with self.assertRaises(weather_api.WeatherError) as ctx:
            client.get_weather("Tehran")
        self.assertEqual(ctx.exception.code, "disabled")

    def test_invalid_url_scheme_still_reaches_request(self):
        # The client itself does not validate schemes; bot.py does at import time.
        client = weather_api.WeatherClient(enabled=True, geocode_url="ftp://x", forecast_url="http://y")
        self.assertTrue(client.is_enabled)


class FormatTests(unittest.TestCase):
    def test_format_for_model_shape(self):
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response([{"name": "Tehran", "latitude": 35.7, "longitude": 51.4, "country": "Iran", "admin1": "Tehran", "timezone": "Asia/Tehran"}]),
            "https://api.open-meteo": make_forecast_response(temperature=22.5, weather_code=0),
        }))
        report = client.get_weather("Tehran")
        data = client.format_for_model(report)
        self.assertEqual(data["current"]["temperature_c"], 22.5)
        self.assertEqual(data["current"]["condition"], "آسمان صاف")
        self.assertIn("Tehran", data["location"])
        self.assertEqual(len(data["daily"]), 3)
        self.assertEqual(data["units"]["temperature"], "celsius")
        # Round-trip through JSON: model sees only compact fields, no raw payload.
        blob = json.dumps(data)
        self.assertLess(len(blob), 4000)

    def test_format_time_helper(self):
        client = weather_api.WeatherClient(session=_FakeSession({
            "https://geocoding": make_geo_response([{"name": "Test", "latitude": 1, "longitude": 1}]),
            "https://api.open-meteo": make_forecast_response(),
        }))
        report = client.get_weather("Test")
        data = client.format_for_model(report)
        self.assertEqual(data["daily"][0]["sunrise"], "06:12")
        self.assertEqual(data["daily"][0]["sunset"], "17:20")


class IntentTests(unittest.TestCase):
    def test_looks_like_weather_positive(self):
        for text in (
            "هوای تهران چطوره",
            "آب و هوا مشهد",
            "هواشناسی تبریز",
            "وضعیت هوای امروز رشت",
            "weather in berlin",
            "will it rain in paris tomorrow",
            "فردا بارون میاد",
            "دما چنده",
        ):
            self.assertTrue(weather_api.looks_like_weather_request(text), f"missing: {text}")

    def test_looks_like_weather_negative(self):
        for text in (
            "سلام",
            "خوبی؟",
            "درباره هواشناسی چیکار کنم",
            "یه عکس بساز از هوای ابری",
            "برنامه‌نویسی چطوره",
            "چند درجه سرد شده ولی بریم بیرون",
            "آب بیاور",
        ):
            self.assertFalse(weather_api.looks_like_weather_request(text), f"false positive: {text}")

    def test_extract_location(self):
        self.assertIn("تهران", weather_api.extract_weather_location("هوای تهران چطوره؟"))
        self.assertIn("مشهد", weather_api.extract_weather_location("آب و هوا مشهد فردا"))
        self.assertIn("Berlin", weather_api.extract_weather_location("weather in Berlin today"))
        self.assertIn("بارسلون", weather_api.extract_weather_location("دما بارسلون چنده"))

    def test_extract_location_empty_for_no_place(self):
        # "هوای چطوره" has no place token
        loc = weather_api.extract_weather_location("آب و هوا چطوره")
        self.assertTrue(loc is None or len(loc) < 2 or loc not in weather_api._LOCATION_FILLERS)


class BotWiringTests(unittest.TestCase):
    """Behavioural checks on the weather wiring inside ``bot.py``.

    The repo convention is to never ``import bot`` in tests: importing it opens
    ``bot.db`` in the project tree, which a separate test forbids. So the handler,
    the tool schema and the prompt section are extracted from the source and
    executed in a controlled namespace instead — real coverage, no Telegram state.
    """

    @classmethod
    def setUpClass(cls):
        root = ROOT
        with open(os.path.join(root, "bot.py"), encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.tree = ast.parse(cls.source)
        cls.funcs = {
            node.name: node for node in cls.tree.body if isinstance(node, ast.FunctionDef)
        }

    def _exec(self, name, namespace):
        node = self.funcs[name]
        module = ast.Module(body=[node], type_ignores=[])
        exec(compile(module, f"<bot.py:{name}>", "exec"), namespace)
        return namespace[name]

    def _module_constant(self, name):
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets
            ):
                return ast.literal_eval(node.value)
        raise AssertionError(f"{name} is not a module-level constant")

    def _handler_namespace(self, client):
        return {
            "weather_client": client,
            "logger": logging.getLogger("test-weather"),
            "WeatherError": weather_api.WeatherError,
            # The handler hands picture work to a separate helper on purpose;
            # this harness executes the handler alone, so stub the seam.
            "_queue_weather_card": lambda *args, **kwargs: None,
        }

    def test_handler_disabled_client(self):
        fake = mock.Mock()
        fake.is_enabled = False
        handler = self._exec("_tool_get_weather", self._handler_namespace(fake))
        result = handler({"location": "Tehran"})
        self.assertFalse(result["ok"])
        fake.get_weather.assert_not_called()

    def test_handler_fails_gracefully_without_location(self):
        fake = mock.Mock()
        fake.is_enabled = True
        handler = self._exec("_tool_get_weather", self._handler_namespace(fake))
        result = handler({})
        self.assertFalse(result["ok"])
        self.assertIn("location", str(result.get("error", "")).lower())
        fake.get_weather.assert_not_called()

    def test_handler_reports_tool_error_without_secrets(self):
        fake = mock.Mock()
        fake.is_enabled = True
        fake.get_weather.side_effect = weather_api.WeatherError(
            "Geocoding returned HTTP 500.", code="upstream", user_message="سرویس جواب نداد"
        )
        handler = self._exec("_tool_get_weather", self._handler_namespace(fake))
        result = handler({"location": "Tehran"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["safe_user_message"], "سرویس جواب نداد")
        # The tool result never leaks endpoints to the model.
        self.assertNotIn("http", json.dumps(result))

    def test_handler_success_returns_compact_payload(self):
        fake = mock.Mock()
        fake.is_enabled = True
        fake.get_weather.return_value = mock.Mock()
        fake.format_for_model.return_value = {
            "location": "Tehran, Iran",
            "latitude": 35.7,
            "longitude": 51.4,
            "current": {"temperature_c": 20.0, "weather_code": 0},
            "daily": [],
            "units": {"temperature": "celsius"},
        }
        handler = self._exec("_tool_get_weather", self._handler_namespace(fake))
        result = handler({"location": "تهران"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["weather"]["location"], "Tehran, Iran")
        fake.get_weather.assert_called_once_with("تهران")

    def test_handler_never_touches_image_pipeline(self):
        src = ast.unparse(self.funcs["_tool_get_weather"])
        for token in ("_model_image", "image_delivery", "reply_photo", "meme_client"):
            self.assertNotIn(token, src)

    def test_declaration_requires_location(self):
        namespace = {
            "types": pytypes.SimpleNamespace(FunctionDeclaration=lambda **kw: kw),
            "_WEATHER_TOOL_DESCRIPTION": self._module_constant("_WEATHER_TOOL_DESCRIPTION"),
        }
        builder = self._exec("_build_weather_declaration", namespace)
        declaration = builder()
        self.assertEqual(declaration["name"], "get_weather")
        self.assertEqual(declaration["parameters_json_schema"]["required"], ["location"])
        self.assertIn("تهران", declaration["description"])
        self.assertIn("Do not call", declaration["description"])

    def test_prompt_section_content(self):
        namespace = {}
        section = self._exec("_weather_prompt_section", namespace)()
        self.assertIn("Weather:", section)
        for line in ("get_weather", "ok=true", "ok=false", "Never invent weather"):
            self.assertIn(line, section)

    def test_config_block_validates_env(self):
        module_src = ast.unparse(ast.Module(body=[n for n in self.tree.body if isinstance(n, ast.Assign)], type_ignores=[]))
        self.assertIn("WEATHER_ENABLED", module_src)
        self.assertIn("WEATHER_GEOCODE_URL", self.source)
        self.assertIn("WEATHER_FORECAST_URL", self.source)
        self.assertIn("WEATHER_CACHE_TTL", self.source)
        self.assertIn("WEATHER_TIMEOUT must be between 3 and 30 seconds", self.source)

    def test_tool_registered_only_when_enabled(self):
        registry_src = ast.unparse(self.funcs["_create_tool_registry"])
        self.assertIn("name='get_weather'", registry_src)
        self.assertIn("if weather_client.is_enabled:", registry_src)
        # Weather results are cacheable: only the meme tool opts out of caching.
        weather_block = registry_src.split("name='get_weather'")[1].split(")\n")[0]
        self.assertNotIn("cache_results=False", weather_block)

    def test_env_example_documents_weather(self):
        root = ROOT
        with open(os.path.join(root, ".env.example"), encoding="utf-8") as fh:
            env = fh.read()
        for var in (
            "WEATHER_ENABLED", "WEATHER_GEOCODE_URL", "WEATHER_FORECAST_URL",
            "WEATHER_TIMEOUT", "WEATHER_CACHE_TTL", "WEATHER_TOOL_MAX_CALLS",
        ):
            self.assertIn(var, env)

    def test_imported_once(self):
        # A duplicated inline import would shadow the module-level one.
        self.assertEqual(self.source.count("from weather_api import"), 1)


if __name__ == "__main__":
    unittest.main()
