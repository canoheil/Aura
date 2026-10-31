"""End-to-end handler coverage with fake Telegram and fake Gemini clients."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from ephemeral_context import ExpiringContext
from test_agent_tool_response import (
    IMAGE, isolated_bot, _declared_names, _telegram_update, _text_response,
)


def _inline_part(client, request_index=-1):
    return client.models.requests[request_index][1][-1].parts[-1].inline_data


def test_direct_image_document_uses_vision_not_document_extractor(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("یک عکس است.")]
    update, context, file = _telegram_update(document=True)
    update.message.document = SimpleNamespace(file_id="image-doc", file_size=len(IMAGE), file_name="photo.jpg", mime_type="image/jpeg")
    asyncio.run(bot.message_handler(update, context))
    assert _inline_part(client).data == IMAGE
    assert _inline_part(client).mime_type == "image/jpeg"
    assert "file_analyze" not in _declared_names(client.models.requests[0][2])
    assert file.saved_path is None
    context.bot.get_file.assert_awaited_once_with("image-doc")


@pytest.mark.parametrize("field,mime", [("voice", "audio/ogg"), ("audio", "audio/mpeg"), ("video", "video/mp4"), ("video_note", "video/mp4"), ("animation", "video/mp4")])
def test_direct_audio_and_video_reach_gemini_inline(isolated_bot, field, mime):
    bot, client = isolated_bot
    client.models.responses = [_text_response("رسانه را بررسی کردم.")]
    update, context, _ = _telegram_update()
    update.message.text = None
    setattr(update.message, field, SimpleNamespace(file_id="media-id", file_size=42, mime_type=mime))
    asyncio.run(bot.message_handler(update, context))
    assert _inline_part(client).mime_type == mime
    assert _inline_part(client).data == IMAGE  # fake bytes: this tests wiring, not decoding
    context.bot.get_file.assert_awaited_once_with("media-id")


@pytest.mark.parametrize("field,details", [
    ("voice", {"duration": 2}),
    ("audio", {"duration": 2}),
    ("video", {"duration": 2, "width": 10, "height": 10}),
    ("video_note", {"duration": 2, "length": 10}),
    ("animation", {"duration": 2, "width": 10, "height": 10}),
])
def test_registered_telegram_filter_accepts_direct_media(isolated_bot, monkeypatch, field, details):
    from telegram import Update
    from telegram.ext import MessageHandler
    bot, _ = isolated_bot
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "123456:dummy")
    application = bot.build_application()
    handler = next(h for h in application.handlers[0] if isinstance(h, MessageHandler))
    media = {"file_id": "file-id", "file_unique_id": "unique", "file_size": 42, **details}
    raw = {"update_id": 1, "message": {"message_id": 1, "date": 0,
           "chat": {"id": 123, "type": "private"},
           "from": {"id": 123, "is_bot": False, "first_name": "Test"}, field: media}}
    assert handler.check_update(Update.de_json(raw, application.bot))


def test_recent_photo_followup_is_scoped_and_redownloaded(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("عکس دریافت شد."), _text_response("لباسش آبی است.")]
    first, context, _ = _telegram_update()
    first.message.text = None
    first.message.photo = [SimpleNamespace(file_id="last-photo", file_size=len(IMAGE))]
    second, second_context, _ = _telegram_update()
    second.message.text = "رنگ لباسش چیه؟"
    asyncio.run(bot.message_handler(first, context))
    asyncio.run(bot.message_handler(second, second_context))
    second_context.bot.get_file.assert_awaited_once_with("last-photo")
    assert _inline_part(client).data == IMAGE
    assert bot._recent_image_context.get((123, 123))["file_id"] == "last-photo"
    assert bot._recent_image_context.get((123, 999)) is None
    assert bot._recent_image_context.get((999, 123)) is None


def test_group_photo_followup_cannot_leak_to_another_user(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("عکس دیده شد."), _text_response("سؤال دوم"), _text_response("سؤال اصلی")]
    first, context, _ = _telegram_update()
    first.effective_chat.id, first.effective_chat.type = -100, "supergroup"
    first.message.text = None
    first.message.caption = "آورا این عکس چیه؟"
    first.message.photo = [SimpleNamespace(file_id="group-photo", file_size=len(IMAGE))]
    asyncio.run(bot.message_handler(first, context))

    other, other_context, _ = _telegram_update()
    other.effective_chat.id, other.effective_chat.type = -100, "supergroup"
    other.effective_user.id = 456
    other.message.text = "آورا رنگش چیه؟"
    asyncio.run(bot.message_handler(other, other_context))
    other_context.bot.get_file.assert_not_awaited()
    assert all(part.inline_data is None for part in client.models.requests[1][1][-1].parts)

    original, original_context, _ = _telegram_update()
    original.effective_chat.id, original.effective_chat.type = -100, "supergroup"
    original.message.text = "آورا رنگش چیه؟"
    asyncio.run(bot.message_handler(original, original_context))
    original_context.bot.get_file.assert_awaited_once_with("group-photo")
    assert _inline_part(client).data == IMAGE


@pytest.mark.parametrize("text", ["سلام، وضعیت کار چطور پیش میره؟", "اینترنت چطور کار می‌کند؟", "هوای این شهر چطوره؟"])
def test_unrelated_message_does_not_attach_recent_photo(isolated_bot, text):
    bot, client = isolated_bot
    client.models.responses = [_text_response("جواب متنی")]
    bot._recent_image_context.put((123, 123), {"file_id": "photo", "mime_type": "image/jpeg", "file_size": 100})
    update, context, _ = _telegram_update()
    update.message.text = text
    asyncio.run(bot.message_handler(update, context))
    context.bot.get_file.assert_not_awaited()
    assert all(part.inline_data is None for part in client.models.requests[0][1][-1].parts)


def test_too_large_and_unsupported_direct_media_report_specific_error(isolated_bot):
    bot, client = isolated_bot
    update, context, _ = _telegram_update()
    update.message.text = None
    update.message.voice = SimpleNamespace(file_id="big", file_size=bot.MAX_INLINE_MEDIA_BYTES + 1, mime_type="audio/ogg")
    asyncio.run(bot.message_handler(update, context))
    context.bot.get_file.assert_not_awaited()
    assert "حداکثر" in update.message.reply_text.await_args.args[0]
    assert client.models.requests == []

    update, context, _ = _telegram_update()
    update.message.text = None
    update.message.video = SimpleNamespace(file_id="unknown", file_size=10, mime_type="video/x-unknown")
    asyncio.run(bot.message_handler(update, context))
    context.bot.get_file.assert_not_awaited()
    assert "فرمت ویدیو" in update.message.reply_text.await_args.args[0]


def test_context_ttl_limit_and_new_command(isolated_bot):
    clock = [0.0]
    ctx = ExpiringContext(10, max_entries=2, clock=lambda: clock[0])
    ctx.put((1, 1), "a")
    ctx.put((1, 2), "b")
    ctx.put((2, 1), "c")
    assert ctx.get((1, 1)) is None
    clock[0] = 11
    assert ctx.get((1, 2)) is None
    bot, _ = isolated_bot
    bot._recent_image_context.put((123, 123), {"file_id": "old"})
    update, context, _ = _telegram_update()
    asyncio.run(bot.new_command(update, context))
    assert bot._recent_image_context.get((123, 123)) is None


def test_html_fallback_preserves_code_indentation_and_markers(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("```python\ndef __init__():\n    return [1]\n```")]
    update, context, _ = _telegram_update()
    update.message.text = "کد را بنویس"
    update.message.reply_text = AsyncMock(side_effect=[BadRequest("invalid html"), None])
    asyncio.run(bot.message_handler(update, context))
    fallback = update.message.reply_text.await_args_list[-1].args[0]
    assert fallback == "def __init__():\n    return [1]"


def test_failed_photo_and_document_fallback_notifies_user(isolated_bot):
    bot, _ = isolated_bot
    update, _, _ = _telegram_update()
    update.message.reply_photo = AsyncMock(side_effect=BadRequest("bad photo"))
    update.message.reply_document = AsyncMock(side_effect=BadRequest("bad document"))
    image = SimpleNamespace(data=IMAGE, mime_type="image/jpeg", filename="x.jpg", telegram_safe_photo=True)
    asyncio.run(bot._deliver_generated_images(update, [image]))
    update.message.reply_photo.assert_awaited_once()
    update.message.reply_document.assert_awaited_once()
    assert "نتوانست" in update.message.reply_text.await_args.args[0]
