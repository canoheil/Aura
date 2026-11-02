"""Regression tests for copy-safe answers and verified web sources."""
import asyncio

import pytest

from answer_quality import (
    has_web_evidence,
    remove_generated_source_section,
    search_source_urls,
)
from test_agent_tool_response import (
    isolated_bot, _runtime, _telegram_update, _text_response, _tool_response,
)


def test_cleaner_preserves_code_indentation_and_numeric_arrays(isolated_bot):
    bot, _ = isolated_bot
    answer = "توضیح  دقیق:\n```python\ndef f():\n    return [1]\n```\nپایان `x = [2]`"
    assert bot.clean_model_answer(answer) == answer


def test_plain_indented_answer_is_not_trimmed(isolated_bot):
    bot, client = isolated_bot
    answer = "    return [1]"
    assert bot.clean_model_answer(answer) == answer
    client.models.responses = [_text_response(answer)]
    update, context, _ = _telegram_update()
    update.message.text = "کد را بنویس"
    asyncio.run(bot.message_handler(update, context))
    assert update.message.reply_text.await_args.args[0] == answer


def test_source_section_removal_does_not_touch_code():
    source = "```text\nSources:\n    [1]\n```\n\nجواب\n\nمنابع:\nhttps://invented.test"
    assert remove_generated_source_section(source) == "```text\nSources:\n    [1]\n```\n\nجواب"


@pytest.mark.parametrize("heading", ["**Sources:**", "### Sources", "🔎 منابع بازیابی‌شده:"])
def test_hallucinated_source_headings_are_removed(heading):
    assert remove_generated_source_section(f"پاسخ\n\n{heading}\nhttps://invented.test") == "پاسخ"


def test_source_cleanup_preserves_user_supplied_source_and_code_after_list():
    user_quote = "Sources:\nhttps://user.example"
    assert remove_generated_source_section(user_quote, user_text=user_quote) == user_quote
    answer = "پاسخ\n\nSources:\nhttps://invented.test\n\n```python\n    return [1]\n```"
    assert remove_generated_source_section(answer) == "پاسخ\n\n```python\n    return [1]\n```"


def test_source_cleanup_keeps_explanation_and_code_after_blank_line():
    answer = (
        "پاسخ\n\nSources:\nhttps://invented.test\n\n"
        "نکتهٔ مهم باقی می‌ماند.\n\n```python\n    return [1]\n```"
    )
    assert remove_generated_source_section(answer) == (
        "پاسخ\n\nنکتهٔ مهم باقی می‌ماند.\n\n```python\n    return [1]\n```"
    )


def test_sources_only_come_from_successful_tool_results():
    events = [
        {"tool": "web_search", "result": {"ok": False, "results": [{"url": "https://bad.test"}]}},
        {"tool": "web_search", "result": {"ok": True, "results": [
            {"url": "https://source.test/a?x=1&y=2", "evidence": "Retrieved text"},
            {"url": "https://source.test/a?x=1&y=2"},
            {"url": "javascript:alert(1)"},
            {"url": "https://user:secret@source.test/private"},
            {"url": "https://evil.test/\nmore"},
        ]}},
        {"tool": "web_open", "result": {"ok": True, "pages": [{"url": "https://source.test/b"}]}},
    ]
    assert has_web_evidence(events)
    assert search_source_urls(events) == ["https://source.test/a?x=1&y=2", "https://source.test/b"]
    assert all("bad.test" not in url for url in search_source_urls(events))


def test_a_bare_link_does_not_count_as_current_search_evidence():
    events = [{"tool": "web_search", "result": {"ok": True, "results": [{"url": "https://source.test"}]}}]
    assert search_source_urls(events) == ["https://source.test"]
    assert not has_web_evidence(events)


def test_forced_search_failure_cannot_become_an_unverified_current_answer():
    ns, _, _ = _runtime([
        RuntimeError("400 cannot use forced search"),
        _text_response("قیمت فعلی ۱۰۰ دلار است."),
    ])
    answer, _, _ = ns["ask_gemini"](
        "test-model", [{"role": "user", "content": "قیمت دلار چنده؟"}], require_web_search=True
    )
    assert "۱۰۰" not in answer
    assert "نمی‌تونم" in answer


def test_voluntary_search_failure_is_not_misrepresented():
    ns, _, _ = _runtime([
        _tool_response("web_search", {"query": "python"}),
        _text_response("پیدا کردم: نتیجهٔ ساختگی."),
    ], handlers={"web_search": lambda _args: {"ok": False, "error": "offline"}})
    answer, _, _ = ns["ask_gemini"]("test-model", [{"role": "user", "content": "پایتون را بررسی کن"}])
    assert "ساختگی" not in answer
    assert "نمی‌تونم" in answer


def test_soft_limit_or_unavailable_search_can_still_require_evidence():
    ns, _, _ = _runtime([_text_response("قیمت فعلی ۱۰۰ دلار است.")])
    answer, _, _ = ns["ask_gemini"](
        "test-model", [{"role": "user", "content": "قیمت دلار چنده؟"}], require_search_evidence=True
    )
    assert "۱۰۰" not in answer
    assert "نمی‌تونم" in answer


def test_real_handler_drops_hallucinated_source_when_search_was_not_run(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("پاسخ\n\nSources:\nhttps://invented.test")]
    update, context, _ = _telegram_update()
    update.message.text = "یک توضیح بده"
    asyncio.run(bot.message_handler(update, context))
    assert update.message.reply_text.await_args.args[0] == "پاسخ"


def test_real_handler_sends_only_the_answer_without_a_source_list(isolated_bot, monkeypatch):
    bot, client = isolated_bot
    client.models.responses = [
        _tool_response("web_search", {"query": "python"}),
        _text_response("نتیجهٔ جست‌وجو\n\nمنابع:\nhttps://invented.test"),
    ]
    source = "https://python.org/_docs_/?a=1&b=2"

    def execute(state, name, args):
        result = {"ok": True, "results": [{"url": source, "title": "Docs", "evidence": "Python documentation"}]}
        state.events.append({"tool": name, "result": result})
        return result

    monkeypatch.setattr(bot.AGENT_RUNNER, "execute_tool", execute)
    update, context, _ = _telegram_update()
    update.message.text = "درباره پایتون توضیح بده"
    asyncio.run(bot.message_handler(update, context))

    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    # One message only: the answer. Search results, including the retrieved
    # URL, are evidence for the model and are never displayed as a list.
    assert replies == ["نتیجهٔ جست‌وجو"]
    assert source not in " ".join(replies)
    assert "invented.test" not in " ".join(replies)


def test_sources_only_answer_becomes_a_neutral_line(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("Sources:\nhttps://invented.test")]
    update, context, _ = _telegram_update()
    update.message.text = "یک توضیح بده"
    asyncio.run(bot.message_handler(update, context))
    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert replies == ["جواب مشخصی برای این سؤال پیدا نکردم."]
    assert "منبع" not in " ".join(replies)
