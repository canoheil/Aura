"""Offline tests for Pollinations integration and image intent parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from image_intent import (
    extract_image_command_prompt,
    extract_text_image_request,
    looks_like_image_edit_request,
    needs_edit_details,
)
from pollinations_image import (
    GeneratedImage,
    PollinationsError,
    PollinationsImageClient,
    fit_dimensions,
    image_dimensions,
)

JPEG_BYTES = b"\xff\xd8\xff\xe0" + (b"0" * 256) + b"\xff\xd9"


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict | None = None,
        content: bytes = b"",
        json_data=None,
        text: str = "",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self._json_data = json_data
        self.text = text
        self._chunks = chunks
        self.closed = False

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data

    def iter_content(self, chunk_size: int = 65536):
        if self._chunks is not None:
            yield from self._chunks
            return
        yield self.content

    def close(self) -> None:
        self.closed = True


def _jpeg_response() -> FakeResponse:
    return FakeResponse(headers={"Content-Type": "image/jpeg"}, content=JPEG_BYTES)


def test_generate_builds_authenticated_pollinations_request():
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _jpeg_response()

    client = PollinationsImageClient("sk_test_key", http_get=fake_get)
    image = client.generate("a cat in space", width=1280, height=720)

    assert isinstance(image, GeneratedImage)
    assert image.data == JPEG_BYTES
    assert image.mime_type == "image/jpeg"
    url, kwargs = calls[0]
    assert url.startswith("https://gen.pollinations.ai/image/")
    assert "a%20cat%20in%20space" in url
    assert kwargs["params"]["model"] == "flux"
    assert kwargs["params"]["width"] == 1280
    assert kwargs["params"]["height"] == 720
    assert kwargs["headers"]["Authorization"] == "Bearer sk_test_key"
    assert kwargs["stream"] is True


def test_generate_falls_back_when_primary_model_is_rejected():
    models = []

    def fake_get(url, **kwargs):
        models.append(kwargs["params"]["model"])
        if len(models) == 1:
            return FakeResponse(
                status_code=400,
                headers={"Content-Type": "application/json"},
                json_data={
                    "status": 400,
                    "success": False,
                    "error": {"code": "BAD_REQUEST", "message": "invalid model: flux"},
                },
            )
        return _jpeg_response()

    client = PollinationsImageClient(
        "sk_test_key",
        image_model="flux",
        image_fallback_model="seedream",
        http_get=fake_get,
    )
    image = client.generate("a lighthouse")

    assert models == ["flux", "seedream"]
    assert image.model == "seedream"


def test_edit_uploads_source_image_then_passes_public_url():
    uploads = []
    requests_seen = []

    def fake_post(url, **kwargs):
        uploads.append((url, kwargs))
        return FakeResponse(
            headers={"Content-Type": "application/json"},
            json_data={
                "id": "abc",
                "url": "https://media.pollinations.ai/abc",
                "contentType": "image/jpeg",
                "size": len(JPEG_BYTES),
            },
        )

    def fake_get(url, **kwargs):
        requests_seen.append((url, kwargs))
        return _jpeg_response()

    client = PollinationsImageClient(
        "sk_test_key",
        edit_model="kontext",
        http_post=fake_post,
        http_get=fake_get,
    )
    image = client.edit("make the sky purple", JPEG_BYTES, "image/jpeg")

    assert image.mime_type == "image/jpeg"
    upload_url, upload_kwargs = uploads[0]
    assert upload_url == "https://media.pollinations.ai/upload"
    assert upload_kwargs["headers"]["Authorization"] == "Bearer sk_test_key"
    assert upload_kwargs["params"] == {"safe": "privacy"}
    assert upload_kwargs["files"]["file"][1] == JPEG_BYTES
    assert requests_seen[0][1]["params"]["image"] == "https://media.pollinations.ai/abc"
    assert requests_seen[0][1]["params"]["model"] == "kontext"


def _jpeg_with_dimensions(width: int, height: int) -> bytes:
    app0_payload = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    app0 = b"\xff\xe0" + (len(app0_payload) + 2).to_bytes(2, "big") + app0_payload
    sof_payload = (
        b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    sof = b"\xff\xc0" + (len(sof_payload) + 2).to_bytes(2, "big") + sof_payload
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def test_image_dimensions_and_fit_helpers():
    jpeg = _jpeg_with_dimensions(1600, 1200)
    assert image_dimensions(jpeg) == (1600, 1200)
    assert fit_dimensions(1600, 1200, max_side=1024) == (1024, 768)
    assert fit_dimensions(1200, 1600, max_side=1024) == (768, 1024)
    assert fit_dimensions(0, 0, max_side=1024) == (1024, 1024)


def test_edit_preserves_source_aspect_ratio():
    source = _jpeg_with_dimensions(800, 1200)
    seen = {}

    def fake_post(url, **kwargs):
        return FakeResponse(
            headers={"Content-Type": "application/json"},
            json_data={"url": "https://media.pollinations.ai/abc"},
        )

    def fake_get(url, **kwargs):
        seen.update(kwargs["params"])
        return _jpeg_response()

    client = PollinationsImageClient(
        "sk_test_key",
        http_post=fake_post,
        http_get=fake_get,
        edit_max_side=1024,
    )
    client.edit("make it purple", source, "image/jpeg")

    assert seen["width"] == 680
    assert seen["height"] == 1024


def test_edit_upload_failure_is_user_friendly():
    def fake_post(url, **kwargs):
        return FakeResponse(
            status_code=413,
            headers={"Content-Type": "application/json"},
            json_data={"status": 413, "success": False, "error": {"message": "too large"}},
        )

    client = PollinationsImageClient("sk_test_key", http_post=fake_post)
    with pytest.raises(PollinationsError) as caught:
        client.edit("change the background", JPEG_BYTES)

    assert caught.value.status_code == 413
    assert "بزرگ" in caught.value.user_message


def test_upload_rejects_non_https_url():
    def fake_post(url, **kwargs):
        return FakeResponse(
            headers={"Content-Type": "application/json"},
            json_data={"url": "http://insecure.example/x.jpg"},
        )

    client = PollinationsImageClient("sk_test_key", http_post=fake_post)
    with pytest.raises(PollinationsError):
        client.upload_image(JPEG_BYTES)


def test_missing_key_is_reported_without_leaking_it():
    client = PollinationsImageClient("")
    with pytest.raises(PollinationsError) as caught:
        client.generate("anything")

    assert "فعال نیست" in caught.value.user_message


def test_auth_error_does_not_include_the_api_key():
    secret = "sk_very_secret_value"

    def fake_get(url, **kwargs):
        return FakeResponse(
            status_code=401,
            headers={"Content-Type": "application/json"},
            json_data={
                "status": 401,
                "success": False,
                "error": {"code": "UNAUTHORIZED", "message": "Missing or invalid API key"},
            },
        )

    client = PollinationsImageClient(secret, http_get=fake_get)
    with pytest.raises(PollinationsError) as caught:
        client.generate("a cat")

    error = caught.value
    assert error.status_code == 401
    assert "دسترسی" in error.user_message
    assert secret not in str(error)


def test_rate_limit_honours_retry_after():
    def fake_get(url, **kwargs):
        return FakeResponse(
            status_code=429,
            headers={"Content-Type": "application/json", "Retry-After": "7"},
            json_data={
                "status": 429,
                "success": False,
                "error": {"code": "RATE_LIMITED", "message": "slow down"},
            },
        )

    client = PollinationsImageClient("sk_test_key", http_get=fake_get)
    with pytest.raises(PollinationsError) as caught:
        client.generate("a cat")

    assert caught.value.retry_after == 7
    assert "7" in caught.value.user_message


def test_content_policy_error_is_classified():
    def fake_get(url, **kwargs):
        return FakeResponse(
            status_code=422,
            headers={"Content-Type": "application/json"},
            json_data={
                "status": 422,
                "success": False,
                "error": {"code": "content_policy_violation", "message": "blocked"},
            },
        )

    client = PollinationsImageClient("sk_test_key", http_get=fake_get)
    with pytest.raises(PollinationsError) as caught:
        client.generate("something blocked")

    assert "فیلتر ایمنی" in caught.value.user_message


def test_non_image_success_response_is_rejected():
    def fake_get(url, **kwargs):
        return FakeResponse(headers={"Content-Type": "text/html"}, content=b"<html>oops</html>")

    client = PollinationsImageClient("sk_test_key", http_get=fake_get)
    with pytest.raises(PollinationsError) as caught:
        client.generate("a cat")

    assert "خروجی معتبری" in caught.value.user_message


def test_empty_prompt_and_empty_image_are_rejected():
    client = PollinationsImageClient("sk_test_key")
    with pytest.raises(PollinationsError):
        client.generate("   ")
    with pytest.raises(PollinationsError):
        client.edit("change this", b"")


def test_prompt_is_truncated_to_configured_limit():
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return _jpeg_response()

    client = PollinationsImageClient("sk_test_key", max_prompt_chars=100, http_get=fake_get)
    client.generate("x" * 500)

    assert len(seen["url"]) < 500


def test_generated_image_filename_matches_mime():
    jpeg = GeneratedImage(JPEG_BYTES, "image/jpeg", "p", "flux")
    svg = GeneratedImage(b"<svg></svg>", "image/svg+xml", "p", "flux")
    assert jpeg.filename.endswith(".jpg")
    assert svg.filename.endswith(".svg")
    assert jpeg.telegram_safe_photo is True
    assert svg.telegram_safe_photo is False


# ---------- intent parsing ----------


@pytest.mark.parametrize(
    "text",
    [
        "آسمان رو بنفش کن",
        "پس‌زمینه رو جنگل کن",
        "اینو کارتونی کن",
        "این عکس رو ادیت کن",
        "این عکس رو بزرگ‌تر کن",
        "میشه این عکس رو نقاشی کنی؟",
        "make the sky purple",
        "turn this into a cartoon",
        "can you edit this photo?",
        "cartoon",
    ],
)
def test_edit_intent_positives(text):
    assert looks_like_image_edit_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "این عکس چیه؟",
        "این عکس رو تحلیل کن",
        "این عکس رو ببین",
        "این عکس قشنگه",
        "what is in this photo?",
        "describe this picture",
        "این روزنامه قدیمی رو تحلیل کن",
        "این عکس رو با یه عکس قدیمی مقایسه کن",
        "این عکس رو برای شب عید آماده کن",
        "این عکس ماشین رو بررسی کن",
        "what changed in this photo?",
        "why did the color change?",
    ],
)
def test_edit_intent_negatives(text):
    assert looks_like_image_edit_request(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "اینو ادیت کن",
        "این عکس رو تغییر بده",
        "عکس رو تغییر بده",
        "ادیت کن",
        "can you edit this photo?",
        "",
    ],
)
def test_vague_edit_requests_need_details(text):
    assert needs_edit_details(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "آسمان رو بنفش کن",
        "این عکس رو بزرگ‌تر کن",
        "این عکس رو ادیت کن و آسمان را بنفش کن",
        "make the sky purple",
    ],
)
def test_specific_edit_requests_do_not_need_details(text):
    assert needs_edit_details(text) is False


@pytest.mark.parametrize(
    ("text", "subject"),
    [
        ("عکس بساز از یک گربه در فضا", "یک گربه در فضا"),
        ("یه تصویر از غروب دریا بکش", "غروب دریا"),
        ("برام یه عکس بساز از جنگل", "جنگل"),
        ("عکس از گربه بساز", "گربه"),
        ("make me an image of a cat", "a cat"),
        ("draw a robot", "a robot"),
    ],
)
def test_explicit_text_image_request_extraction(text, subject):
    assert extract_text_image_request(text) == subject


@pytest.mark.parametrize(
    "text",
    [
        "",
        "عکس بساز",
        "یه عکس می‌خوام",
        "میتونی عکس بسازی؟",
        "این عکس چیه؟",
        "what is in this photo?",
    ],
)
def test_non_explicit_text_image_requests_are_left_to_gemini(text):
    assert extract_text_image_request(text) is None


def test_image_command_prompt_parsing():
    assert extract_image_command_prompt("/image a cat in space") == "a cat in space"
    assert extract_image_command_prompt("/image@AuraBot a cat") == "a cat"
    assert extract_image_command_prompt("/image") == ""
    assert extract_image_command_prompt("please /image a cat") is None
    assert extract_image_command_prompt("عکس بساز از گربه") is None


# ---------- bot wiring ----------


def test_bot_wires_image_tool_command_and_delivery():
    source = Path("bot.py").read_text(encoding="utf-8")
    assert 'name="generate_image"' in source
    assert "def _tool_generate_image(" in source
    assert 'CommandHandler("image", image_command)' in source
    assert "extract_image_command_prompt" in source
    assert "extract_text_image_request" in source
    assert "looks_like_image_edit_request" in source
    assert "needs_edit_details" in source
    assert "_deliver_generated_images(update, image_delivery)" in source
    assert "image_delivery" in source
    assert "POLLINATIONS_API_KEY" in source


def test_pollinations_client_is_injected_only_when_configured():
    source = Path("bot.py").read_text(encoding="utf-8")
    assert "if pollinations_client.is_configured:" in source
    assert "if pollinations_client.is_configured:\n        registry.register(" in source
