"""Meme fetching for Aura, backed by the keyless meme-api.com service.

This module is deliberately offline-safe: it never imports Telegram or Gemini
and never sends anything itself. bot.py owns the tool declaration, the delivery
queue and the vision round; this layer owns the policy and the network I/O:

* resolve a free-text topic (Persian or English) to a sensible subreddit,
* call ``GET {base}/gimme[/{subreddit}]`` for JSON metadata,
* download the actual image bytes (never a bare URL) with a hard size cap,
* refuse NSFW/spoiler posts, non-images and unknown/blocked subreddits,
* avoid repeating a meme the bot already sent recently,
* fall back to a mirror when the primary service is down,
* finish - succeed or fail - inside one total time budget, so a broken service
  can never hold a reply for minutes.

The public API is small: :class:`MemeClient`, :class:`Meme`, :class:`MemeError`,
plus :func:`looks_like_meme_request`, :func:`could_involve_a_meme` and
:func:`extract_meme_topic` which give the chat pipeline a conservative,
testable hint.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import random
import re
import secrets
import threading
import time
from typing import Optional, Sequence
from urllib.parse import urlparse

import requests


logger = logging.getLogger("aura-memes")

DEFAULT_BASE_URL = "https://meme-api.com"
# Same API surface and JSON shape, used only when the primary service fails.
DEFAULT_FALLBACK_URL = "https://meme-api.aelx.de"

DEFAULT_TIMEOUT_SECONDS = 12.0
# One deadline for the whole fetch, not only per HTTP request. A dead or
# hanging mirror must never keep a Telegram reply waiting for minutes.
DEFAULT_FETCH_BUDGET_SECONDS = 20.0
MIN_FETCH_BUDGET_SECONDS = 5.0
MAX_FETCH_BUDGET_SECONDS = 60.0
DEFAULT_MAX_BYTES = 6 * 1024 * 1024
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024
_PHOTO_MIME_TYPES = {"image/jpeg", "image/png"}
_ANIMATION_MIME_TYPES = {"image/gif"}
_SUPPORTED_MIME_TYPES = _PHOTO_MIME_TYPES | _ANIMATION_MIME_TYPES | {"image/webp"}
_MAX_TITLE_CHARS = 200
_MAX_TOPIC_CHARS = 120
_MAX_SEEN_URLS = 200
_MAX_SUBREDDIT_CANDIDATES = 3

# A meme is never worth sending more than a handful of giant requests for.
_MAX_IMAGE_BYTES_HARD_LIMIT = 10 * 1024 * 1024
_MIN_IMAGE_BYTES_HARD_LIMIT = 256 * 1024

# Never start an attempt with less time than this, and never give a single HTTP
# request less than _MIN_REQUEST_TIMEOUT: a request with a 0.05s timeout can only
# produce a pointless error.
_MIN_ATTEMPT_SECONDS = 0.75
_MIN_REQUEST_TIMEOUT = 0.5

# A broken base is not retried for every remaining candidate: if the API itself
# is unreachable or rate limiting, the mirror is the next useful step.
_ABANDON_BASE_CODES = frozenset({"unreachable", "rate_limited"})
# A missing subreddit stays missing for this base; re-rolling cannot help.
_ABANDON_CANDIDATE_CODES = frozenset({"not_found", "bad_subreddit", "not_allowed"})

DEFAULT_SUBREDDITS = ("memes", "dankmemes", "me_irl", "wholesomememes")

_USER_MESSAGE_UNAVAILABLE = "الان نتونستم میم بگیرم؛ سرویس میم جواب نداد. یه بار دیگه امتحان کن."
_USER_MESSAGE_NOT_FOUND = "برای این موضوع میمی پیدا نکردم. یه موضوع دیگه بگو تا امتحان کنم."
_USER_MESSAGE_NOT_IMAGE = "این یکی فایل تصویری نبود و نشد بفرستمش. یکی دیگه امتحان می‌کنم."
_USER_MESSAGE_TOO_LARGE = "این میم زیادی سنگین بود و نشد بفرستمش."
_USER_MESSAGE_BUDGET = "سرویس میم این بار خیلی کند بود؛ یه بار دیگه امتحان کن."
_USER_MESSAGE_BLOCKED = "از این سابردیت نمی‌تونم میم بیارم."
_USER_MESSAGE_BAD_SUBREDDIT = "اسم این سابردیت معتبر نیست."


class MemeError(Exception):
    """A meme fetch failure with a human-readable Persian message.

    ``str(exc)`` carries the short technical reason for the model's tool
    result; ``user_message`` is the phrasing Aura can reuse with the user.
    Neither contains endpoints, API details or secrets.
    """

    def __init__(self, message: str, *, code: str = "", user_message: str = _USER_MESSAGE_UNAVAILABLE) -> None:
        super().__init__(message[:300])
        self.code = code
        self.user_message = user_message


@dataclass(frozen=True)
class Meme:
    """One fetched meme, ready for Telegram delivery and for Aura's vision."""

    data: bytes
    mime_type: str
    subreddit: str
    title: str
    author: str
    source_url: str
    post_link: str
    upvotes: int
    nsfw: bool = False
    spoiler: bool = False

    @property
    def extension(self) -> str:
        return {
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
        }.get(self.mime_type, "jpg")

    @property
    def filename(self) -> str:
        return f"aura-meme-{int(time.time())}-{secrets.token_hex(3)}.{self.extension}"

    @property
    def is_animation(self) -> bool:
        return self.mime_type in _ANIMATION_MIME_TYPES

    @property
    def telegram_safe_photo(self) -> bool:
        """sendPhoto only accepts JPEG/PNG under 10 MB; GIFs go as animations."""
        return self.mime_type in _PHOTO_MIME_TYPES and len(self.data) <= TELEGRAM_PHOTO_MAX_BYTES


