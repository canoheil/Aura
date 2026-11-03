"""Reading stickers: which file is sent, how it is described, and the handlers."""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import sticker_vision
from sticker_vision import StickerPlan, describe_for_model, plan_sticker, sniff_image_mime
from test_agent_tool_response import _telegram_update, _text_response

PERSIAN = re.compile("[\u0600-\u06FF]")

# Minimal but correctly signed payloads: the MIME is sniffed from these bytes.
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24


def _static(file_id="sticker-id", **overrides):
    fields = dict(
        file_id=file_id, is_animated=False, is_video=False, emoji="😹",
        set_name="Cats", thumbnail=None, file_size=len(WEBP),
    )
    return SimpleNamespace(**{**fields, **overrides})


def _animated(thumb="thumb-id", **overrides):
    fields = dict(
        file_id="tgs-id", is_animated=True, is_video=False, emoji="🎉",
        set_name="Party", thumbnail=SimpleNamespace(file_id=thumb, file_size=len(WEBP)),
        file_size=9000,
    )
    return SimpleNamespace(**{**fields, **overrides})


def _video(thumb="video-thumb-id", **overrides):
    fields = dict(
        file_id="webm-id", is_animated=False, is_video=True, emoji="🐱",
        set_name="Movies", thumbnail=SimpleNamespace(file_id=thumb, file_size=len(WEBP)),
        file_size=22000,
    )
    return SimpleNamespace(**{**fields, **overrides})


# --- choosing the file ------------------------------------------------------

def test_a_static_sticker_is_sent_as_itself():
    plan = plan_sticker(_static())
    assert plan.file_id == "sticker-id"
    assert plan.kind == sticker_vision.STATIC
    assert plan.from_thumbnail is False
    assert plan.mime_type == "image/webp"
    assert plan.still_only is False


@pytest.mark.parametrize("factory,kind", [(_animated, "animated"), (_video, "video")])
def test_animated_and_video_stickers_use_the_still_thumbnail(factory, kind):
    plan = plan_sticker(factory())
    assert plan.kind == kind
    assert plan.from_thumbnail is True
    assert plan.file_id.startswith("thumb") or plan.file_id.startswith("video-thumb")
    assert plan.still_only is True
    # Unknown until the bytes arrive; Telegram thumbnails are WebP or JPG.
    assert plan.mime_type == ""


@pytest.mark.parametrize("factory", [_animated, _video])
def test_a_sticker_without_a_thumbnail_is_unreadable(factory):
    assert plan_sticker(factory(thumb="")) is None
    assert plan_sticker(factory(thumb=None)) is None


def test_no_sticker_and_no_file_id_are_unreadable():
    assert plan_sticker(None) is None
    assert plan_sticker(_static(file_id="")) is None


def test_emoji_and_pack_name_are_cleaned_and_bounded():
    plan = plan_sticker(_static(emoji="  😹  ", set_name="Cats\n" + "x" * 200))
    assert plan.emoji == "😹"
    assert "\n" not in plan.set_name
    assert len(plan.set_name) <= 64


def test_missing_optional_fields_do_not_break_the_plan():
    bare = SimpleNamespace(file_id="only-an-id", is_animated=False, is_video=False)
    plan = plan_sticker(bare)
    assert plan.file_id == "only-an-id"
    assert plan.emoji == "" and plan.set_name == "" and plan.file_size == 0


# --- sniffing ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (WEBP, "image/webp"),
        (JPEG, "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"),
        (b"GIF89a" + b"\x00" * 8, "image/gif"),
        (b"", ""),
        (b"<html>not an image</html>", ""),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", ""),
    ],
)
def test_image_mime_is_sniffed_from_the_bytes(raw, expected):
    assert sniff_image_mime(raw) == expected


# --- the prompt the model sees ----------------------------------------------

def test_the_prompt_says_it_is_a_sticker_and_not_a_photo():
    text = describe_for_model(plan_sticker(_static()))
    assert "Telegram sticker" in text
    assert "not a photograph" in text
    assert "Do not describe it like a photo" in text


def test_the_prompt_lists_the_pack_and_the_senders_emoji():
    text = describe_for_model(plan_sticker(_static()))
    assert "Sticker pack: Cats" in text
    assert "Emoji the sender sent with it: 😹" in text


def test_the_prompt_admits_it_only_has_one_frame():
    text = describe_for_model(plan_sticker(_animated()))
    assert "only one still frame" in text
    assert "Never claim to have watched the animation" in text
    # A static sticker has no animation to disclaim.
    assert "only one still frame" not in describe_for_model(plan_sticker(_static()))


def test_the_users_words_are_appended_after_the_framing():
    text = describe_for_model(plan_sticker(_static()), "  چرا این استیکر؟  ")
    assert text.endswith("CURRENT MESSAGE:\nچرا این استیکر؟")


def test_a_wordless_sticker_gets_an_explicit_default():
    assert describe_for_model(StickerPlan(file_id="x", kind="static")).endswith(
        "The user sent this sticker without any words."
    )


# --- the handlers -----------------------------------------------------------

class _FakeFile:
    def __init__(self, raw: bytes):
        self.raw = raw

    async def download_to_memory(self, *, out):
        out.write(self.raw)


def _sticker_message(sticker, *, text=None, raw=WEBP):
    update, context, _ = _telegram_update()
    update.message.text = text
    update.message.sticker = sticker
    context.bot.get_file = AsyncMock(return_value=_FakeFile(raw))
    return update, context


