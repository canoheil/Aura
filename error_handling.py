"""Safe, user-facing error classification for Aura.

This module deliberately has no Telegram/Gemini imports so it can be tested
offline and reused by handlers without coupling error presentation to SDKs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class UserError:
    message: str
    log_level: str = "error"


def classify_gemini_error(status_code: Optional[int], detail: str = "") -> UserError:
    """Turn a Gemini failure into a stable Persian message without leaking raw API details."""
    status = int(status_code) if isinstance(status_code, int) else None
    text = (detail or "").lower()

    if status == 429 or "resource exhausted" in text or "rate limit" in text:
        return UserError("❌ درخواست‌ها به Gemini موقتاً زیاد شده. چند لحظه بعد دوباره امتحان کن.", "warning")
    if status in {500, 502, 503, 504} or "unavailable" in text or "high demand" in text:
        return UserError("❌ سرویس Gemini فعلاً پاسخ‌گو نیست. چند لحظه بعد دوباره امتحان کن.", "warning")
    if status in {408, 504} or "timeout" in text or "timed out" in text:
        return UserError("❌ ارتباط با Gemini بیش از حد طول کشید. دوباره امتحان کن.", "warning")
    if status in {401, 403} or "api key" in text or "permission" in text:
        return UserError("❌ دسترسی Aura به سرویس Gemini مشکل دارد.", "error")
    if status == 400:
        return UserError("❌ درخواست Aura به Gemini قابل پردازش نبود.", "error")
    return UserError("❌ در ارتباط با Gemini خطایی رخ داد. لطفاً دوباره امتحان کن.", "error")


def classify_telegram_error(error_name: str = "", detail: str = "") -> UserError:
    """Return a safe message for common Telegram failures."""
    name = (error_name or "").lower()
    text = (detail or "").lower()
    if "retryafter" in name or "retry after" in text:
        return UserError("❌ تلگرام موقتاً محدودیت ارسال اعمال کرده. کمی بعد دوباره امتحان کن.", "warning")
    if "forbidden" in name or "bot was blocked" in text:
        return UserError("❌ Aura اجازه ارسال این پیام را در تلگرام ندارد.", "warning")
    if "badrequest" in name:
        return UserError("❌ تلگرام نتوانست این پیام را پردازش کند.", "warning")
    return UserError("❌ ارتباط Aura با تلگرام با مشکل مواجه شد. دوباره امتحان کن.", "warning")


def generic_user_error() -> UserError:
    return UserError("❌ یک خطای غیرمنتظره رخ داد. دوباره امتحان کن.", "error")