# ---------- topic resolution ----------

# Alias groups are matched against the normalized topic string. Longer phrases
# win over shorter ones so «برنامه نویسی» is not shadowed by a generic alias.
_TOPIC_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("گربه", "بچه گربه", "پیشی", "cat", "cats", "kitten", "kittens", "meow"), ("catmemes", "MEOW_IRL")),
    (("سگ", "توله سگ", "سگا", "dog", "dogs", "puppy", "puppies", "doge"), ("dogmemes", "dogggomemes")),
    (("پرنده", "طوطی", "جوجه", "کلاغ", "bird", "birds", "parrot", "pigeon"), ("birdmemes", "AnimalMemes")),
    (("حیوان", "حیوانات", "جانور", "animal", "animals", "pet", "pets"), ("AnimalMemes", "animalsbeingderps")),
    (("پنگوئن", "penguin", "penguins"), ("penguinmemes", "AnimalMemes")),
    (("برنامه نویسی", "کدنویسی", "کد", "دیباگ", "پایتون", "programming", "coding", "code", "developer", "python", "bug"), ("ProgrammerHumor",)),
    (("لینوکس", "linux", "arch"), ("linuxmemes",)),
    (("ریاضی", "math", "maths", "mathematics"), ("mathmemes",)),
    (("فیزیک", "physics"), ("PhysicsMemes",)),
    (("شیمی", "chemistry"), ("chemistrymemes",)),
    (("علم", "دانش", "science", "biology", "space"), ("sciencememes",)),
    (("بازی", "گیم", "game", "games", "gaming", "gamer"), ("gaming",)),
    (("انیمه", "مانگا", "anime", "manga"), ("Animemes",)),
    (("تاریخ", "history", "historical"), ("historymemes",)),
    (("جنگ ستارگان", "star wars", "starwars", "jedi"), ("PrequelMemes", "StarWarsMemes")),
    (("ارباب حلقه", "lord of the rings", "lotr", "hobbit"), ("lotrmemes",)),
    (("باب اسفنجی", "spongebob", "sponge bob"), ("BikiniBottomTwitter",)),
    (("هری پاتر", "harry potter", "hogwarts"), ("harrypottermemes",)),
    (("مارول", "marvel", "avengers"), ("marvelmemes",)),
    (("دارک", "سیاه", "ادج", "dark", "edgy", "dank"), ("dankmemes",)),
    (("بامزه", "خنده دار", "خنده‌دار", "خوشایند", "مثبت", "wholesome", "cute", "funny", "happy"), ("wholesomememes", "memes")),
    (("فوتبال", "football", "soccer", "messi", "ronaldo"), ("footballmemes", "soccer")),
    (("ماشین", "خودرو", "car", "cars", "driving"), ("carmemes", "memes")),
    (("غذا", "خوراک", "پیتزا", "food", "pizza", "cooking"), ("foodmemes", "memes")),
    (("مدرسه", "دانشگاه", "درس", "school", "college", "university", "student"), ("me_irl", "memes")),
    (("کار", "اداره", "محل کار", "office", "work", "job"), ("workmemes", "me_irl")),
    (("سیاست", "political", "politics"), ("PoliticalMemes", "memes")),
    (("دوست دختر", "دوست پسر", "رابطه", "girlfriend", "boyfriend", "relationship"), ("me_irl", "memes")),
    (("شغل", "پول", "money", "salary"), ("memes", "me_irl")),
)

