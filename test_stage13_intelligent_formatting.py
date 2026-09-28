from pathlib import Path

from telegram_formatting import render_telegram_html


BOT_SOURCE = (Path(__file__).resolve().parent / "bot.py").read_text()


def test_system_prompt_requires_usefulness_decision():
    assert "quick usefulness decision" in BOT_SOURCE
    assert "If not, leave the text plain" in BOT_SOURCE


def test_casual_replies_are_directed_to_plain_text():
    assert "short casual replies, greetings, jokes" in BOT_SOURCE
    assert "prefer plain text" in BOT_SOURCE


def test_emphasis_is_for_hierarchy_not_decoration():
    assert "Use emphasis for hierarchy, not decoration" in BOT_SOURCE


def test_spoilers_are_contextual_not_decorative():
    assert "Use spoilers only when the user is clearly discussing spoilers" in BOT_SOURCE


def test_entities_require_exact_source_data():
    assert "never invent a URL, Telegram user ID, or custom emoji ID" in BOT_SOURCE


def test_plain_answer_remains_plain_when_no_formatting_is_requested():
    text = "سلام، آره اینجا هستم."
    assert render_telegram_html(text) == text
