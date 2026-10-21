"""Weather fetching for Aura, backed by the keyless Open-Meteo service.

This module is deliberately offline-safe: it never imports Telegram or Gemini
and never sends anything itself. bot.py owns the tool declaration and the
model prompt; this layer owns the policy and the network I/O:

* resolve a free-text location (Persian or English) to coordinates via the
  Open-Meteo geocoding API, retrying a known Persian name with its Latin
  spelling when the geocoder has nothing to say,
* call ``GET https://api.open-meteo.com/v1/forecast`` for current + daily,
* map WMO weather codes to Persian descriptions,
* cache recent results for a few minutes to avoid hammering the free service.

The public API is small: :class:`WeatherClient`, :class:`WeatherReport`,
:class:`WeatherError`, plus :func:`looks_like_weather_request` and
:func:`extract_weather_location` which give the chat pipeline a conservative,
testable hint.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger("aura-weather")

DEFAULT_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
DEFAULT_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_CACHE_TTL_SECONDS = 600
_MAX_LOCATION_CHARS = 80
_MAX_CACHE_ENTRIES = 256

_USER_MESSAGE_UNAVAILABLE = "الان نتونستم وضع هوا رو بگیرم؛ سرویس هواشناسی جواب نداد. یه دقیقه دیگه امتحان کن."
_USER_MESSAGE_NOT_FOUND = "اون شهر رو پیدا نکردم. اسمش رو به فارسی یا انگلیسی دقیق‌تر بگو — مثلا «تهران» یا «Shiraz»."
_USER_MESSAGE_BAD_LOCATION = "اسم شهر معتبر نیست."

# WMO weather interpretation codes -> Persian short description.
# Source: https://open-meteo.com/en/docs#weathervariables
WMO_CODE_DESCRIPTIONS: dict[int, str] = {
    0: "آسمان صاف",
    1: "نیمه‌ابری",
    2: "ابری",
    3: "ابری کامل",
    45: "مه‌آلود",
    48: "مه یخ‌زده",
    51: "نم‌نم باران سبک",
    53: "نم‌نم باران",
    55: "نم‌نم باران شدید",
    56: "نم‌نم باران یخ‌زده سبک",
    57: "نم‌نم باران یخ‌زده شدید",
    61: "باران سبک",
    63: "باران",
    65: "باران شدید",
    66: "باران یخ‌زده سبک",
    67: "باران یخ‌زده شدید",
    71: "برف سبک",
    73: "برف",
    75: "برف شدید",
    77: "دانه برف",
    80: "رگبار سبک",
    81: "رگبار",
    82: "رگبار شدید",
    85: "رگبار برف سبک",
    86: "رگبار برف شدید",
    95: "رعدوبرق",
    96: "رعدوبرق با تگرگ سبک",
    99: "رعدوبرق با تگرگ شدید",
}

# English fallback for WMO codes (used when formatting for English replies if needed).
WMO_CODE_DESCRIPTIONS_EN: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def wmo_description(code: int, lang: str = "fa") -> str:
    code = int(code) if isinstance(code, (int, float)) else -1
    if lang == "en":
        return WMO_CODE_DESCRIPTIONS_EN.get(code, f"کد هوا {code}")
    return WMO_CODE_DESCRIPTIONS.get(code, f"کد هوا {code}")


class WeatherError(Exception):
    """A weather fetch failure with a human-readable Persian message."""

    def __init__(self, message: str, *, code: str = "", user_message: str = _USER_MESSAGE_UNAVAILABLE) -> None:
        super().__init__(message[:400])
        self.code = code
        self.user_message = user_message


@dataclass(frozen=True)
class ResolvedLocation:
    name: str
    country: str
    admin1: str
    latitude: float
    longitude: float
    timezone: str
    country_code: str = ""


@dataclass(frozen=True)
class CurrentWeather:
    temperature: float
    apparent_temperature: float
    humidity: Optional[float]
    precipitation: Optional[float]
    wind_speed: Optional[float]
    wind_direction: Optional[float]
    cloud_cover: Optional[float]
    weather_code: int
    is_day: Optional[int]
    time: str


@dataclass(frozen=True)
class DailyWeather:
    date: str
    weather_code: int
    temp_max: float
    temp_min: float
    sunrise: str
    sunset: str
    precipitation_sum: Optional[float]
    precipitation_probability_max: Optional[float]
    wind_speed_max: Optional[float]


@dataclass(frozen=True)
class WeatherReport:
    location: ResolvedLocation
    current: CurrentWeather
    daily: tuple[DailyWeather, ...]
    fetched_at: float


# ---------- conservative request detection ----------

# Three tiers, mirroring how the meme hint stays conservative:
# * a strong phrase always means a weather question,
# * a bare mention of "هوا" needs an interrogative,
# * a detail word (rain / cold / humidity) needs both, so statements about
#   feeling cold do not fire a network call.
_STRONG_WEATHER_TERMS = (
    # normalize_text() maps ZWNJ to a space, so both spellings are listed.
    "آب و هوا", "آب هوا", "آب‌وهوا", "هواشناسی", "هوا شناسی",
    "وضعیت هوا", "وضعیت هوای", "دمای هوا", "دمای هوای", "پیش‌بینی هوا", "پیشبینی هوا",
    "weather", "forecast",
)
_BARE_WEATHER_TERMS = ("هوا", "هوای")
_WEATHER_DETAIL_TERMS = (
    "باران", "بارون", "بارندگی", "بارش", "برف", "طوفان", "آفتابی", "ابری", "شرجی",
    "رطوبت", "باد", "دما", "سرد", "گرم", "خنک", "نم نم", "نم‌نم", "رگبار", "رعد",
    "مه غبار", "سیل",
    "rain", "snow", "storm", "sunny", "cloudy", "humidity", "temperature", "frost", "hail",
)
# Words that make the interrogative explicit; used for the weaker tiers so that
# "چند درجه سرد شده ولی بریم بیرون" stays a statement, not a tool call.
_INTERROGATIVE_TERMS = (
    "چطوره", "چطوریه", "چنده", "چقدره", "میاد", "می آد", "می‌آد", "میباره", "می‌باره",
    "هست", "هستی", "؟", "?",
    "how is", "how's", "will it", "is it", "rain tomorrow", "how cold", "how hot",
)
# "درباره هواشناسی چیکار کنم" is a question about the field, not about the sky.
_TALKING_ABOUT_TERMS = (
    "درباره", "راجب", "راجع", "رشته", "تحصیل", "شغل", "کتاب", "مقاله", "سازمان هواشناسی",
)
_CREATION_MARKERS = (
    "بساز", "درست کن", "بکش", "طراحی کن", "رسم کن", "لوگو", "نقشه", "استیکر", "میم",
    "generate", "create", "draw", "logo", "sticker", "meme",
)

# Common fillers to strip when extracting location
_LOCATION_FILLERS = {
    "آب", "و", "هوا", "هوای", "هواشناسی", "دمای", "وضعیت", "پیش", "بینی", "پیشبینی",
    "چطوره", "چطوریه", "چگونه", "چنده", "چند", "درجه", "هست", "بود", "است",
    "امروز", "امشب", "فردا", "دیروز", "این", "هفته", "ماه", "الان", "فعلا", "فعلاً",
    "شهر", "استان", "کشور", "منطقه", "ناحیه",
    "بگو", "بده", "نشون", "بده", "لطفا", "لطفاً", "برام", "برای", "من", "واسم", "واسه",
    "به", "از", "در", "را", "رو", "که", "یا", "با", "تا", "درباره", "راجب", "راجع",
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "for", "of", "about",
    "weather", "forecast", "temperature", "today", "tomorrow", "tonight", "this", "week",
    "please", "tell", "me", "show", "what", "how", "is", "will", "be", "it",
    "city", "town",
}


def normalize_text(text: str) -> str:
    value = (text or "").replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", value).strip()


# Anything that is weather vocabulary rather than a place name; used to dig the
# city out of a sentence without a second model call.
_STRIP_TERMS = tuple(
    sorted(
        set(_STRONG_WEATHER_TERMS)
        | set(_BARE_WEATHER_TERMS)
        | set(_WEATHER_DETAIL_TERMS)
        | set(_INTERROGATIVE_TERMS)
        | {"لطفا", "لطفاً", "امروز", "امشب", "فردا", "دیروز", "پیشبینی", "پیش‌بینی",
           "چطور", "چگونه", "چند درجه", "این هفته", "هفته", "week", "day", "right now",
           "now", "الان", "فعلا", "check", "بگو", "ببین", "می‌گه", "میگه"},
        key=len,
        reverse=True,
    )
)
_STRIP_PATTERN = re.compile(
    "|".join(re.escape(term) for term in _STRIP_TERMS if term.strip()),
    flags=re.IGNORECASE,
)


def _has_any(text: str, terms) -> bool:
    return any(term.lower() in text for term in terms if term.strip())


def looks_like_weather_request(text: str) -> bool:
    """True only for an explicit weather/forecast request.

    Deliberately conservative: "درباره هواشناسی چیکار کنم" talks about the
    field, and "چند درجه سرد شده" is a statement, so neither may spend a tool
    call. A missing nudge only costs one round of the model deciding on its
    own, while a false one sends weather nobody asked for.
    """
    normalized = normalize_text(text)
    if not normalized:
        return False
    lowered = normalized.lower()
    if _has_any(lowered, _CREATION_MARKERS) or _has_any(lowered, _TALKING_ABOUT_TERMS):
        return False
    if _has_any(lowered, _STRONG_WEATHER_TERMS):
        return True
    bare = _has_any(lowered, _BARE_WEATHER_TERMS)
    detail = _has_any(lowered, _WEATHER_DETAIL_TERMS)
    question = _has_any(lowered, _INTERROGATIVE_TERMS)
    if bare and (detail or question):
        return True
    return bool(detail and question)


def extract_weather_location(text: str) -> Optional[str]:
    """Return the place the user asked about, or ``None`` when unnamed.

    Only local vocabulary is removed; the city itself is passed to the geocoder
    unchanged so the service, not this regex, decides what is a real place.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    cleaned = _STRIP_PATTERN.sub(" ", normalized)
    cleaned = re.sub(r"[?؟.!،,؛;:\"'()\[\]{}«»]", " ", cleaned)
    kept = [
        token
        for token in cleaned.split()
        if token.lower() not in _LOCATION_FILLERS and len(token) > 1
    ]
    location = re.sub(r"\s+", " ", " ".join(kept)).strip()
    if len(location) < 2 or location.lower() in _LOCATION_FILLERS:
        return None
    return location[:_MAX_LOCATION_CHARS]


