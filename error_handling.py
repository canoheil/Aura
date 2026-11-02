"""Safe, user-facing error classification for Aura.

This module deliberately has no Telegram/Gemini imports so it can be tested
offline and reused by handlers without coupling error presentation to SDKs.

Messages are localized through `i18n.t`; the language defaults to Persian so an
existing deployment keeps exactly the text it had before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from i18n import t as _t


@dataclass(frozen=True)
class UserError:
    message: str
    log_level: str = "error"


def classify_gemini_error(status_code: Optional[int], detail: str = "", lang: Optional[str] = None) -> UserError:
    """Turn a provider failure into a stable message without leaking raw API details.

    The name is historical: it now covers every model provider, so the wording
    names "the model service" rather than one vendor.
    """
    status = int(status_code) if isinstance(status_code, int) else None
    text = (detail or "").lower()

    if status == 429 or "resource exhausted" in text or "rate limit" in text:
        return UserError(_t("error.provider_rate_limited", lang), "warning")
    if status in {500, 502, 503, 504} or "unavailable" in text or "high demand" in text:
        return UserError(_t("error.provider_unavailable", lang), "warning")
    if status in {408, 504} or "timeout" in text or "timed out" in text:
        return UserError(_t("error.provider_timeout", lang), "warning")
    if status in {401, 403} or "api key" in text or "permission" in text:
        return UserError(_t("error.provider_auth", lang), "error")
    if status == 400:
        return UserError(_t("error.provider_bad_request", lang), "error")
    return UserError(_t("error.provider_generic", lang), "error")


def classify_telegram_error(error_name: str = "", detail: str = "", lang: Optional[str] = None) -> UserError:
    """Return a safe message for common Telegram failures."""
    name = (error_name or "").lower()
    text = (detail or "").lower()
    if "retryafter" in name or "retry after" in text:
        return UserError(_t("error.telegram_flood", lang), "warning")
    if "forbidden" in name or "bot was blocked" in text:
        return UserError(_t("error.telegram_forbidden", lang), "warning")
    if "badrequest" in name:
        return UserError(_t("error.telegram_bad_request", lang), "warning")
    return UserError(_t("error.telegram_generic", lang), "warning")


def generic_user_error(lang: Optional[str] = None) -> UserError:
    return UserError(_t("error.generic", lang), "error")
