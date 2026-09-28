from telegram_formatting import render_telegram_html, split_telegram_html, strip_formatting


def test_spoiler():
    assert render_telegram_html("این ||اسپویل|| است") == "این <tg-spoiler>اسپویل</tg-spoiler> است"


def test_https_link():
    assert render_telegram_html("[سایت](https://example.com/a?x=1&y=2)") == '<a href="https://example.com/a?x=1&amp;y=2">سایت</a>'


def test_telegram_user_mention():
    assert render_telegram_html("[Canoheil](tg://user?id=123456789)") == '<a href="tg://user?id=123456789">Canoheil</a>'


def test_reject_unsafe_link():
    out = render_telegram_html("[bad](javascript:alert(1))")
    assert "javascript:" not in out.lower() or "<a" not in out


def test_plain_username_is_not_fake_mention():
    assert render_telegram_html("@example") == "@example"


def test_custom_emoji_attribute_is_sanitized():
    assert render_telegram_html('<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>') == '<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>'


def test_unsafe_html_attribute_is_not_preserved():
    out = render_telegram_html('<a href="javascript:alert(1)">x</a>')
    assert '<a ' not in out.lower()


def test_spoiler_survives_chunking():
    text = '<tg-spoiler>' + ('x' * 3990) + '</tg-spoiler>'
    chunks = split_telegram_html(text, 4000)
    assert len(chunks) >= 2
    assert all(c.startswith('<tg-spoiler>') or c.endswith('</tg-spoiler>') for c in chunks)
    assert ''.join(strip_formatting(c) for c in chunks).count('x') == 3990


def test_entity_tag_attributes_survive_chunking():
    text = '<tg-emoji emoji-id="5368324170671202286">' + ('👍' * 3990) + '</tg-emoji>'
    chunks = split_telegram_html(text, 4000)
    assert len(chunks) >= 2
    assert all('emoji-id="5368324170671202286"' in c for c in chunks)
