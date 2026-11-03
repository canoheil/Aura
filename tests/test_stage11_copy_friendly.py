from telegram_formatting import render_telegram_html, strip_formatting


def test_copy_sensitive_command_stays_exact_inside_code():
    source = "Run `python -m py_compile bot.py`"
    assert render_telegram_html(source) == "Run <code>python -m py_compile bot.py</code>"


def test_special_characters_inside_code_are_literal():
    source = "`echo '<tag> & * _ ~'`"
    result = render_telegram_html(source)
    assert result == "<code>echo '&lt;tag&gt; &amp; * _ ~'</code>"


def test_multiline_config_preserves_indentation_and_punctuation():
    source = "```env\nGEMINI_MODEL=gemini-3.1-flash-lite\nAPI_URL=https://example.com/a?x=1&y=2\n  INDENTED=true\n```"
    result = render_telegram_html(source)
    assert "GEMINI_MODEL=gemini-3.1-flash-lite" in result
    assert "API_URL=https://example.com/a?x=1&amp;y=2" in result
    assert "  INDENTED=true" in result


def test_plain_fallback_for_a_failed_chunk_does_not_require_original_answer():
    chunk = "<b>مرحله</b> <code>python bot.py</code>"
    assert strip_formatting(chunk) == "مرحله python bot.py"


def test_prose_is_not_automatically_wrapped_as_code():
    result = render_telegram_html("این یک توضیح معمولی است، نه کد.")
    assert result == "این یک توضیح معمولی است، نه کد."