_ALIAS_LOOKUP: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    sorted(
        ((alias, subreddits) for aliases, subreddits in _TOPIC_ALIASES for alias in aliases),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

_SUBREDDIT_NAME = re.compile(r"^[A-Za-z0-9_]{2,32}$")

# ---------- conservative request detection ----------

_FA_AR_LETTERS = "آ-ی"

_MEME_WORD = re.compile(
    rf"(?<![A-Za-z{_FA_AR_LETTERS}])(?:میم|میمس|میمر|memes?|dankmemes)(?![A-Za-z{_FA_AR_LETTERS}])",
    flags=re.IGNORECASE,
)
_REQUEST_VERB = re.compile(
    r"(?:بده|بدی|بدید|بدهید|بفرست|بفرسی|بفرس|بفرستید|بنداز|بیار|بیاور|بگیر|بگیرم|می دی|میدی|"
    r"send|give|show|share|drop|fetch|gimme|post)",
    flags=re.IGNORECASE,
)
_ANALYSIS_MARKER = re.compile(
    r"(?:تحلیل|توضیح|شرح|معنی|یعنی چی|چیه|چیست|explain|describe|analy[sz]e|meaning)",
    flags=re.IGNORECASE,
)
_CREATION_MARKER = re.compile(
    r"(?:بساز|درست کن|بکش|طراحی|generate|create|make me|draw)",
    flags=re.IGNORECASE,
)
_LEADING_FILLER = re.compile(
    r"^(?:لطفا|لطفاً|please|hey|آقا|اقا|یواش|فقط)\s+",
    flags=re.IGNORECASE,
)
_LEADING_MEME = re.compile(
    rf"^(?:(?:یه|یک|دو|سه|چند)\s+)?(?:میم|میمس|memes?)(?![A-Za-z{_FA_AR_LETTERS}])",
    flags=re.IGNORECASE,
)
# Short follow-ups after a meme was already sent («یکی دیگه»). They contain no
# meme keyword at all, so the advertisement switch has to name them explicitly.
_MEME_FOLLOWUP = re.compile(
    r"(?:یکی دیگه|یه دیگه|یکی دیگر|باز یکی|باز هم یکی|دوباره یکی|"
    r"another one|one more|next one|more memes?)",
    flags=re.IGNORECASE,
)
_FOLLOWUP_STRIP = " \t!?.,;:؛،؟…()[]{}«»\"'"

_TOPIC_FILLERS = {
    "لطفا", "لطفاً", "برام", "برای", "من", "واسه", "واسم", "به", "از", "درباره",
    "در", "مورد", "راجب", "راجع", "موضوع", "یه", "یک", "دو", "سه", "چند", "تا",
    "میم", "میمس", "میمر", "بده", "بدی", "بدید", "بدهید", "بفرست", "بفرسی", "بفرس",
    "بفرستید", "بنداز", "بیار", "بیاور", "بگیر", "بگیرم", "میدی", "می‌دی",
    "please", "send", "give", "show", "share", "drop", "fetch", "gimme", "post",
    "me", "a", "an", "some", "the", "meme", "memes", "about", "of", "for", "to",
    "random", "any",
}


def normalize_text(text: str) -> str:
    """Normalize Persian/Arabic characters and collapse whitespace."""
    value = (text or "").replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", value).strip()


def _contains_alias(text: str, alias: str) -> bool:
    """Boundary-aware substring check that works for Persian and English."""
    if not alias:
        return False
    if len(alias) <= 3 and alias.isascii():
        return bool(re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", text))
    if alias.isascii():
        return bool(re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", text))
    return alias in text


def resolve_topic_subreddits(topic: Optional[str]) -> tuple[str, ...]:
    """Map a free-text topic to plausible subreddits, best match first."""
    normalized = normalize_text(topic or "").lower()
    if not normalized:
        return ()
    for alias, subreddits in _ALIAS_LOOKUP:
        if _contains_alias(normalized, alias):
            return tuple(subreddits)
    # A bare subreddit name from the user ("r/ProgrammerHumor") is honoured too.
    bare = re.sub(r"^(?:r/|/r/)", "", normalized).strip("/ ")
    if _SUBREDDIT_NAME.fullmatch(bare):
        return (bare,)
    return ()


def looks_like_meme_request(text: str) -> bool:
    """True only for an explicit request to receive a meme.

    Conservative on purpose: conversations *about* memes, analysis requests,
    and "make me a meme" (an image-generation job) must not trigger fetching.
    """
    normalized = normalize_text(text)
    if not normalized:
        return False
    lowered = normalized.lower()
    if not _MEME_WORD.search(lowered):
        return False
    if _ANALYSIS_MARKER.search(lowered) or _CREATION_MARKER.search(lowered):
        return False
    if _REQUEST_VERB.search(lowered):
        return True
    return False


def could_involve_a_meme(text: str) -> bool:
    """Decide whether the fetch_meme tool should be offered for this message.

    This is the *advertisement* switch, not the request classifier. A tool that
    is declared on every turn can be called on any turn, so ordinary chat must
    never carry it. It therefore returns True only when the message actually
    mentions memes (a request, a question about one, or a joke about them) or is
    a short "another one" follow-up. Everything else - greetings, questions,
    work talk - stays completely free of the tool.
    """
    normalized = normalize_text(text)
    if not normalized:
        return False
    if _MEME_WORD.search(normalized.lower()):
        return True
    # A follow-up is a short phrase ("باز یکی دیگه", "another one please"); a long
    # sentence that merely contains one of those words is not a meme turn.
    stripped = normalized.strip(_FOLLOWUP_STRIP)
    return len(stripped) <= 40 and bool(_MEME_FOLLOWUP.search(stripped))


def extract_meme_topic(text: str) -> Optional[str]:
    """Return the requested meme topic without the request boilerplate."""
    normalized = normalize_text(text)
    if not normalized:
        return None
    without_meme_word = _MEME_WORD.sub(" ", normalized)
    tokens = []
    for token in re.split(r"\s+", without_meme_word):
        cleaned = token.strip("\"'`!?,.;:؛،؟…()[]{}«»")
        if not cleaned:
            continue
        if cleaned.lower() in _TOPIC_FILLERS:
            continue
        tokens.append(cleaned)
    topic = " ".join(tokens).strip()
    if not topic:
        return None
    return topic[:_MAX_TOPIC_CHARS]


def normalize_subreddit(value: str) -> str:
    """Strip ``r/`` and slashes from a subreddit name without validating it."""
    name = str(value or "").strip()
    name = re.sub(r"^(?:https?://(?:www\.)?reddit\.com/)?(?:r/|/r/)", "", name, flags=re.IGNORECASE)
    return name.strip("/ ")


# ---------- client ----------


def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


class MemeClient:
    """Fetch real memes from meme-api.com without an API key.

    Every failure path raises :class:`MemeError`; the tool handler converts it
    into a short function response so the model can recover in natural language.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        base_urls: Sequence[str] = (DEFAULT_BASE_URL,),
        fallback_urls: Sequence[str] = (DEFAULT_FALLBACK_URL,),
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        fetch_budget_seconds: float = DEFAULT_FETCH_BUDGET_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        allow_nsfw: bool = False,
        allowed_subreddits: Sequence[str] = (),
        default_subreddits: Sequence[str] = DEFAULT_SUBREDDITS,
        max_seen_urls: int = _MAX_SEEN_URLS,
        session: Optional[requests.Session] = None,
        randomizer: Optional[random.Random] = None,
    ) -> None:
        self._enabled = bool(enabled)
        bases: list[str] = []
        for base in list(base_urls) + list(fallback_urls):
            cleaned = str(base or "").strip().rstrip("/")
            if cleaned and cleaned not in bases:
                bases.append(cleaned)
        self._base_urls = tuple(bases)
        self._timeout = max(3.0, min(60.0, float(timeout)))
        self._fetch_budget = max(
            MIN_FETCH_BUDGET_SECONDS, min(MAX_FETCH_BUDGET_SECONDS, float(fetch_budget_seconds))
        )
        self._max_bytes = max(_MIN_IMAGE_BYTES_HARD_LIMIT, min(_MAX_IMAGE_BYTES_HARD_LIMIT, int(max_bytes)))
        self._allow_nsfw = bool(allow_nsfw)
        self._allowed_subreddits = {
            normalize_subreddit(name).lower()
            for name in allowed_subreddits
            if normalize_subreddit(name)
        }
        self._default_subreddits = tuple(
            name for name in (normalize_subreddit(item) for item in default_subreddits) if name
        ) or DEFAULT_SUBREDDITS
        self._recent_urls: deque[str] = deque(maxlen=max(16, int(max_seen_urls)))
        self._lock = threading.Lock()
        self._random = randomizer or random.Random()
        self._session = session
        self._owned_session: Optional[requests.Session] = None

    # ---------- public state ----------

    @property
    def is_enabled(self) -> bool:
        return self._enabled and bool(self._base_urls)

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def fetch_budget_seconds(self) -> float:
        return self._fetch_budget

    @property
    def allowed_subreddits(self) -> tuple[str, ...]:
        return tuple(sorted(self._allowed_subreddits))

    # ---------- session ----------

    @property
    def session(self) -> requests.Session:
        if self._session is not None:
            return self._session
        if self._owned_session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 AuraBot/1.0"
                ),
                "Accept": "application/json, image/*;q=0.9, */*;q=0.5",
            })
            self._owned_session = session
        return self._owned_session

    # ---------- candidates ----------

    def candidate_subreddits(self, *, topic: Optional[str] = None, subreddit: Optional[str] = None) -> tuple[str, ...]:
        """Return subreddits to try, in order, for this request."""
        explicit = normalize_subreddit(subreddit or "")
        if explicit:
            if not _SUBREDDIT_NAME.fullmatch(explicit):
                raise MemeError(
                    "Invalid subreddit name supplied to the meme tool.",
                    code="bad_subreddit",
                    user_message=_USER_MESSAGE_BAD_SUBREDDIT,
                )
            self._check_allowed(explicit)
            return (explicit,)

        candidates: list[str] = []
        for name in resolve_topic_subreddits(topic):
            if not _SUBREDDIT_NAME.fullmatch(name):
                continue
            if self._allowed_subreddits and name.lower() not in self._allowed_subreddits:
                continue
            if name not in candidates:
                candidates.append(name)

        fallback_pool = [name for name in self._default_subreddits if self._is_allowed(name)]
        if fallback_pool:
            drawn = self._random.choice(fallback_pool)
            if drawn not in candidates:
                candidates.append(drawn)
        if not candidates:
            raise MemeError(
                "No allowed subreddit is available for this meme request.",
                code="not_allowed",
                user_message=_USER_MESSAGE_BLOCKED,
            )
        return tuple(candidates[:_MAX_SUBREDDIT_CANDIDATES])

    def _is_allowed(self, name: str) -> bool:
        return not self._allowed_subreddits or name.lower() in self._allowed_subreddits

    def _check_allowed(self, name: str) -> None:
        if not self._is_allowed(name):
            raise MemeError(
                "Requested subreddit is outside the configured allowlist.",
                code="not_allowed",
                user_message=_USER_MESSAGE_BLOCKED,
            )

    # ---------- fetch ----------

    def fetch(
        self,
        *,
        topic: Optional[str] = None,
        subreddit: Optional[str] = None,
        attempts_per_candidate: int = 1,
        budget_seconds: Optional[float] = None,
    ) -> Meme:
        """Fetch one usable meme or raise :class:`MemeError`.

        The whole operation is bounded by one deadline (``budget_seconds``,
        default ``DEFAULT_FETCH_BUDGET_SECONDS``), not just by per-request
        timeouts, so a dead or hanging service can never keep a Telegram reply
        waiting for minutes. Inside that budget the cheap tricks are used first:

        * a failure that cannot change by asking again (unknown subreddit) is
          not retried, it moves on to the next candidate immediately,
        * an unreachable or rate-limiting base is abandoned for the mirror
          instead of being asked again for every remaining candidate,
        * a post whose image was already sent is discarded before its bytes are
          downloaded, so a duplicate costs one small JSON call instead of 6 MB.
        """
        if not self.is_enabled:
            raise MemeError(
                "Meme fetching is disabled.",
                code="disabled",
                user_message="قابلیت میم روی Aura فعال نیست.",
            )
        candidates = self.candidate_subreddits(topic=topic, subreddit=subreddit)
        attempts = max(1, min(3, int(attempts_per_candidate)))
        budget = self._fetch_budget if budget_seconds is None else float(budget_seconds)
        budget = max(_MIN_ATTEMPT_SECONDS, budget)
        deadline = time.monotonic() + budget
        last_error: Optional[MemeError] = None
        abandoned_bases: set[str] = set()
        attempts_made = 0
        exhausted = False

        for base in self._base_urls:
            if exhausted or base in abandoned_bases:
                continue
            for candidate in candidates:
                if exhausted or base in abandoned_bases:
                    break
                for _ in range(attempts):
                    remaining = deadline - time.monotonic()
                    if remaining < _MIN_ATTEMPT_SECONDS:
                        exhausted = True
                        break
                    attempts_made += 1
                    try:
                        payload = self._request_json(base, candidate, timeout=remaining)
                        image_url = payload.get("url")
                        if isinstance(image_url, str) and self._is_recent(image_url):
                            last_error = MemeError(
                                "The service returned a meme that was already sent recently.",
                                code="repeat",
                                user_message=_USER_MESSAGE_NOT_FOUND,
                            )
                            logger.info(
                                "Meme fetch repeated a recent image; skipped before download | subreddit=%s",
                                candidate,
                            )
                            continue
                        meme = self._download_meme(
                            payload,
                            expected_subreddit=candidate,
                            timeout=deadline - time.monotonic(),
                        )
                    except MemeError as exc:
                        last_error = exc
                        logger.info(
                            "Meme fetch attempt failed | base=%s subreddit=%s code=%s | %s",
                            base, candidate, exc.code or "-", exc,
                        )
                        if exc.code in _ABANDON_BASE_CODES:
                            logger.info(
                                "Meme base abandoned | base=%s code=%s", base, exc.code,
                            )
                            abandoned_bases.add(base)
                            break
                        if exc.code in _ABANDON_CANDIDATE_CODES:
                            break
                        continue
                    self._remember(meme.source_url)
                    logger.info(
                        "Meme fetched | subreddit=%s title_chars=%s bytes=%s mime=%s attempts=%s",
                        meme.subreddit, len(meme.title), len(meme.data), meme.mime_type,
                        attempts_made,
                    )
                    return meme
                if exhausted:
                    break

        if exhausted:
            logger.warning(
                "Meme fetch budget exhausted | budget=%.1fs | attempts=%s", budget, attempts_made,
            )
        raise last_error or MemeError(
            "Meme service did not return a usable meme.",
            code="budget" if exhausted else "unavailable",
            user_message=_USER_MESSAGE_BUDGET if exhausted else _USER_MESSAGE_UNAVAILABLE,
        )

    # ---------- internals ----------

    def _is_recent(self, url: str) -> bool:
        with self._lock:
            return bool(url) and url in self._recent_urls

    def _remember(self, url: str) -> None:
        if not url:
            return
        with self._lock:
            self._recent_urls.append(url)

    def _bounded_timeout(self, timeout: Optional[float]) -> float:
        """Never let one request outlive the remaining fetch budget."""
        if timeout is None:
            return self._timeout
        return max(_MIN_REQUEST_TIMEOUT, min(self._timeout, float(timeout)))

    def _request_json(
        self, base: str, subreddit: str, *, timeout: Optional[float] = None
    ) -> dict:
        url = f"{base}/gimme/{subreddit}"
        effective = self._bounded_timeout(timeout)
        try:
            response = self.session.get(
                url,
                timeout=(min(6.0, effective), effective),
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as exc:
            raise MemeError(
                f"Meme service request failed ({type(exc).__name__}).",
                code="unreachable",
                user_message=_USER_MESSAGE_UNAVAILABLE,
            ) from exc

        try:
            status = int(getattr(response, "status_code", 0) or 0)
            if status == 404:
                raise MemeError(
                    "Meme service does not know that subreddit.",
                    code="not_found",
                    user_message=_USER_MESSAGE_NOT_FOUND,
                )
            if status == 429:
                raise MemeError(
                    "Meme service is rate limiting requests.",
                    code="rate_limited",
                    user_message=_USER_MESSAGE_UNAVAILABLE,
                )
            if status != 200:
                raise MemeError(
                    f"Meme service returned HTTP {status}.",
                    code="upstream",
                    user_message=_USER_MESSAGE_UNAVAILABLE,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise MemeError(
                    "Meme service returned invalid JSON.",
                    code="bad_json",
                    user_message=_USER_MESSAGE_UNAVAILABLE,
                ) from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        if not isinstance(payload, dict):
            raise MemeError(
                "Meme service returned an unexpected payload.",
                code="bad_payload",
                user_message=_USER_MESSAGE_UNAVAILABLE,
            )
        # The batch form (count > 1) is never requested, but tolerate it.
        if isinstance(payload.get("memes"), list) and payload["memes"]:
            first = payload["memes"][0]
            if isinstance(first, dict):
                return first
        return payload

    def _download_meme(
        self, payload: dict, *, expected_subreddit: str, timeout: Optional[float] = None
    ) -> Meme:
        if payload.get("nsfw") is True and not self._allow_nsfw:
            raise MemeError("Meme was marked NSFW.", code="nsfw", user_message=_USER_MESSAGE_NOT_FOUND)
        if payload.get("spoiler") is True:
            raise MemeError("Meme was marked as a spoiler.", code="spoiler", user_message=_USER_MESSAGE_NOT_FOUND)

        image_url = payload.get("url")
        if not isinstance(image_url, str) or not image_url.startswith(("http://", "https://")):
            raise MemeError("Meme payload had no direct image URL.", code="no_url", user_message=_USER_MESSAGE_NOT_IMAGE)
        parsed = urlparse(image_url)
        if not parsed.netloc or _looks_like_video_url(parsed):
            raise MemeError("Meme URL points to a video, not an image.", code="not_image", user_message=_USER_MESSAGE_NOT_IMAGE)

        data, mime_type = self._download_bytes(image_url, timeout=timeout)
        if not mime_type:
            raise MemeError("Downloaded file was not a recognised image.", code="not_image", user_message=_USER_MESSAGE_NOT_IMAGE)
        if mime_type not in _SUPPORTED_MIME_TYPES:
            raise MemeError(f"Unsupported meme image type: {mime_type}.", code="unsupported_type", user_message=_USER_MESSAGE_NOT_IMAGE)

        return Meme(
            data=data,
            mime_type=mime_type,
            subreddit=_clean_meta(payload.get("subreddit")) or expected_subreddit,
            title=_clean_meta(payload.get("title"))[:_MAX_TITLE_CHARS],
            author=_clean_meta(payload.get("author"))[:64],
            source_url=image_url,
            post_link=_clean_meta(payload.get("postLink"))[:300],
            upvotes=_coerce_int(payload.get("ups")),
            nsfw=bool(payload.get("nsfw")),
            spoiler=bool(payload.get("spoiler")),
        )

    def _download_bytes(self, image_url: str, *, timeout: Optional[float] = None) -> tuple[bytes, str]:
        effective = self._bounded_timeout(timeout)
        try:
            response = self.session.get(
                image_url,
                timeout=(min(6.0, effective), effective),
                stream=True,
                headers={"Accept": "image/*,*/*;q=0.5"},
            )
        except requests.RequestException as exc:
            raise MemeError(
                f"Meme image download failed ({type(exc).__name__}).",
                # A different code from the API being unreachable: this one is
                # about the image host for one post, not about the base itself.
                code="image_unreachable",
                user_message=_USER_MESSAGE_UNAVAILABLE,
            ) from exc

        try:
            status = int(getattr(response, "status_code", 0) or 0)
            if status != 200:
                raise MemeError(
                    f"Meme image host returned HTTP {status}.",
                    code="image_http",
                    user_message=_USER_MESSAGE_UNAVAILABLE,
                )
            chunks: list[bytes] = []
            total = 0
            iterator = response.iter_content(chunk_size=65536)
            for chunk in iterator:
                if not chunk:
                    continue
                total += len(chunk)
                if total > self._max_bytes:
                    raise MemeError(
                        f"Meme image exceeded the {self._max_bytes} byte limit.",
                        code="too_large",
                        user_message=_USER_MESSAGE_TOO_LARGE,
                    )
                chunks.append(bytes(chunk))
            data = b"".join(chunks)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        if not data:
            raise MemeError("Meme image download was empty.", code="empty", user_message=_USER_MESSAGE_NOT_IMAGE)
        # Trust the magic bytes, never the extension or the JSON-declared type.
        mime_type = _sniff_image_mime(data)
        return data, mime_type


def _looks_like_video_url(parsed) -> bool:
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if path.endswith((".mp4", ".webm", ".mov", ".gifv", ".m3u8")):
        return True
    return host in {"v.redd.it", "youtube.com", "www.youtube.com", "youtu.be", "streamable.com", "gfycat.com"}


def _clean_meta(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _coerce_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
