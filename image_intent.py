"""Conservative intent parsing for Aura's image features.

Only explicit edit instructions are detected here. Everything else is left to
Gemini, which has the ``generate_image`` tool and Aura's full conversation
context. Keeping these rules conservative avoids hijacking ordinary vision
questions such as «این عکس چیه؟».
"""
from __future__ import annotations

import re
from typing import Optional

_IMAGE_COMMAND = re.compile(
    r"^\s*/image(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$",
    flags=re.IGNORECASE | re.DOTALL,
)

# Persian edit verbs. The optional particle handles «عوضش کن»، «حذفش کن»، ...
_FA_EDIT_ACTION = re.compile(
    r"(?:ادیت|ویرایش)\s*(?:کن|کنید|بکن|بکنید|بده|بدید)"
    r"|(?:تغییر|عوض|تبدیل|جایگزین|حذف|اضافه|اصلاح)"
    r"(?:ش|اش|شو|رو|را|و)?\s*(?:بده|بدید|کن|کنید|بکن|بکنید)"
    r"|(?:به|رو\s*به)\s+.+?\s+(?:تبدیل|تغییر)\s*(?:کن|کنید|بده|بدید)"
)

_FA_END_IMPERATIVE = re.compile(r"(?:می\s*)?(?:کن|کنی|کنید|بکن|بکنی|بکنید)\s*[.!؟?]*$")

# Visual properties that make a bare «... کن» an image edit instead of an
# ordinary request (e.g. «این عکس رو تحلیل کن» must not match).
_FA_VISUAL_CHANGE = re.compile(
    r"\b(?:رنگ|رنگش|آسمان|آسمون|پس\s*زمینه|زمینه|چهره|صورت|موی|لباس|خودرو|ماشین|"
    r"ساختمان|منظره|صحنه|نور|نورپردازی|سایه|جزئیات|کیفیت|اندازه|"
    r"بزرگ\s*تر|کوچک\s*تر|روشن\s*تر|تیره\s*تر|کارتونی|انیمه|نقاشی|آبرنگ|"
    r"رئال|واقعی|سیاه\s*و\s*سفید|قدیمی|رترو|سایبرپانک|فانتزی|برف|باران|بارون|"
    r"غروب|طلوع|شب|روز|جنگل|ساحل|دریا|کوه|فضا|کهکشان|"
    r"بنفش|قرمز|آبی|سبز|زرد|نارنجی|صورتی|طلایی|نقره\s*ای|مشکی|سفید|"
    r"شفاف|محو|واضح|بافت|استایل)\b"
)

# If the caption asks Aura to inspect/compare the photo, style words such as
# «قدیمی» must not turn it into an edit request.
_ANALYSIS_MARKERS = re.compile(
    r"تحلیل|مقایسه|توضیح|شرح|تشخیص|بررسی|نقد|بگو|ببین|چیست|چیه|کجاست|کیست|چرا|"
    r"آماده|چاپ|ارسال|بفرست|پست|ذخیره|دانلود|انتخاب|"
    r"explain|describe|analy[sz]e|compare",
    flags=re.IGNORECASE,
)

_FA_THIS_IMAGE = re.compile(r"(?:این|همین|اون|آن)\s*(?:عکس|تصویر)(?:و|رو|را)?")
_FA_IMAGE_ACTION = re.compile(r"(?:بساز|بکش|ادیت|تغییر|عوض|تبدیل|کارتونی|نقاشی|استایل)")

_EN_EDIT_ACTION = re.compile(
    r"\b(?:edit|modify|alter|change|replace|remove|add|turn|convert|"
    r"cartoonify|restyle|stylize|recolor|re-?color)\b"
    r"|\bmake\s+(?:it|this|the)\b"
)

_EN_STYLE_ONLY = re.compile(
    r"^(?:please\s+)?(?:make\s+it\s+)?"
    r"(?:cartoon|anime|painting|watercolor|realistic|black\s+and\s+white|"
    r"vintage|retro|cyberpunk|fantasy)"
    r"(?:\s+style)?\s*[.!]?$"
)


def normalize_text(text: str) -> str:
    """Normalize Persian/Arabic characters and collapse whitespace."""
    value = (text or "").replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", value).strip()


def extract_image_command_prompt(text: str) -> Optional[str]:
    """Return the prompt after ``/image`` (possibly empty), or None if absent.

    Used for photo captions because Telegram command handlers only inspect
    ``message.text``. The prompt may be empty, which callers treat as a usage
    hint rather than an error.
    """
    match = _IMAGE_COMMAND.match(text or "")
    if not match:
        return None
    return (match.group(1) or "").strip()


_FA_REQUEST_PREFIX = (
    r"(?:لطفا\s+)?(?:برام|برای\s*من|واسه\s*من|برا\s*من)?\s*"
    r"(?:یه\s+|یک\s+)?"
)
_FA_NOUN = r"(?:عکس|تصویر|نقاشی|طرح|تصویرسازی)"
_FA_GENERATE_VERB = (
    r"(?:بساز|بسازی|بکش|بکشی|درست\s*کن|درست\s*کنی|ایجاد\s*کن|"
    r"تولید\s*کن|بسازه|بکشه)"
)
_FA_PREPOSITION = r"(?:از|برای|با|در\s*مورد|درباره)"

