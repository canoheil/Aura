from telegram_formatting import render_telegram_html, strip_formatting, split_telegram_html


def test_plain_text_is_escaped():
    assert render_telegram_html("5 < 7 & 9 > 3") == "5 &lt; 7 &amp; 9 &gt; 3"


def test_core_emphasis():
    result = render_telegram_html("**bold** *italic* __under__ ~~gone~~")
    assert result == "<b>bold</b> <i>italic</i> <u>under</u> <s>gone</s>"


def test_inline_code_is_protected():
    result = render_telegram_html("Use `a*b_c**d` here")
    assert result == "Use <code>a*b_c**d</code> here"


def test_code_block_is_protected_and_language_is_kept():
    result = render_telegram_html("```python\nprint('*')\n```\nتمام")
    assert '<pre><code class="language-python">print(\'*\')</code></pre>' in result
    assert result.endswith("تمام")


def test_code_preserves_indentation_array_and_intentional_blank_lines():
    result = render_telegram_html("```python\n\n    return [1]\n\n\n```")
    assert result == '<pre><code class="language-python">\n    return [1]\n\n</code></pre>'


def test_unfenced_indentation_and_fallback_code_are_preserved():
    code = "    def __init__():\n        return [1]"
    assert render_telegram_html(code) == code
    assert strip_formatting(code) == code
    unindented_header = "def __init__():\n    return [1]"
    assert render_telegram_html(unindented_header) == unindented_header
    assert strip_formatting(unindented_header) == unindented_header
    assert strip_formatting('<pre><code class="language-python">def __init__():\n    return [1]</code></pre>') == "def __init__():\n    return [1]"


def test_existing_html_code_and_nested_backticks_are_literal():
    source = '<pre><code>def __init__():\n    return "`a__b`", [1]</code></pre>'
    assert render_telegram_html(source) == source
    fenced = "```text\n<pre>**literal** [1]</pre>\n```"
    assert render_telegram_html(fenced) == '<pre><code class="language-text">&lt;pre&gt;**literal** [1]&lt;/pre&gt;</code></pre>'


def test_quote_block():
    result = render_telegram_html("> این یک نقل‌قول است\n> خط دوم")
    assert result == "<blockquote>این یک نقل‌قول است\nخط دوم</blockquote>"


def test_existing_safe_html_is_preserved():
    result = render_telegram_html("<b>سلام</b> <i>دنیا</i>")
    assert result == "<b>سلام</b> <i>دنیا</i>"


def test_unsafe_html_is_escaped():
    result = render_telegram_html("<script>alert(1)</script>")
    assert "&lt;script&gt;" in result
    assert "<script>" not in result


def test_mixed_persian_and_code():
    result = render_telegram_html("**نسخه:** `Python 3.11`\n\n__مهم__")
    assert "<b>نسخه:</b> <code>Python 3.11</code>" in result
    assert "<u>مهم</u>" in result


def test_plain_fallback_removes_formatting():
    assert strip_formatting("**سلام** `کد`\n> نقل قول") == "سلام کد\nنقل قول"


def test_long_formatted_message_splits_without_cutting_tags():
    chunks = split_telegram_html("<b>" + ("الف" * 2000) + "</b>", 1000)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert all(chunk.startswith("<b>") and chunk.endswith("</b>") for chunk in chunks)
