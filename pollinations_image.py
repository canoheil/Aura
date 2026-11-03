"""Pollinations.ai image generation and editing for Aura.

This module is a small, synchronous HTTP client. It deliberately has no
Telegram or Gemini imports so it can be unit-tested offline and reused from
both the tool runtime and the `/image` command path.

Endpoints used:
- ``GET  https://gen.pollinations.ai/image/{prompt}`` for text-to-image and
  image-to-image (the source image is passed with the ``image=`` parameter).
- ``POST https://media.pollinations.ai/upload`` to turn a Telegram photo into
  a public URL that Pollinations can download.

The API key is always sent in the ``Authorization`` header, never in a URL and
never included in logs or exception messages.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger("aura-pollinations")

DEFAULT_BASE_URL = "https://gen.pollinations.ai"
DEFAULT_UPLOAD_URL = "https://media.pollinations.ai/upload"
DEFAULT_IMAGE_MODEL = "flux"
DEFAULT_EDIT_MODEL = "kontext"
DEFAULT_IMAGE_FALLBACK_MODEL = "seedream"
DEFAULT_EDIT_FALLBACK_MODEL = "nanobanana"
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_PROMPT_CHARS = 1500
DEFAULT_MAX_IMAGE_BYTES = 32 * 1024 * 1024

CONNECT_TIMEOUT = 10
_MAX_ERROR_BODY_CHARS = 600
_PHOTO_MIME_TYPES = {"image/jpeg", "image/png"}
_TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024

_GENERIC_ERROR = "❌ ساخت یا ادیت عکس انجام نشد. دوباره امتحان کن."


class PollinationsError(Exception):
    """A safe, classified Pollinations failure.

    ``message`` is technical and safe to log (it never contains the API key).
    ``user_message`` is the Persian text shown to the Telegram user.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: str = "",
        user_message: str = "",
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.user_message = user_message or _GENERIC_ERROR
        self.retry_after = retry_after


@dataclass(frozen=True)
class GeneratedImage:
    """One image returned by Pollinations, ready for Telegram delivery."""

    data: bytes
    mime_type: str
    prompt: str
    model: str

    @property
    def extension(self) -> str:
        return _extension_for_mime(self.mime_type)

    @property
    def filename(self) -> str:
        return f"aura-{int(time.time())}-{secrets.token_hex(3)}.{self.extension}"

    @property
    def telegram_safe_photo(self) -> bool:
        """Telegram sendPhoto accepts JPEG/PNG under 10 MB; other formats go as files."""
        return (
            self.mime_type in _PHOTO_MIME_TYPES
            and len(self.data) <= _TELEGRAM_PHOTO_MAX_BYTES
        )


def _extension_for_mime(mime_type: str) -> str:
    mapping = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/svg+xml": "svg",
    }
    return mapping.get((mime_type or "").lower(), "jpg")


def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "image/svg+xml"
    return ""


def _normalise_image_mime(content_type: str, data: bytes) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith("image/"):
        return "image/jpeg" if mime == "image/jpg" else mime
    sniffed = _sniff_image_mime(data)
    if sniffed:
        return sniffed
    raise PollinationsError(
        "Pollinations returned a non-image response.",
        user_message="❌ سرویس تصویرسازی خروجی معتبری برنگرداند. دوباره امتحان کن.",
    )


def image_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    """Read PNG/GIF/JPEG/WebP dimensions without Pillow.

    Used only to preserve the source aspect ratio during editing; an unknown
    format simply falls back to the configured square size.
    """
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            return (width, height) if width and height else None
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            width = int.from_bytes(data[6:8], "little")
            height = int.from_bytes(data[8:10], "little")
            return (width, height) if width and height else None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                width = 1 + int.from_bytes(data[24:27], "little")
                height = 1 + int.from_bytes(data[27:30], "little")
                return (width, height) if width and height else None
            if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                width = int.from_bytes(data[26:28], "little") & 0x3FFF
                height = int.from_bytes(data[28:30], "little") & 0x3FFF
                return (width, height) if width and height else None
        if data.startswith(b"\xff\xd8"):
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    index += 2
                    continue
                segment_length = int.from_bytes(data[index + 2:index + 4], "big")
                if segment_length < 2:
                    return None
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height = int.from_bytes(data[index + 5:index + 7], "big")
                    width = int.from_bytes(data[index + 7:index + 9], "big")
                    return (width, height) if width and height else None
                index += 2 + segment_length
    except (IndexError, ValueError):
        return None
    return None