_FA_VERB_FIRST = re.compile(
    rf"^{_FA_REQUEST_PREFIX}{_FA_NOUN}\s+{_FA_GENERATE_VERB}"
    rf"(?:\s+{_FA_PREPOSITION}\s+(?P<subject>.+))?$",
    flags=re.IGNORECASE,
)
_FA_SUBJECT_FIRST = re.compile(
    rf"^{_FA_REQUEST_PREFIX}{_FA_NOUN}\s+{_FA_PREPOSITION}\s+(?P<subject>.+?)\s+"
    rf"{_FA_GENERATE_VERB}\s*[.!؟?]*$",
    flags=re.IGNORECASE,
)
_EN_IMAGE_REQUEST = re.compile(
    r"^(?:please\s+)?(?:generate|create|make|paint)\s+(?:me\s+)?"
    r"(?:an?\s+)?(?:image|picture|photo|illustration|drawing|artwork)\s+"
    r"(?:of|showing|with|about)?\s*(?P<subject>.+)$",
    flags=re.IGNORECASE,
)
_EN_DRAW_REQUEST = re.compile(
    r"^draw\s+(?:me\s+)?(?P<subject>.+)$",
    flags=re.IGNORECASE,
)

_VAGUE_SUBJECTS = {
    "",
    "عکس",
    "تصویر",
    "نقاشی",
    "چیزی",
    "یه چیزی",
    "یک چیزی",
    "image",
    "picture",
    "photo",
    "something",
    "an image",
    "a picture",
    "a photo",
}


def extract_text_image_request(text: str) -> Optional[str]:
    """Return the described subject of an explicit text-to-image request.

    This is a hint for the model (and a testable contract), not a local
    generator: the actual call still goes through Aura's tool runtime so the
    conversation context and Aura's own wording stay intact.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None

    match = (
        _FA_SUBJECT_FIRST.match(normalized)
        or _FA_VERB_FIRST.match(normalized)
        or _EN_IMAGE_REQUEST.match(normalized)
        or _EN_DRAW_REQUEST.match(normalized)
    )
    if not match:
        return None
    subject = (match.group("subject") or "").strip(" \t.,!?؛،؟")
    if subject.casefold() in _VAGUE_SUBJECTS or len(subject) < 2:
        return None
    return subject


_FA_VAGUE_EDIT = re.compile(
    r"^(?:لطفا\s+)?"
    r"(?:(?:(?:این|همین|اون)\s*)?(?:عکس|تصویر)?\s*(?:و|رو|را)?\s*)?"
    r"(?:ادیت|ویرایش|تغییر|عوض|اصلاح)(?:ش|اش|شو|رو|را|و)?\s*"
    r"(?:کن|کنید|بکن|بکنید|بده|بدید)?\s*[.!؟?]*$"
)
_EN_VAGUE_EDIT = re.compile(
    r"^(?:please\s+)?(?:can\s+you\s+)?(?:edit|change|modify)\s+"
    r"(?:it|this(?:\s+(?:image|photo|picture))?|the\s+(?:image|photo|picture))"
    r"\s*[.!?]*$",
    flags=re.IGNORECASE,
)


def needs_edit_details(text: str) -> bool:
    """True for generic «این عکس رو ادیت کن» requests with no actual change."""
    normalized = normalize_text(text)
    if not normalized:
        return True
    return bool(_FA_VAGUE_EDIT.match(normalized) or _EN_VAGUE_EDIT.match(normalized))


def looks_like_image_edit_request(text: str) -> bool:
    """Return True only for explicit requests to modify an attached image."""
    normalized = normalize_text(text)
    if not normalized:
        return False

    lowered = normalized.lower()
    # «what changed in this photo?» is a question, not an edit instruction.
    if re.match(r"^(?:what|why|how|when|where|who)\b", lowered):
        return False

    if _FA_EDIT_ACTION.search(normalized):
        return True

    # «آسمان رو بنفش کن»، «اینو کارتونی کن»: imperative + a visual property.
    # Captions requesting analysis/comparison are excluded so a photo of an old
    # newspaper («این روزنامه قدیمی رو تحلیل کن») is not mistaken for an edit.
    if (
        _FA_END_IMPERATIVE.search(normalized)
        and _FA_VISUAL_CHANGE.search(normalized)
        and not _ANALYSIS_MARKERS.search(normalized)
    ):
        return True

    # «از این عکس یه طرح بساز»: explicit action on the attached image.
    if _FA_THIS_IMAGE.search(normalized) and _FA_IMAGE_ACTION.search(normalized):
        return True

    if _EN_EDIT_ACTION.search(lowered):
        return True
    if _EN_STYLE_ONLY.fullmatch(lowered):
        return True
    return False