def _last_request(client):
    return client.models.requests[-1]


def _last_inline_part(client):
    _, contents, _ = _last_request(client)
    parts = contents[-1].parts or []
    return next(part for part in parts if getattr(part, "inline_data", None))


def test_a_static_sticker_reaches_the_model_as_a_webp_image(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("گربه‌ی خنده‌دار")]
    update, context = _sticker_message(_static())
    asyncio.run(bot.message_handler(update, context))

    context.bot.get_file.assert_awaited_once_with("sticker-id")
    inline = _last_inline_part(client)
    assert inline.inline_data.mime_type == "image/webp"
    assert inline.inline_data.data == WEBP
    assert update.message.reply_text.await_args.args[0] == "گربه‌ی خنده‌دار"


def test_an_animated_sticker_sends_its_thumbnail_and_says_so(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("ok")]
    update, context = _sticker_message(_animated(), raw=JPEG)
    asyncio.run(bot.message_handler(update, context))

    context.bot.get_file.assert_awaited_once_with("thumb-id")
    # The MIME comes from the bytes, not from the sticker type.
    assert _last_inline_part(client).inline_data.mime_type == "image/jpeg"
    prompt = _last_request(client)[1][-1].parts[0].text
    assert "only one still frame" in prompt


def test_a_wordless_sticker_is_saved_as_a_sticker_turn(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("ok")]
    update, context = _sticker_message(_static())
    asyncio.run(bot.message_handler(update, context))

    row = bot.db.execute("SELECT content FROM messages WHERE role='user' ORDER BY id").fetchall()[-1]
    assert row["content"].rstrip().endswith("[sticker]")


def test_a_sticker_without_a_preview_is_explained_not_sent(isolated_bot):
    bot, client = isolated_bot
    update, context = _sticker_message(_animated(thumb=""))
    asyncio.run(bot.message_handler(update, context))

    assert client.models.requests == []
    reply = update.message.reply_text.await_args.args[0]
    assert "تصویر قابل خواندنی" in reply


def test_bytes_that_are_not_an_image_are_refused(isolated_bot):
    """A broken download must not be sent to the model as a WebP."""
    bot, client = isolated_bot
    update, context = _sticker_message(_static(), raw=b"<html>404</html>")
    asyncio.run(bot.message_handler(update, context))

    assert client.models.requests == []
    reply = update.message.reply_text.await_args.args[0]
    assert "تصویر قابل خواندنی" in reply


def test_the_error_follows_the_user_language(isolated_bot):
    bot, _ = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    update, context = _sticker_message(_animated(thumb=""))
    asyncio.run(bot.message_handler(update, context))
    reply = update.message.reply_text.await_args.args[0]
    assert "no readable image" in reply
    assert PERSIAN.search(reply) is None


def test_replying_to_a_sticker_analyses_the_replied_sticker(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("یعنی خوشحاله")]
    replied = SimpleNamespace(message_id=41, from_user=None, text=None, caption=None,
                              sticker=_static(file_id="replied-sticker"))
    update, context = _sticker_message(None, text="این یعنی چی؟")
    update.message.reply_to_message = replied
    asyncio.run(bot.message_handler(update, context))

    context.bot.get_file.assert_awaited_once_with("replied-sticker")
    assert _last_inline_part(client).inline_data.mime_type == "image/webp"
    # The replied sticker gets the sticker framing, not the generic "look at
    # this media" wording.
    prompt = _last_request(client)[1][-1].parts[0].text
    assert "Telegram sticker" in prompt
    assert "این یعنی چی؟" in prompt


def test_replying_to_an_animated_sticker_says_it_is_one_frame(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("جشن")]
    replied = SimpleNamespace(message_id=41, from_user=None, text=None, caption=None, sticker=_animated())
    update, context = _sticker_message(None, text="چه مناسبتیه؟", raw=JPEG)
    update.message.reply_to_message = replied
    asyncio.run(bot.message_handler(update, context))

    context.bot.get_file.assert_awaited_once_with("thumb-id")
    prompt = _last_request(client)[1][-1].parts[0].text
    assert "only one still frame" in prompt


def test_a_sticker_in_a_group_is_ignored_unless_addressed(isolated_bot):
    """A sticker is not an invitation; group chatter stays silent."""
    bot, client = isolated_bot
    update, context = _sticker_message(_static())
    update.effective_chat = SimpleNamespace(id=-100123, type="supergroup")
    asyncio.run(bot.message_handler(update, context))
    assert client.models.requests == []
    context.bot.get_file.assert_not_awaited()


def test_an_addressed_sticker_in_a_group_is_still_answered(isolated_bot):
    """The silence above is about being unaddressed, not about stickers."""
    bot, client = isolated_bot
    client.models.responses = [_text_response("خوشحالیه")]
    update, context = _sticker_message(_static(), text="آورا این استیکر یعنی چی؟")
    update.effective_chat = SimpleNamespace(id=-100123, type="supergroup")
    asyncio.run(bot.message_handler(update, context))
    context.bot.get_file.assert_awaited_once_with("sticker-id")
    assert _last_inline_part(client).inline_data.mime_type == "image/webp"


def test_the_handler_now_receives_stickers():
    """Filtering matters: without this the handler never sees a sticker at all."""
    source = open("bot.py", encoding="utf-8").read()
    assert "filters.Sticker.ALL" in source