# ---------- client ----------


# The Open-Meteo geocoder matches Latin names best, so the most common Persian
# spellings are mapped locally. A missed entry is not fatal: the original
# wording is always tried first and the geocoder still gets the final say.
PERSIAN_PLACE_NAMES = {
    # Iran
    "تهران": "Tehran", "مشهد": "Mashhad", "اصفهان": "Isfahan", "شیراز": "Shiraz",
    "تبریز": "Tabriz", "کرج": "Karaj", "اهواز": "Ahvaz", "رشت": "Rasht",
    "کرمان": "Kerman", "یزد": "Yazd", "اردبیل": "Ardabil", "بندرعباس": "Bandar Abbas",
    "بندر عباس": "Bandar Abbas", "ارومیه": "Urmia", "قم": "Qom", "قزوین": "Qazvin",
    "زاهدان": "Zahedan", "سنندج": "Sanandaj", "کرمانشاه": "Kermanshah", "همدان": "Hamadan",
    "ساری": "Sari", "گرگان": "Gorgan", "خرمآباد": "Khorramabad", "خرم آباد": "Khorramabad",
    "ایلام": "Ilam", "بوشهر": "Bushehr", "بجنورد": "Bojnurd", "شهرکرد": "Shahrekord",
    "زنجان": "Zanjan", "کاشان": "Kashan", "چابهار": "Chabahar", "انزلی": "Anzali",
    # Frequent foreign cities in Persian conversation
    "استانبول": "Istanbul", "آنکارا": "Ankara", "ایزمیر": "Izmir", "وان": "Van",
    "دبی": "Dubai", "ابوظبی": "Abu Dhabi", "دوحه": "Doha", "جده": "Jeddah",
    "مکه": "Mecca", "مدینه": "Medina", "بغداد": "Baghdad", "کربلا": "Karbala",
    "نجف": "Najaf", "قاهره": "Cairo", "بیروت": "Beirut",
    "مسکو": "Moscow", "کیف": "Kyiv", "مینسک": "Minsk", "توکیو": "Tokyo",
    "پکن": "Beijing", "دهلی": "New Delhi", "کولالامپور": "Kuala Lumpur",
    "پاریس": "Paris", "لندن": "London", "برلین": "Berlin", "مونیخ": "Munich",
    "هامبورگ": "Hamburg", "فرانکفورت": "Frankfurt", "رم": "Rome", "میلان": "Milan",
    "مادرید": "Madrid", "بارسلون": "Barcelona", "لشبونه": "Lisbon",
    "امستردام": "Amsterdam", "بروکسل": "Brussels", "برسلز": "Brussels", "ویانا": "Vienna",
    "زوریخ": "Zurich", "استکهلم": "Stockholm", "اسلو": "Oslo", "کپنهاگ": "Copenhagen",
    "آتن": "Athens", "نیویورک": "New York", "شیکاگو": "Chicago", "لسآنجلس": "Los Angeles",
    "تورنتو": "Toronto", "ونکوور": "Vancouver", "سیدنی": "Sydney", "ملبورن": "Melbourne",
}


