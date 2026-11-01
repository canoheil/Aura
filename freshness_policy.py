"""Decide when Aura must search the live web instead of answering from memory.

Why this module exists: a language model cannot notice that its own knowledge is
stale, because it does not know what day it is. "Who is the president", "what is
the latest Python release" and "چند شده دلار" all feel answerable from memory,
and the answer that comes back is whatever was true when training ended. Asking
politely in the prompt to search "when it helps" does not fix that, so the call
is made deterministically here, where the user's exact wording is known.

The module is only a classifier: it never touches the network, the database or
Telegram. :func:`classify` returns a small :class:`Decision` and bot.py decides
what to do with it — require one search for the first agent round, add a nudge to
the prompt, or stay out of the way.

Three levels, so accuracy is not bought by burning every search credit:

``force``
    A time-dependent question about a named subject: prices and rates, news,
    releases and versions, results and standings, who currently holds a role.
    The application may require a web search before the model may answer.
``hint``
    Time-dependent wording that lacks a clear subject. The model is told to
    search unless it is certain its own answer is still correct, and may answer
    directly — nothing is paid for at this level.
``off``
    Stable knowledge, chit-chat, short follow-ups, code and translation asks, or
    a request another tool already owns (weather, memes, stickers, images).
    Searching here is pure cost.

Two ingredients make a ``force``: an *ask* — a phrase whose answer expires — and
a *subject* for the search query. Either half alone is only a hint. Keyword
matching is deliberate: the behaviour stays testable offline and does not shift
when the underlying model is swapped.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Optional, Sequence

logger = logging.getLogger("aura-freshness")

LEVEL_FORCE = "force"
LEVEL_HINT = "hint"
LEVEL_OFF = "off"

_MAX_TEXT_CHARS = 1200

def normalize(text: str) -> str:
    """Fold case, Persian spelling variants and punctuation for keyword matching."""
    value = (text or "").replace("\u200c", " ").replace("\u200b", "")
    value = value.replace("ي", "ی").replace("ك", "ک")
    value = re.sub(r"[؟?!,.،]+", " ", value)
    return re.sub(r"\s+", " ", value).strip().casefold()[:_MAX_TEXT_CHARS]


def _first_match(text: str, terms: Sequence[str]) -> Optional[str]:
    for term in terms:
        if term and term in text:
            return term
    return None


# An "ask": a phrase whose correct answer has an expiry date. Grouped only so the
# log can say which kind of freshness required the search.
_ASK_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "price",
        (
            "قیمت", "نرخ", "ارزش", "هزینه", "چنده", "چند شده", "چند است", "چقدر شده",
            "چقدر است", "قیمت روز", "exchange rate", "price", "cost", "rate of",
            "how much is", "value of", "market", "chart", "چند دلار", "چند تومان",
            "چند ریال", "چند یورو", "چند درهم", "چند لیر", "چند سنت", "dollars is",
        ),
    ),
    (
        "news",
        (
            "خبر", "اخبار", "news", "breaking", "رویداد", "واکنش", "بیانیه", "گزارش",
            "آخرین", "جدیدترین", "تازه‌ترین", "latest", "newest", "recent",
            "چی شده", "چه شده", "چی شد", "چه شد", "چه خبر", "اتفاق افتاده", "رخ داد", "وقوع",
            "جنگ", "اعتراض", "تصویب", "استعفا", "تحریم", "attack", "زلزله", "سیل",
            "دستگیر", "بازداشت", "آزاد شد", "توقف", "آغاز شد", "شروع شد", "کشته",
        ),
    ),
    (
        "release",
        (
            "نسخه", "release", "released", "releases", "version", "changelog",
            "آپدیت", "بروزرسانی", "بروز رسانی", "به‌روزرسانی", "به روز رسانی", "update",
            "patch", "beta", "عرضه", "رونمایی", "launch", "announce", "announced",
            "announcement", "end of life", "پشتیبانی", "supported",
        ),
    ),
    (
        "results",
        (
            "نتیجه", "نتایج", "score", "جدول", "standing", "standings", "رتبه",
            "rank", "ranking", "امتیاز", "برنده", "winner", "قهرمان", "champion",
            "فینال", "final", "مسابقات", "tournament", "المپیک", "olympics",
            "انتخابات", "election", "referendum", "رأی", "بلیط", "بلیت", "ticket price",
        ),
    ),
    (
        "holder",
        (
            "رئیس جمهور", "رئیس‌جمهور", "president", "نخست وزیر", "prime minister",
            "صدراعظم", "وزیر", "minister", "مدیرعامل", "ceo", "chairman", "مالک",
            "owner", "دارنده", "holder", "مربی", "head coach", "انتخاب شد", "منصوب",
            "کیه", "کی هست", "who is", "who's", "زنده است", "زنده؟",
            "از دنیا رفت", "کناره گرفت", "بازنشسته", "دادگاه", "محکوم", "آزاد شد",
        ),
    ),
)

# A subject is what the ask attaches to. Without one, "how much is it" cannot be
# turned into a query without guessing, so the model should ask back rather than
# spend a credit. These are a fast path; any content word that is not policy
# vocabulary also counts (see :func:`has_subject`).
_SUBJECT_HINTS: tuple[str, ...] = (
    "دلار", "یورو", "پوند", "رئال", "درهم", "لیر", "دینار", "تومان", "ریال", "طلا", "سکه", "نقره", "نفت",
    "گاز", "سیمان", "بورس", "سهام", "شاخص", "صندوق", "مسکن", "اجاره", "خودرو", "بنزین", "تتر", "tether",
    "بیت", "بیت کوین", "bitcoin", "btc", "اتریوم", "ethereum", "ریپل", "xrp", "سولانا", "solana", "کاردانو", "دوج",
    "bnb", "رمزارز", "crypto", "cryptocurrency", "token", "gold", "silver", "oil", "playstation", "ps5",
    "xbox", "nintendo", "steam", "epic", "warframe", "fortnite", "minecraft", "gta", "iphone", "آیفون",
    "samsung", "سامسونگ", "xiaomi", "شیائومی", "huawei", "google", "گوگل", "apple", "microsoft", "nvidia",
    "intel", "اینتل", "laptop", "گرافیک", "gpu", "cpu", "ssd", "windows", "ویندوز", "linux", "ubuntu",
    "macbook", "android", "اندروید", "ios", "telegram", "تلگرام", "whatsapp", "واتساپ", "instagram",
    "اینستا", "twitter", "youtube", "یوتیوب", "tiktok", "discord", "openai", "chatgpt", "chat gpt", "gemini",
    "claude", "deepseek", "grok", "llama", "perplexity", "github", "docker", "kubernetes", "python",
    "javascript", "typescript", "react", "django", "laravel", "flutter", "swift", "kotlin", "rust", "golang",
    "mysql", "postgres", "node.js", "app", "برنامه", "اپلیکیشن", "سایت", "سرور", "iran", "ایران", "تهران",
    "tehran", "مشهد", "اصفهان", "شیراز", "turkey", "ترکیه", "استانبول", "دبی", "امارات", "canada", "کانادا",
    "germany", "آلمان", "uk", "usa", "america", "آمریکا", "england", "انگلستان", "france", "فرانسه", "spain",
    "اسپانیا", "italy", "ایتالیا", "russia", "روسیه", "ukraine", "اوکراین", "china", "چین", "japan", "ژاپن",
    "کره", "عراق", "سوریه", "افغانستان", "پاکستان", "غزه", "فلسطین", "lebanon", "israel", "europe", "اروپا",
    "nato", "opec", "imf", "fifa", "uefa", "parliament", "senate", "congress", "دولت", "government",
    "cabinet", "bank", "بانک", "un", "union", "european", "board", "لیگ", "league", "cup", "جام",
    "championship", "باشگاه", "تیم", "team", "persepolis", "پرسپولیس", "esteghlal", "استقلال", "real madrid",
    "barcelona", "بارسلونا", "manchester", "chelsea", "arsenal", "liverpool", "juventus", "milan",
    "world cup", "جام جهانی", "olympics", "المپیک", "f1", "trump", "ترامپ", "biden", "بایدن", "putin",
    "پوتین", "musk", "elon", "netanyahu", "khamenei", "rahbar", "pezeshki", "presidency", "senator", "mayor",
)

# Words that carry no subject: grammar glue, politeness and the classifier's own
# vocabulary. A token only counts as a subject when it is none of these.
_FILLER_TOKENS: frozenset[str] = frozenset({
    "من", "تو", "ما", "شما", "این", "اون", "آن", "برای", "با", "به", "از", "که", "و", "یا",
    "در", "را", "رو", "ها", "است", "هست", "نیست", "می", "میشه", "میشود", "چگونه", "چطور",
    "چرا", "کجا", "کی", "چی", "چه", "آیا", "ایا", "لطفا", "بگو", "بده", "یه", "یک", "همه",
    "هم", "فقط", "درباره", "راجب", "راجع", "خوب", "بد", "کن", "کنم", "بکن", "باشه", "هستش",
    "حالا", "وقتی", "چون", "اگر", "اما", "ولی", "تا", "بعد", "قبل", "زیاد", "کاملا", "دقیق",
    "مگه", "آره", "نه", "مرسی", "ممنون", "قربان", "عزیز", "بابا", "چیز", "چیزی", "طور",
    # Time adverbs date a question; they never say what to look up.
    "امروز", "دیروز", "دیشب", "امشب", "فردا", "پریروز", "هنوز", "الان", "اکنون", "حالا",
    "اخیرا", "اخیراً", "فعلا", "فعلاً", "halen", "قبلا", "قبلاً", "همیشه", "هیچوقت",
    "اینروزها", "چندروزپیش", "هفتهپیش", "ماهپیش", "سالپیش", "چند", "چقدر", "چنده",
    "چندم", "چندشنبه", "روزچندم", "ساعت", "تاریخ", "الانچطور", "الان",
    # Ordinary Persian verbs and pronouns are never a search subject on their own.
    "داری", "دارم", "داره", "دارید", "داشت", "داشتم", "ندارد", "نداره", "هستی", "هستیم",
    "هستید", "بودم", "بودی", "بوده", "شده", "شدم", "شوی", "بشه", "شد", "شدی", "شدن",
    "کنم", "کنی", "کنه", "کنید", "کردم", "کردی", "کرده", "بکن", "ببینم", "ببین", "دید",
    "دیدم", "دیدی", "دیده", "برو", "بریم", "بروید", "بیا", "بیای", "آمد", "آمدم",
    "میرم", "میره", "میری", "میام", "میای", "بگیر", "بگیرم", "میتونی", "میتونم",
    "نمیدونم", "میدونی", "نمیدونی", "فکر", "لازم", "نیاز", "خوابم", "حالت", "خوبی",
    # Recalling something from the conversation is not a web-search subject.
    "یادم", "یادت", "بیار", "بیاور",
    "have", "know", "want", "need", "like", "love", "think", "got", "feel", "going",
    "wanna", "gonna", "thing", "stuff", "really", "very", "much", "soon", "maybe",
    "the", "and", "for", "are", "was", "were", "has", "have", "had", "not", "but", "you",
    "your", "with", "without", "about", "from", "this", "that", "they", "them", "then",
    "please", "could", "would", "should", "there", "their", "what", "when", "where",
    "which", "while", "does", "did", "can", "will", "just", "much", "many", "more",
    "tell", "say", "give", "show", "know", "think", "maybe", "still", "today", "now",
    "how", "who", "why", "any", "some", "into", "over", "again", "already", "even",
    "is", "it", "of", "in", "on", "to", "do", "me", "us", "he", "she", "be", "i",
})

# The user demanding a search is always obeyed, and it overrides every veto.
_EXPLICIT_SEARCH_PREFIXES: tuple[str, ...] = (
    "search:", "web:", "google:", "جستجو:", "سرچ:", "گوگل:", "جستجو کن:", "سرچ کن:",
)
_EXPLICIT_FRESH_TERMS: tuple[str, ...] = (
    "سرچ کن", "جستجو کن", "دوباره سرچ", "دوباره جستجو", "سرچ تازه", "جستجوی تازه", "یه سرچ", "پیدا کن",
    "چک کن", "مطمئن شو", "research", "look up", "find out", "google it", "search for", "search the web",
    "verify", "بدون کش", "از کش استفاده نکن", "اطلاعاتت قدیمیه", "اطلاعاتت قدیمی", "ببین چی شده", "بررسی کن",
    "بگرد", "آپدیتش کن", "تازه ش کن", "از وب", "از اینترنت", "از گوگل", "from the web", "on the internet",
)

# Another tool answers these better and cheaper than a web search would.
_OTHER_TOOL_TERMS: tuple[str, ...] = (
    "میم", "meme", "استیکر", "sticker", "هواشناسی", "آب و هوا", "آب وهوا", "وضعیت هوا", "دمای هوا",
    "هوا چطوره", "هوای چطوره", "weather", "forecast", "بارون میاد", "باران میاد", "چند درجه", "یخ میزنه",
    "یخ می زنه",
)

# Picture, sticker and voice asks: never spend a search on a drawing.
_CREATION_TERMS: tuple[str, ...] = (
    "بساز", "بکش", "طراحی کن", "لوگو", "generate image", "generate an image",
    "create an image", "make an image", "draw", "paint", "عکس بساز", "تصویر بساز",
    "فیلم بساز", "ویس بده", "voice message",
)

# Questions whose answer is the same today as five years ago: a search here is
# wasted money, so these block a forced search unless one was requested.
_STABLE_ASKS: tuple[str, ...] = (
    "معنی", "مترادف", "تلفظ", "تفاوت", "فرق", "فرمول", "قانون", "مثال", "تمرین", "ترجمه", "translate",
    "translation", "خلاصه", "improve this", "refactor", "debug", "کد بده", "کد بنویس", "کد رو", "تابع",
    "syntax", "math", "ریاضی", "محاسبه کن", "calculate", "چطور کار می کند", "چطور کار میکنه",
    "چطور کار میکند", "how does it work", "how do i write", "داستان", "شعر", "poem", "story", "معرفی کتاب",
    "بهترین فیلم", "توضیح بده", "explain", "write a function", "برنامه بنویس", "اسکریپت", "اسکریپت بنویس",
    "کد پایتون", "کد جاوا",
)

# Broad freshness vocabulary that is not worth paying for on its own.
_HINT_TERMS: tuple[str, ...] = (
    "now", "currently", "current", "right now", "at the moment", "as of", "changed", "اکنون",
    "در حال حاضر", "فعلا", "فعلاً", "جدید", "تازه", "به روز", "وضعیت", "status", "موجودی", "موجوده",
    "آیا هنوز", "هنوز", "تغییر کرده", "whether", "فعال", "غیرفعال", "کار می کنه", "باز است",
    "بسته است",
)

# The runtime already knows these: the injected date answers them, so a search
# would only cost money for a number the prompt already holds.
_LOCAL_ANSWER_TERMS: tuple[str, ...] = (
    "ساعت چنده", "ساعت چند", "ساعت چند است", "تاریخ چنده", "تاریخ چند", "روز چندم",
    "چند شنبه", "چندشنبه", "امروز چندشنبه", "چه روزی است", "what day is it",
    "what time is it", "what is today", "today's date", "چند روز مونده",
)


# The vocabulary below is matched against normalized user text, so it is
# normalized here too. Without this step a half-normalized spelling written in the
# source (a ZWNJ, a capital letter) could never match again, and the rule would
# look alive while being silently dead.
def _policy_terms(terms: Sequence[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for term in terms:
        value = normalize(term)
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


_ASK_GROUPS = tuple((name, _policy_terms(group_terms)) for name, group_terms in _ASK_GROUPS)
_SUBJECT_HINTS = _policy_terms(_SUBJECT_HINTS)
_STABLE_ASKS = _policy_terms(_STABLE_ASKS)
_OTHER_TOOL_TERMS = _policy_terms(_OTHER_TOOL_TERMS)
_CREATION_TERMS = _policy_terms(_CREATION_TERMS)
_HINT_TERMS = _policy_terms(_HINT_TERMS)
_LOCAL_ANSWER_TERMS = _policy_terms(_LOCAL_ANSWER_TERMS)
_EXPLICIT_FRESH_TERMS = _policy_terms(_EXPLICIT_FRESH_TERMS)
_EXPLICIT_SEARCH_PREFIXES = tuple(normalize(prefix) for prefix in _EXPLICIT_SEARCH_PREFIXES)
_FILLER_TOKENS = frozenset(_policy_terms(sorted(_FILLER_TOKENS)))

# Policy vocabulary is not a subject either: "قیمت" inside "قیمت چنده" says
# nothing about what should be looked up, so the classifier's own words are
# removed before a message is judged to have a subject.
_POLICY_TOKENS: frozenset[str] = frozenset(
    token
    for group in _ASK_GROUPS
    for term in group[1]
    for token in term.split()
) | frozenset(token for term in _SUBJECT_HINTS for token in term.split()) | frozenset(
    token for term in _HINT_TERMS for token in term.split()
)


@dataclass(frozen=True)
class Decision:
    """What the application should do about freshness for one user message."""

    level: str = LEVEL_OFF
    reason: str = ""
    matched: tuple[str, ...] = ()

    @property
    def should_force(self) -> bool:
        return self.level == LEVEL_FORCE

    @property
    def should_nudge(self) -> bool:
        return self.level in {LEVEL_FORCE, LEVEL_HINT}

    def as_log_fields(self) -> str:
        terms = ",".join(term for term in self.matched if term)
        return f"level={self.level} reason={self.reason or '-'} terms={terms or '-'}"


def content_tokens(text: str) -> list[str]:
    """Meaningful words left after grammar, policy vocabulary and short noise."""
    normalized = normalize(text)
    return [
        token
        for token in re.findall(r"[0-9a-zآ-ی]+", normalized)
        if len(token) >= 2 and token not in _FILLER_TOKENS and token not in _POLICY_TOKENS
    ]


def has_subject(text: str) -> bool:
    """True when the message names something a search query could be about."""
    normalized = normalize(text)
    if not normalized:
        return False
    if _first_match(normalized, _SUBJECT_HINTS):
        return True
    return bool(content_tokens(normalized))


def classify(text: str, *, extra_force_terms: Sequence[str] = ()) -> Decision:
    """Classify one user message into ``force``, ``hint`` or ``off``.

    ``extra_force_terms`` lets a deployment extend the ask vocabulary with its own
    jargon without editing this module.
    """
    normalized = normalize(text)
    if not normalized:
        return Decision()

    stripped = normalized.strip()
    explicit_prefix = stripped.startswith(_EXPLICIT_SEARCH_PREFIXES)
    explicit = _first_match(normalized, _EXPLICIT_FRESH_TERMS)

    # An explicit "search it" wins over every veto below.
    if not (explicit_prefix or explicit) and _first_match(normalized, _CREATION_TERMS):
        return Decision(LEVEL_OFF, "creation")

    if explicit_prefix:
        return Decision(LEVEL_FORCE, "explicit-prefix", (stripped.split(":", 1)[0],))
    if explicit:
        return Decision(LEVEL_FORCE, "user-requested", (explicit,))

    ask_group: Optional[str] = None
    ask_term: Optional[str] = None
    for group_name, terms in _ASK_GROUPS:
        hit = _first_match(normalized, terms)
        if hit:
            ask_group, ask_term = group_name, hit
            break
    if ask_group is None and extra_force_terms:
        hit = _first_match(normalized, tuple(extra_force_terms))
        if hit:
            ask_group, ask_term = "deployment", hit

    if ask_group is None and _first_match(normalized, _OTHER_TOOL_TERMS):
        # A dedicated tool answers this better than a search would. The veto is
        # deliberately skipped when a real ask is also present: «هوا چطوره و قیمت
        # دلار چنده» still deserves the search, and the weather tool answers the
        # other half in a later round.
        return Decision(LEVEL_OFF, "other-tool")

    if ask_group:
        stable = _first_match(normalized, _STABLE_ASKS) or _first_match(normalized, _LOCAL_ANSWER_TERMS)
        if stable and ask_group not in {"results"}:
            # "What is the price difference between Python 2 and 3" wants an
            # explanation, not a live quote.
            return Decision(LEVEL_OFF, "stable-ask", (stable,))
        if has_subject(normalized):
            return Decision(LEVEL_FORCE, ask_group, (ask_term or "",))
        return Decision(LEVEL_HINT, f"{ask_group}-thin", (ask_term or "",))

    if _first_match(normalized, _STABLE_ASKS) or _first_match(normalized, _LOCAL_ANSWER_TERMS):
        # "ساعت چنده" is answered by the clock in the prompt, not by a search.
        return Decision(LEVEL_OFF, "stable-ask")

    soft = _first_match(normalized, _HINT_TERMS)
    if soft and has_subject(normalized):
        return Decision(LEVEL_HINT, "possibly-stale", (soft,))
    return Decision(LEVEL_OFF, "stable")
