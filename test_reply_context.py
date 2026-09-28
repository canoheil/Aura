from types import SimpleNamespace

from reply_context import build_reply_context, build_request_with_reply_context


def user(**kwargs):
    defaults = dict(first_name="Test", last_name="User", username="testuser", id=123)
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_text_reply_context_contains_exact_content_and_author():
    reply = SimpleNamespace(
        message_id=42,
        from_user=user(first_name="Ali", last_name="Rezaei"),
        text="این همان پیامی است که منظورم بود.",
        caption=None,
        photo=None,
        document=None,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        animation=None,
    )
    result = build_reply_context(reply)
    assert "REPLIED-TO MESSAGE" in result
    assert "Author: Ali Rezaei" in result
    assert "Telegram message ID: 42" in result
    assert "این همان پیامی است که منظورم بود." in result


def test_caption_is_used_when_reply_has_no_text():
    reply = SimpleNamespace(
        message_id=43,
        from_user=user(username="caption_user"),
        text=None,
        caption="توضیح عکس",
        photo=[object()],
        document=None,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        animation=None,
    )
    result = build_reply_context(reply)
    assert "توضیح عکس" in result
    assert "Media type(s): image" in result


def test_media_without_text_is_described_but_not_downloaded_in_stage8():
    reply = SimpleNamespace(
        message_id=44,
        from_user=user(),
        text=None,
        caption=None,
        photo=None,
        document=object(),
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        animation=None,
    )
    result = build_reply_context(reply)
    assert "detailed media analysis will be handled separately" in result
    assert "Media type(s): document" in result


def test_no_reply_returns_none():
    assert build_reply_context(None) is None


def test_reply_context_is_high_priority_before_current_message():
    history = [{"role": "system", "content": "system"}]
    result = build_request_with_reply_context(history, "ائورا اینو توضیح بده", "[REPLY]")
    assert result[0] == history[0]
    assert result[1] == {"role": "system", "content": "[REPLY]"}
    assert result[2] == {"role": "user", "content": "ائورا اینو توضیح بده"}


def test_no_reply_preserves_normal_request_shape():
    history = [{"role": "system", "content": "system"}]
    result = build_request_with_reply_context(history, "سلام", None)
    assert result == history + [{"role": "user", "content": "سلام"}]


def test_reply_media_request_contains_media_once_with_current_question():
    from reply_context import build_reply_media_request

    result = build_reply_media_request(
        [{"role": "user", "content": "پیام قبلی"}],
        "این عکس را توضیح بده",
        b"fake-image-bytes",
        "image/jpeg",
    )

    assert len(result) == 2
    assert result[-1]["role"] == "user"
    content = result[-1]["content"]
    assert sum(1 for item in content if item.get("type") == "text") == 1
    assert sum(1 for item in content if item.get("type") == "media_bytes") == 1
    assert content[1]["data"] == b"fake-image-bytes"
    assert content[1]["mime_type"] == "image/jpeg"