def fit_dimensions(
    width: Optional[int],
    height: Optional[int],
    *,
    max_side: int = 1024,
) -> tuple[int, int]:
    """Scale a source size down to ``max_side`` and round to multiples of 8."""
    if not width or not height or width < 1 or height < 1:
        return max_side, max_side
    scale = min(1.0, float(max_side) / float(max(width, height)))
    fitted_width = max(64, min(2048, round(width * scale / 8) * 8))
    fitted_height = max(64, min(2048, round(height * scale / 8) * 8))
    return fitted_width, fitted_height


def _friendly_error_message(
    status_code: Optional[int],
    code: str = "",
    *,
    retry_after: Optional[int] = None,
) -> str:
    """Map an upstream error to a stable Persian message without leaking details."""
    code = (code or "").strip().lower()
    status = status_code if isinstance(status_code, int) else None

    if code in {"content_blocked", "content_policy_violation"} or (
        status == 422 and "content" in code
    ):
        return "❌ این درخواست توسط فیلتر ایمنی سرویس تصویر رد شد. توضیح یا عکس را تغییر بده."
    if code in {
        "invalid_image_url",
        "failed_to_download_image",
        "image_too_large",
        "unsupported_image_media_type",
    }:
        return "❌ نتوانستم عکس ورودی را برای ادیت آماده کنم. یک عکس واضح‌تر و کوچک‌تر بفرست."
    if status == 401:
        return "❌ دسترسی Aura به سرویس تصویرسازی تنظیم نشده یا کلید آن معتبر نیست."
    if status == 402:
        return "❌ اعتبار یا سهمیه سرویس تصویرسازی تمام شده است."
    if status == 403:
        return "❌ این کلید اجازه استفاده از مدل تصویر انتخاب‌شده را ندارد."
    if status == 413:
        return "❌ عکس ورودی برای ادیت بیش از حد بزرگ است."
    if status in {400, 404, 422}:
        return "❌ درخواست ساخت/ادیت عکس قابل پردازش نبود. توضیح یا عکس را تغییر بده و دوباره امتحان کن."
    if status == 429:
        wait = f" حدود {retry_after} ثانیه" if retry_after else " کمی"
        return f"❌ سرویس تصویرسازی فعلاً شلوغ است؛{wait} بعد دوباره امتحان کن."
    if status in {500, 502, 503, 504}:
        return "❌ سرویس تصویرسازی فعلاً پاسخ‌گو نیست. کمی بعد دوباره امتحان کن."
    return _GENERIC_ERROR