def transliterate_place(query: str) -> Optional[str]:
    """Latin spelling for a known Persian place name, else ``None``."""
    normalized = normalize_text(query).lower()
    if not normalized:
        return None
    if normalized in PERSIAN_PLACE_NAMES:
        return PERSIAN_PLACE_NAMES[normalized]
    for token in normalized.split():
        if token in PERSIAN_PLACE_NAMES:
            return PERSIAN_PLACE_NAMES[token]
    return None


class WeatherClient:
    """Fetch weather from Open-Meteo without an API key.

    Every failure path raises :class:`WeatherError`; the tool handler converts it
    into a short function response so the model can recover in natural language.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        geocode_url: str = DEFAULT_GEOCODE_URL,
        forecast_url: str = DEFAULT_FORECAST_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._geocode_url = str(geocode_url or "").strip().rstrip("/")
        self._forecast_url = str(forecast_url or "").strip().rstrip("/")
        self._timeout = max(3.0, min(30.0, float(timeout)))
        self._cache_ttl = max(60.0, min(3600.0, float(cache_ttl)))
        self._session = session
        self._owned_session: Optional[requests.Session] = None
        self._cache: dict[str, tuple[float, WeatherReport]] = {}
        self._cache_lock = threading.Lock()

    @property
    def is_enabled(self) -> bool:
        return self._enabled and bool(self._geocode_url) and bool(self._forecast_url)

    @property
    def session(self) -> requests.Session:
        if self._session is not None:
            return self._session
        if self._owned_session is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": "AuraBot/1.0 (weather; +https://github.com/canoheil/Aura)",
                "Accept": "application/json",
            })
            self._owned_session = s
        return self._owned_session

    def get_weather(self, location: str) -> WeatherReport:
        """Resolve location and fetch weather, or raise :class:`WeatherError`."""
        if not self.is_enabled:
            raise WeatherError("Weather is disabled.", code="disabled", user_message="قابلیت هواشناسی فعال نیست.")
        query = normalize_text(location or "")[:_MAX_LOCATION_CHARS]
        if not query or len(query) < 2:
            raise WeatherError("Location is empty.", code="bad_location", user_message=_USER_MESSAGE_BAD_LOCATION)
        cache_key = query.lower()
        with self._cache_lock:
            entry = self._cache.get(cache_key)
            if entry and (time.monotonic() - entry[0] < self._cache_ttl):
                logger.info("Weather cache hit | location=%s", query)
                return entry[1]

        resolved = self._geocode(query)
        report = self._forecast(resolved)

        with self._cache_lock:
            # Evict oldest if over capacity
            if len(self._cache) >= _MAX_CACHE_ENTRIES:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
            self._cache[cache_key] = (time.monotonic(), report)

        logger.info(
            "Weather fetched | location=%s (%s) lat=%.3f lon=%.3f temp=%.1f code=%s",
            resolved.name, resolved.country, resolved.latitude, resolved.longitude,
            report.current.temperature, report.current.weather_code,
        )
        return report

    def _geocode(self, query: str) -> ResolvedLocation:
        """Resolve a free-text place into coordinates.

        The user's own wording is always tried first (localized for Persian
        questions); only when the geocoder has nothing to say do we retry with
        the Latin spelling from :data:`PERSIAN_PLACE_NAMES`.
        """
        has_persian = bool(re.search(r"[\u0600-\u06ff]", query))
        languages = ("fa", "en") if has_persian else ("en", "fa")
        candidates = [query]
        latin = transliterate_place(query)
        if latin and latin.lower() != query.lower():
            candidates.append(latin)

        last_error: Optional[WeatherError] = None
        for candidate in candidates:
            for lang in languages:
                try:
                    payload = self._get_json(
                        self._geocode_url,
                        {"name": candidate, "count": 3, "language": lang, "format": "json"},
                        label="Geocoding",
                    )
                except WeatherError as exc:
                    # A missing result is a reason to try the next variant; a
                    # rate limit or outage is not, so those surface immediately.
                    if exc.code != "not_found":
                        raise
                    last_error = exc
                    continue
                hit = self._pick_result(payload)
                if hit is not None:
                    return ResolvedLocation(
                        name=str(hit.get("name") or candidate).strip(),
                        country=str(hit.get("country") or "").strip(),
                        admin1=str(hit.get("admin1") or "").strip(),
                        latitude=float(hit["latitude"]),
                        longitude=float(hit["longitude"]),
                        timezone=str(hit.get("timezone") or "auto").strip() or "auto",
                        country_code=str(hit.get("country_code") or "").strip(),
                    )
                last_error = WeatherError(
                    f"No geocoding results for {candidate!r} (lang={lang})",
                    code="not_found",
                    user_message=_USER_MESSAGE_NOT_FOUND,
                )
        raise last_error or WeatherError("Geocoding found nothing.", code="not_found", user_message=_USER_MESSAGE_NOT_FOUND)

    @staticmethod
    def _pick_result(payload: dict) -> Optional[dict]:
        """First usable result; the geocoder already orders them by relevance."""
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return None
        for hit in results:
            if isinstance(hit, dict) and hit.get("latitude") is not None and hit.get("longitude") is not None:
                return hit
        return None

    def _get_json(self, url: str, params: dict, *, label: str) -> dict:
        try:
            resp = self.session.get(url, params=params, timeout=(min(5.0, self._timeout), self._timeout))
        except requests.RequestException as exc:
            raise WeatherError(f"{label} network error ({type(exc).__name__})", code="unreachable") from exc
        try:
            status = int(getattr(resp, "status_code", 0) or 0)
            if status == 429:
                raise WeatherError(f"{label} rate limited.", code="rate_limited", user_message=_USER_MESSAGE_UNAVAILABLE)
            if status != 200:
                raise WeatherError(f"{label} returned HTTP {status}.", code="upstream")
            try:
                payload = resp.json()
            except ValueError as exc:
                raise WeatherError(f"{label} returned invalid JSON.", code="bad_json") from exc
        finally:
            close = getattr(resp, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if not isinstance(payload, dict):
            raise WeatherError(f"{label} returned unexpected payload.", code="bad_payload")
        return payload

    def _forecast(self, loc: ResolvedLocation) -> WeatherReport:
        params = {
            "latitude": loc.latitude,
            "longitude": loc.longitude,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,precipitation,cloud_cover,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_sum,precipitation_probability_max,wind_speed_10m_max",
            "timezone": "auto",
            "forecast_days": 3,
        }
        payload = self._get_json(self._forecast_url, params, label="Forecast")

        current_raw = payload.get("current")
        daily_raw = payload.get("daily")
        if not isinstance(current_raw, dict):
            raise WeatherError("Forecast missing current weather.", code="bad_payload")
        if not isinstance(daily_raw, dict):
            raise WeatherError("Forecast missing daily weather.", code="bad_payload")

        try:
            current = CurrentWeather(
                temperature=float(current_raw["temperature_2m"]),
                apparent_temperature=float(current_raw.get("apparent_temperature", current_raw["temperature_2m"])),
                humidity=float(current_raw["relative_humidity_2m"]) if current_raw.get("relative_humidity_2m") is not None else None,
                precipitation=float(current_raw["precipitation"]) if current_raw.get("precipitation") is not None else None,
                wind_speed=float(current_raw["wind_speed_10m"]) if current_raw.get("wind_speed_10m") is not None else None,
                wind_direction=float(current_raw.get("wind_direction_10m")) if current_raw.get("wind_direction_10m") is not None else None,
                cloud_cover=float(current_raw["cloud_cover"]) if current_raw.get("cloud_cover") is not None else None,
                weather_code=int(current_raw["weather_code"]),
                is_day=int(current_raw["is_day"]) if current_raw.get("is_day") is not None else None,
                time=str(current_raw.get("time") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError(f"Forecast current fields invalid: {exc}", code="bad_payload") from exc

        try:
            dates = daily_raw.get("time") or []
            codes = daily_raw.get("weather_code") or []
            tmax = daily_raw.get("temperature_2m_max") or []
            tmin = daily_raw.get("temperature_2m_min") or []
            sunrises = daily_raw.get("sunrise") or []
            sunsets = daily_raw.get("sunset") or []
            prec_sums = daily_raw.get("precipitation_sum") or []
            prec_probs = daily_raw.get("precipitation_probability_max") or []
            wind_maxs = daily_raw.get("wind_speed_10m_max") or []
        except Exception as exc:
            raise WeatherError(f"Forecast daily fields invalid: {exc}", code="bad_payload") from exc

        daily: list[DailyWeather] = []
        for i, date in enumerate(dates):
            try:
                daily.append(DailyWeather(
                    date=str(date),
                    weather_code=int(codes[i]) if i < len(codes) and codes[i] is not None else 0,
                    temp_max=float(tmax[i]) if i < len(tmax) and tmax[i] is not None else 0.0,
                    temp_min=float(tmin[i]) if i < len(tmin) and tmin[i] is not None else 0.0,
                    sunrise=str(sunrises[i]) if i < len(sunrises) and sunrises[i] else "",
                    sunset=str(sunsets[i]) if i < len(sunsets) and sunsets[i] else "",
                    precipitation_sum=float(prec_sums[i]) if i < len(prec_sums) and prec_sums[i] is not None else None,
                    precipitation_probability_max=float(prec_probs[i]) if i < len(prec_probs) and prec_probs[i] is not None else None,
                    wind_speed_max=float(wind_maxs[i]) if i < len(wind_maxs) and wind_maxs[i] is not None else None,
                ))
            except (TypeError, ValueError, IndexError):
                continue

        return WeatherReport(location=loc, current=current, daily=tuple(daily), fetched_at=time.time())

    def format_for_model(self, report: WeatherReport) -> dict:
        """Return a compact, model-friendly dict for the tool response."""
        def _fmt_time(iso: str) -> str:
            # "2026-10-21T06:12" -> "06:12"
            if "T" in iso:
                return iso.split("T")[-1][:5]
            return iso

        cur = report.current
        loc = report.location
        loc_label = loc.name
        if loc.admin1 and loc.admin1 != loc.name:
            loc_label += f" ({loc.admin1})"
        if loc.country:
            loc_label += f", {loc.country}"

        daily_list = []
        for d in report.daily:
            daily_list.append({
                "date": d.date,
                "condition": wmo_description(d.weather_code),
                "condition_en": wmo_description(d.weather_code, "en"),
                "temp_max_c": round(d.temp_max, 1),
                "temp_min_c": round(d.temp_min, 1),
                "precipitation_sum_mm": round(d.precipitation_sum, 1) if d.precipitation_sum is not None else None,
                "precipitation_probability_max": int(d.precipitation_probability_max) if d.precipitation_probability_max is not None else None,
                "wind_speed_max_kmh": round(d.wind_speed_max, 1) if d.wind_speed_max is not None else None,
                "sunrise": _fmt_time(d.sunrise),
                "sunset": _fmt_time(d.sunset),
            })

        return {
            "location": loc_label,
            "latitude": round(loc.latitude, 4),
            "longitude": round(loc.longitude, 4),
            "timezone": loc.timezone,
            "current": {
                "time": cur.time,
                "temperature_c": round(cur.temperature, 1),
                "feels_like_c": round(cur.apparent_temperature, 1),
                "condition": wmo_description(cur.weather_code),
                "condition_en": wmo_description(cur.weather_code, "en"),
                "weather_code": cur.weather_code,
                "humidity_percent": int(cur.humidity) if cur.humidity is not None else None,
                "precipitation_mm": cur.precipitation,
                "cloud_cover_percent": int(cur.cloud_cover) if cur.cloud_cover is not None else None,
                "wind_speed_kmh": round(cur.wind_speed, 1) if cur.wind_speed is not None else None,
                "wind_direction_deg": int(cur.wind_direction) if cur.wind_direction is not None else None,
                "is_day": cur.is_day,
            },
            "daily": daily_list,
            "units": {"temperature": "celsius", "wind": "km/h", "precipitation": "mm"},
        }