class PollinationsImageClient:
    """Thin, testable wrapper around Pollinations image endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        upload_url: str = DEFAULT_UPLOAD_URL,
        image_model: str = DEFAULT_IMAGE_MODEL,
        edit_model: str = DEFAULT_EDIT_MODEL,
        image_fallback_model: str = DEFAULT_IMAGE_FALLBACK_MODEL,
        edit_fallback_model: str = DEFAULT_EDIT_FALLBACK_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        edit_max_side: int = 1024,
        upload_privacy: bool = True,
        http_get: Optional[Callable[..., requests.Response]] = None,
        http_post: Optional[Callable[..., requests.Response]] = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        if self.api_key.lower().startswith("bearer "):
            self.api_key = self.api_key[7:].strip()
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.upload_url = str(upload_url or DEFAULT_UPLOAD_URL)
        self.image_model = str(image_model or DEFAULT_IMAGE_MODEL).strip()
        self.edit_model = str(edit_model or DEFAULT_EDIT_MODEL).strip()
        self.image_fallback_model = str(image_fallback_model or "").strip()
        self.edit_fallback_model = str(edit_fallback_model or "").strip()
        self.timeout = max(20.0, float(timeout))
        self.max_prompt_chars = max(100, int(max_prompt_chars))
        self.max_image_bytes = max(1024, int(max_image_bytes))
        self.edit_max_side = max(512, min(2048, int(edit_max_side)))
        self.upload_privacy = bool(upload_privacy)
        self._http_get = http_get or requests.get
        self._http_post = http_post or requests.post

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    # ---------- public operations ----------

    def generate(
        self,
        prompt: str,
        *,
        width: int = 1024,
        height: int = 1024,
        seed: Optional[int] = None,
    ) -> GeneratedImage:
        """Create a new image from a text prompt."""
        self._require_configured()
        clean_prompt = self._clean_prompt(prompt)
        return self._request_image(
            clean_prompt,
            models=self._model_chain(self.image_model, self.image_fallback_model),
            width=width,
            height=height,
            seed=seed,
            image_url=None,
        )

    def edit(
        self,
        prompt: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> GeneratedImage:
        """Edit a user image by uploading it and passing the public URL as ``image=``.

        When width/height are omitted, the source aspect ratio is preserved and
        scaled to ``edit_max_side`` so a portrait photo is not forced into a square.
        """
        self._require_configured()
        clean_prompt = self._clean_prompt(prompt)
        if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
            raise PollinationsError(
                "Image bytes are missing.",
                user_message="❌ عکسی برای ادیت دریافت نشد.",
            )
        source_bytes = bytes(image_bytes)
        source_url = self.upload_image(source_bytes, mime_type)
        if width is None or height is None:
            source_width, source_height = image_dimensions(source_bytes) or (0, 0)
            width, height = fit_dimensions(
                source_width,
                source_height,
                max_side=self.edit_max_side,
            )
        return self._request_image(
            clean_prompt,
            models=self._model_chain(self.edit_model, self.edit_fallback_model),
            width=int(width),
            height=int(height),
            seed=seed,
            image_url=source_url,
        )

    def upload_image(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        """Upload a user image to Pollinations media storage and return its public URL."""
        self._require_configured()
        if not image_bytes:
            raise PollinationsError(
                "Upload was called with empty image bytes.",
                user_message="❌ عکسی برای آپلود دریافت نشد.",
            )
        mime = _normalise_image_mime(mime_type, image_bytes)
        filename = f"aura-source-{secrets.token_hex(4)}.{_extension_for_mime(mime)}"
        kwargs = {
            "headers": self._auth_headers(accept="application/json"),
            "files": {"file": (filename, image_bytes, mime)},
            "timeout": (CONNECT_TIMEOUT, self.timeout),
        }
        if self.upload_privacy:
            # Strip GPS/camera metadata before the photo leaves Telegram's context.
            kwargs["params"] = {"safe": "privacy"}
        try:
            response = self._http_post(self.upload_url, **kwargs)
        except requests.RequestException as exc:
            logger.warning("Pollinations upload network error | type=%s", type(exc).__name__)
            raise PollinationsError(
                f"Pollinations upload failed with network error ({type(exc).__name__}).",
                user_message="❌ ارتباط با سرویس آپلود عکس برقرار نشد. دوباره امتحان کن.",
            ) from exc

        try:
            self._raise_for_status(response, operation="upload")
            try:
                payload = response.json()
            except ValueError as exc:
                raise PollinationsError(
                    "Pollinations upload returned invalid JSON.",
                    user_message="❌ سرویس آپلود عکس پاسخ معتبری نداد. دوباره امتحان کن.",
                ) from exc
        finally:
            _close_response(response)

        url = payload.get("url") if isinstance(payload, dict) else None
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.netloc:
            raise PollinationsError(
                "Pollinations upload response did not contain a usable HTTPS URL.",
                user_message="❌ سرویس آپلود عکس آدرس معتبری برنگرداند. دوباره امتحان کن.",
            )
        logger.info(
            "Pollinations upload completed | host=%s bytes=%s mime=%s",
            parsed.netloc, len(image_bytes), mime,
        )
        return url

    # ---------- internals ----------

    def _require_configured(self) -> None:
        if not self.is_configured:
            raise PollinationsError(
                "POLLINATIONS_API_KEY is not configured.",
                user_message="❌ قابلیت ساخت عکس روی Aura فعال نیست.",
            )

    def _clean_prompt(self, prompt: str) -> str:
        text = re.sub(r"\s+", " ", str(prompt or "")).strip()
        if not text:
            raise PollinationsError(
                "Image prompt is empty.",
                user_message="❌ توضیح عکس خالی است. بگو چه تصویری می‌خواهی.",
            )
        if len(text) > self.max_prompt_chars:
            text = text[: self.max_prompt_chars].rsplit(" ", 1)[0].strip() or text[: self.max_prompt_chars]
        return text

    @staticmethod
    def _model_chain(primary: str, fallback: str) -> list[str]:
        models: list[str] = []
        for model in (primary, fallback):
            model = str(model or "").strip()
            if model and model not in models:
                models.append(model)
        return models or [DEFAULT_IMAGE_MODEL]

    def _auth_headers(self, *, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": accept,
            "User-Agent": "Aura-Telegram-Bot/1.0",
        }

    def _request_image(
        self,
        prompt: str,
        *,
        models: list[str],
        width: int,
        height: int,
        seed: Optional[int],
        image_url: Optional[str],
    ) -> GeneratedImage:
        last_error: Optional[PollinationsError] = None
        for index, model in enumerate(models):
            try:
                return self._fetch_image(
                    prompt,
                    model=model,
                    width=width,
                    height=height,
                    seed=seed,
                    image_url=image_url,
                )
            except PollinationsError as exc:
                last_error = exc
                has_next = index + 1 < len(models)
                if has_next and self._should_try_fallback(exc):
                    logger.warning(
                        "Pollinations model fallback | failed_model=%s fallback_model=%s status=%s code=%s",
                        model, models[index + 1], exc.status_code, exc.code,
                    )
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise PollinationsError("No Pollinations image model was available.")

    def _fetch_image(
        self,
        prompt: str,
        *,
        model: str,
        width: int,
        height: int,
        seed: Optional[int],
        image_url: Optional[str],
    ) -> GeneratedImage:
        endpoint = f"{self.base_url}/image/{quote(prompt, safe='')}"
        params: dict[str, object] = {
            "model": model,
            "width": max(64, int(width)),
            "height": max(64, int(height)),
        }
        if seed is not None:
            params["seed"] = int(seed)
        if image_url:
            params["image"] = image_url

        try:
            response = self._http_get(
                endpoint,
                params=params,
                headers=self._auth_headers(accept="image/*"),
                timeout=(CONNECT_TIMEOUT, self.timeout),
                stream=True,
            )
        except requests.RequestException as exc:
            logger.warning(
                "Pollinations image request network error | model=%s type=%s",
                model, type(exc).__name__,
            )
            raise PollinationsError(
                f"Pollinations image request failed with network error ({type(exc).__name__}).",
                user_message="❌ ارتباط با سرویس تصویرسازی برقرار نشد. دوباره امتحان کن.",
            ) from exc

        try:
            self._raise_for_status(response, operation="edit" if image_url else "generate")
            content_type = str(getattr(response, "headers", {}).get("Content-Type", "") or "")
            if content_type.split(";", 1)[0].strip().lower() == "application/json":
                raise self._json_error_from_success(response)

            chunks: list[bytes] = []
            total = 0
            try:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_image_bytes:
                        raise PollinationsError(
                            "Pollinations image exceeded the configured size limit.",
                            user_message="❌ حجم عکس تولیدشده بیش از حد مجاز است.",
                        )
                    chunks.append(chunk)
            except requests.RequestException as exc:
                logger.warning(
                    "Pollinations image download interrupted | model=%s type=%s",
                    model, type(exc).__name__,
                )
                raise PollinationsError(
                    f"Pollinations image download was interrupted ({type(exc).__name__}).",
                    user_message="❌ دریافت عکس از سرویس تصویرسازی ناتمام ماند. دوباره امتحان کن.",
                ) from exc
        finally:
            _close_response(response)

        data = b"".join(chunks)
        if not data:
            raise PollinationsError(
                "Pollinations returned an empty image body.",
                user_message="❌ سرویس تصویرسازی عکس خالی برگرداند. دوباره امتحان کن.",
            )
        mime = _normalise_image_mime(content_type, data)
        logger.info(
            "Pollinations image ready | model=%s mime=%s bytes=%s operation=%s",
            model, mime, len(data), "edit" if image_url else "generate",
        )
        return GeneratedImage(data=data, mime_type=mime, prompt=prompt, model=model)

    def _raise_for_status(self, response: requests.Response, *, operation: str) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if 200 <= status < 300:
            return
        code, message, retry_after = _error_details(response)
        friendly = _friendly_error_message(status, code, retry_after=retry_after)
        raise PollinationsError(
            f"Pollinations {operation} failed | status={status} code={code or '-'} | {message}",
            status_code=status,
            code=code,
            user_message=friendly,
            retry_after=retry_after,
        )

    def _json_error_from_success(self, response: requests.Response) -> PollinationsError:
        """Handle a rare HTTP 200 envelope carrying an error object."""
        code, message, retry_after = _error_details(response)
        return PollinationsError(
            f"Pollinations returned an error payload | code={code or '-'} | {message}",
            status_code=502,
            code=code,
            user_message=_friendly_error_message(502, code, retry_after=retry_after),
            retry_after=retry_after,
        )

    @staticmethod
    def _should_try_fallback(error: PollinationsError) -> bool:
        if error.status_code == 404:
            return True
        if error.status_code not in {400, 422}:
            return False
        blob = f"{error.code} {error}".lower()
        markers = (
            "model", "not found", "unsupported", "not supported", "invalid",
            "image is required", "does not support",
        )
        return any(marker in blob for marker in markers)


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _error_details(
    response: requests.Response,
) -> tuple[str, str, Optional[int]]:
    """Extract (code, message, retry_after) from Pollinations' error envelope."""
    code = ""
    message = ""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = str(error.get("message") or "")
        elif isinstance(error, str):
            message = error
        code = code or str(payload.get("code") or "")
        message = message or str(payload.get("message") or "")
    if not message:
        text = str(getattr(response, "text", "") or "")
        message = text[:_MAX_ERROR_BODY_CHARS].strip() or "upstream error"

    retry_after: Optional[int] = None
    headers = getattr(response, "headers", {}) or {}
    raw_retry = headers.get("Retry-After") or headers.get("retry-after")
    if raw_retry is not None:
        try:
            retry_after = max(0, int(float(raw_retry)))
        except (TypeError, ValueError):
            retry_after = None
    return code, message[:_MAX_ERROR_BODY_CHARS], retry_after
