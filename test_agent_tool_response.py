"""Offline regression tests for agent tool responses and inline media."""
from copy import deepcopy
from pathlib import Path

from google.genai import types
import pytest

from test_freshness_policy import _agent_namespace
from tool_system import AgentRunner, ToolRegistry, has_textual_tool_declaration


RAW_DECLARATION = (
    "declaration:default_api:file_analyze{focus:Identify if this character is "
    "Komi Shouko from Komi Can't Communicate or a similar character,max_chars:5000}"
)
IMAGE = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


def _response(*parts, usage=None):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))],
        usage_metadata=(
            types.GenerateContentResponseUsageMetadata(
                prompt_token_count=usage[0],
                candidates_token_count=usage[1],
                total_token_count=usage[2],
            )
            if usage else None
        ),
    )


def _text_response(text, *, usage=None):
    return _response(types.Part.from_text(text=text), usage=usage)


def _tool_response(name, args, *, extra_text=None):
    parts = [types.Part(function_call=types.FunctionCall(name=name, args=args))]
    if extra_text is not None:
        parts.append(types.Part.from_text(text=extra_text))
    return _response(*parts)


class _FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def generate_content(self, *, model, contents, config):
        self.requests.append((model, deepcopy(contents), config))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _FakeClient:
    def __init__(self, responses):
        self.models = _FakeModels(responses)


def _runtime(responses, *, handlers=None, max_rounds=3):
    handlers = handlers or {}
    registry = ToolRegistry()
    for name in ("file_analyze", "web_search"):
        registry.register(
            name,
            types.FunctionDeclaration(name=name, description=f"Test {name}"),
            handlers.get(name, lambda args: {"ok": True}),
        )
    runner = AgentRunner(registry)
    client = _FakeClient(responses)
    ns = _agent_namespace(client=client, registry=registry, runner=runner, max_rounds=max_rounds)
    return ns, client, runner


def _reply_photo_messages():
    # The same multimodal structure the reply-media builder passes to the agent.
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "این کیه؟"},
            {"type": "media_bytes", "data": IMAGE, "mime_type": "image/jpeg"},
        ],
    }]


def _declared_names(config):
    return {
        declaration.name
        for tool in config.tools or []
        for declaration in tool.function_declarations or []
    }


def test_reply_photo_raw_declaration_is_retried_instead_of_sent():
    ns, client, _ = _runtime([
        _text_response(RAW_DECLARATION),
        _text_response("احتمالاً کومی شوکو از Komi Can't Communicate است."),
    ])

    answer, _usage, events = ns["ask_gemini"]("test-model", _reply_photo_messages())

    assert answer == "احتمالاً کومی شوکو از Komi Can't Communicate است."
    assert events == []
    assert len(client.models.requests) == 2
    for _, contents, config in client.models.requests:
        assert contents[-1].parts[-1].inline_data.data == IMAGE
        assert "file_analyze" not in _declared_names(config)
        assert "web_search" in _declared_names(config)


def test_file_tool_is_not_advertised_without_a_document_context():
    ns, client, _ = _runtime([_text_response("سلام")])
    ns["ask_gemini"]("test-model", [{"role": "user", "content": "سلام"}])
    assert "file_analyze" not in _declared_names(client.models.requests[0][2])
    assert "web_search" in _declared_names(client.models.requests[0][2])


@pytest.mark.parametrize("text", [
    RAW_DECLARATION,
    "دارم بررسی می‌کنم...\n" + RAW_DECLARATION,
    "DECLARATION : default_API : file_analyze  { focus: test }",
    "declaration:default_api:web_search(query:latest)",
])
def test_detects_raw_tool_syntax_but_not_only_a_specific_tool(text):
    assert has_textual_tool_declaration(_text_response(text))


@pytest.mark.parametrize("text", [
    "این احتمالاً کومی شوکو است.",
    "Use the file_analyze tool for a document.",
    'در کد از {"name": "file_analyze", "focus": "test"} استفاده کن.',
    "declaration:default_api:file_analyze",
    "default_api:file_analyze{focus:test}",
    "این لینکِ declaration نیست.",
])
def test_normal_text_and_ordinary_code_do_not_trigger_recovery(text):
    response = _text_response(text)
    assert not has_textual_tool_declaration(response)
    ns, client, _ = _runtime([response])
    answer, _, events = ns["ask_gemini"]("test-model", [{"role": "user", "content": "سلام"}])
    assert answer == text
    assert len(client.models.requests) == 1
    assert events == []


def test_detector_joins_visible_parts_and_ignores_thought_parts():
    split = _response(
        types.Part.from_text(text="declaration:default_"),
        types.Part.from_text(text="api:file_analyze{focus:test}"),
    )
    assert has_textual_tool_declaration(split)
    thought = _response(
        types.Part(text=RAW_DECLARATION, thought=True),
        types.Part.from_text(text="این احتمالاً کومی است."),
    )
    assert not has_textual_tool_declaration(thought)


def test_repeated_raw_output_fails_safely_without_executing_it(caplog):
    executed = []
    ns, client, _ = _runtime(
        [_text_response(RAW_DECLARATION), _text_response(RAW_DECLARATION)],
        handlers={"file_analyze": lambda args: executed.append(args)},
    )
    with pytest.raises(ns["GeminiAPIError"]) as exc:
        ns["ask_gemini"]("test-model", _reply_photo_messages())
    assert len(client.models.requests) == 2
    assert executed == []
    assert RAW_DECLARATION not in str(exc.value)
    assert RAW_DECLARATION not in caplog.text
    # The failed model response was never added to the next prompt.
    assert client.models.requests[0][1] == client.models.requests[1][1]


def test_raw_text_never_executes_even_when_a_real_call_is_in_the_same_response():
    executed = []
    ns, client, _ = _runtime(
        [
            _tool_response("web_search", {"query": "should not run"}, extra_text=RAW_DECLARATION),
            _text_response("به نظر می‌رسد کومی باشد."),
        ],
        handlers={"web_search": lambda args: executed.append(args)},
    )
    answer, _, events = ns["ask_gemini"]("test-model", _reply_photo_messages())
    assert answer == "به نظر می‌رسد کومی باشد."
    assert events == []
    assert executed == []
    assert client.models.requests[0][1] == client.models.requests[1][1]


def test_retry_can_return_a_genuine_structured_tool_call():
    executed = []

    def search(args):
        executed.append(dict(args))
        return {"ok": True, "result": "verified", "results": [{"url": "https://example.org/result", "evidence": "Verified text"}]}

    ns, client, _ = _runtime(
        [
            _text_response(RAW_DECLARATION),
            _tool_response("web_search", {"query": "Komi Shouko"}),
            _text_response("احتمالاً کومی شوکو است."),
        ],
        handlers={"web_search": search},
    )
    answer, _, events = ns["ask_gemini"]("test-model", _reply_photo_messages())
    assert answer == "احتمالاً کومی شوکو است."
    assert len(executed) == 1 and executed[0]["query"] == "Komi Shouko"
    assert len(events) == 1 and events[0]["tool"] == "web_search"
    assert len(client.models.requests) == 3
    assert client.models.requests[0][1] == client.models.requests[1][1]
    assert "structured function calls" in client.models.requests[1][2].system_instruction
    # The actual image survives into the post-tool model round too.
    assert client.models.requests[2][1][0].parts[-1].inline_data.data == IMAGE


def test_one_recovery_is_shared_by_all_rounds_of_the_same_task():
    executed = []
    ns, client, _ = _runtime(
        [
            _text_response(RAW_DECLARATION),
            _tool_response("web_search", {"query": "Komi"}),
            _text_response(RAW_DECLARATION),
        ],
        handlers={"web_search": lambda args: executed.append(args) or {"ok": True}},
    )
    with pytest.raises(ns["GeminiAPIError"]):
        ns["ask_gemini"]("test-model", _reply_photo_messages())
    assert len(client.models.requests) == 3
    assert len(executed) == 1


def test_real_document_tool_remains_available_and_receives_private_context(tmp_path):
    doc = tmp_path / "test.txt"
    doc.write_text("document contents", encoding="utf-8")
    executed = []

    def analyze(args):
        executed.append(dict(args))
        return {"ok": True, "content": Path(args["_file_path"]).read_text(encoding="utf-8")}

    ns, client, _ = _runtime(
        [
            _text_response(RAW_DECLARATION),
            _tool_response("file_analyze", {"focus": "summary"}),
            _text_response("محتوای سند بررسی شد."),
        ],
        handlers={"file_analyze": analyze},
    )
    answer, _, events = ns["ask_gemini"](
        "test-model",
        [{"role": "user", "content": "این سند را خلاصه کن"}],
        file_context={"file_path": str(doc), "file_name": doc.name, "file_mime_type": "text/plain"},
    )
    assert answer == "محتوای سند بررسی شد."
    assert len(executed) == 1
    assert executed[0]["_file_path"] == str(doc)
    assert executed[0]["focus"] == "summary"
    assert events[0]["result"]["content"] == "document contents"
    assert all("file_analyze" in _declared_names(config) for _, _, config in client.models.requests)
    assert str(doc) not in str(client.models.requests[1][2])


def test_hallucinated_file_call_without_document_is_not_executed():
    executed = []
    ns, client, _ = _runtime(
        [_tool_response("file_analyze", {"focus": "photo"}), _text_response("عکس را می‌بینم.")],
        handlers={"file_analyze": lambda args: executed.append(args)},
    )
    answer, _, events = ns["ask_gemini"]("test-model", _reply_photo_messages())
    assert answer == "عکس را می‌بینم."
    assert executed == []
    assert events[0]["tool"] == "file_analyze"
    assert events[0]["result"]["ok"] is False
    assert "inline media" in events[0]["result"]["error"]
    assert "file_analyze" not in _declared_names(client.models.requests[0][2])


def test_forced_search_and_its_recovery_keep_the_same_tool_restriction():
    executed = []
    ns, client, _ = _runtime(
        [
            _text_response(RAW_DECLARATION),
            _tool_response("web_search", {"query": "قیمت دلار"}),
            _text_response("نتیجه جست‌وجو آماده است."),
        ],
        handlers={"web_search": lambda args: executed.append(args) or {"ok": True}},
    )
    ns["ask_gemini"](
        "test-model", [{"role": "user", "content": "قیمت دلار چنده؟"}], require_web_search=True
    )
    for _, _, config in client.models.requests[:2]:
        forced = config.tool_config.function_calling_config
        assert str(forced.mode).endswith("ANY")
        assert list(forced.allowed_function_names) == ["web_search"]
    assert client.models.requests[2][2].tool_config is None
    assert len(executed) == 1


def test_forced_search_error_and_later_rounds_keep_active_document_tool(tmp_path):
    doc = tmp_path / "test.txt"
    doc.write_text("contents", encoding="utf-8")
    ns, client, _ = _runtime([
        RuntimeError("upstream does not support forced mode"),
        _tool_response("file_analyze", {}),
        _text_response("خلاصه سند"),
    ])
    ns["ask_gemini"](
        "test-model",
        [{"role": "user", "content": "سند را بررسی کن"}],
        file_context={"file_path": str(doc)},
        require_web_search=True,
    )
    assert all("file_analyze" in _declared_names(config) for _, _, config in client.models.requests)
    assert client.models.requests[0][2].tool_config is not None
    assert client.models.requests[1][2].tool_config is None
    assert client.models.requests[2][2].tool_config is None


def test_final_round_is_guarded_and_retried_without_reenabling_tools():
    executed = []
    ns, client, _ = _runtime(
        [
            _tool_response("web_search", {"query": "first"}),
            _tool_response("web_search", {"query": "over round limit"}),
            _text_response(RAW_DECLARATION),
            _text_response("پاسخ نهایی"),
        ],
        handlers={"web_search": lambda args: executed.append(args) or {"ok": True, "results": [{"url": "https://example.org/result", "evidence": "Verified text"}]}},
        max_rounds=1,
    )
    answer, _, events = ns["ask_gemini"]("test-model", _reply_photo_messages())
    assert answer == "پاسخ نهایی"
    assert len(executed) == 1
    assert len(events) == 1
    assert client.models.requests[2][2].tools is None
    assert client.models.requests[3][2].tools is None
    assert client.models.requests[2][1] == client.models.requests[3][1]
    assert client.models.requests[3][1][0].parts[-1].inline_data.data == IMAGE


def test_usage_includes_both_the_bad_and_retry_responses():
    ns, _, _ = _runtime([
        _text_response(RAW_DECLARATION, usage=(10, 2, 12)),
        _text_response("پاسخ", usage=(11, 3, 14)),
    ])
    _, usage, _ = ns["ask_gemini"]("test-model", _reply_photo_messages())
    assert usage == {"prompt_tokens": 21, "completion_tokens": 5, "total_tokens": 26}


@pytest.fixture
def isolated_bot(monkeypatch):
    """Load the real handler with a private in-memory DB and a fake API client."""
    import importlib.util
    import sqlite3
    from google import genai

    for key, value in {
        "TELEGRAM_BOT_TOKEN": "123456:test-token",
        "GEMINI_API_KEY": "test-key",
        "FOUNDER_USER_ID": "0",
        "GEMINI_MAX_RETRIES": "0",
        "STICKERS_ENABLED": "false",
        "SVG_IMAGE_ENABLED": "false",
        "MEMES_ENABLED": "false",
        "WEATHER_ENABLED": "false",
        "POLLINATIONS_ENABLED": "false",
        "REACTION_AI_ENABLED": "false",
        "CONTEXT_MANAGER_ENABLED": "false",
        "YOU_API_KEY": "",
    }.items():
        monkeypatch.setenv(key, value)

    client = _FakeClient([])
    real_connect = sqlite3.connect
    with monkeypatch.context() as setup:
        setup.setattr(sqlite3, "connect", lambda _path, **kw: real_connect(":memory:", **kw))
        setup.setattr(genai, "Client", lambda **_kw: client)
        spec = importlib.util.spec_from_file_location("_isolated_aura_bot", Path(__file__).with_name("bot.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    yield module, client
    module.db.close()


def _telegram_update(*, reply_photo=False, document=False):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    user = SimpleNamespace(id=123, first_name="Test", last_name=None, username=None, is_bot=False)
    previous = None
    if reply_photo:
        previous = SimpleNamespace(
            message_id=41, from_user=user, text=None, caption=None,
            photo=[SimpleNamespace(file_id="photo-id", file_size=len(IMAGE))],
        )
    message = SimpleNamespace(
        message_id=42,
        from_user=user,
        text=None if document else "این کیه؟",
        caption="این فایل را خلاصه کن" if document else None,
        photo=None,
        document=(
            SimpleNamespace(file_id="document-id", file_size=17, file_name="test.txt", mime_type="text/plain")
            if document else None
        ),
        reply_to_message=previous,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=user,
        effective_chat=SimpleNamespace(id=123, type="private"),
        message=message,
    )

    class FakeTelegramFile:
        saved_path = None

        async def download_to_memory(self, *, out):
            out.write(IMAGE)

        async def download_to_drive(self, *, custom_path):
            self.saved_path = custom_path
            Path(custom_path).write_text("document contents", encoding="utf-8")

    tg_file = FakeTelegramFile()
    tg_bot = SimpleNamespace(
        get_file=AsyncMock(return_value=tg_file),
        send_chat_action=AsyncMock(),
    )
    return update, SimpleNamespace(bot=tg_bot), tg_file


def test_telegram_reply_photo_recovers_and_only_saves_the_real_answer(isolated_bot):
    import asyncio

    bot, client = isolated_bot
    client.models.responses = [
        _text_response(RAW_DECLARATION),
        _text_response("احتمالاً کومی شوکو است."),
    ]
    update, context, _ = _telegram_update(reply_photo=True)
    asyncio.run(bot.message_handler(update, context))

    context.bot.get_file.assert_awaited_once_with("photo-id")
    update.message.reply_text.assert_awaited_once()
    assert update.message.reply_text.await_args.args[0] == "احتمالاً کومی شوکو است."
    rows = bot.db.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[-1]["content"] == "احتمالاً کومی شوکو است."
    assert RAW_DECLARATION not in " ".join(row["content"] for row in rows)
    assert len(client.models.requests) == 2
    for _, contents, config in client.models.requests:
        assert contents[-1].parts[-1].inline_data.data == IMAGE
        assert "file_analyze" not in _declared_names(config)


def test_telegram_reply_photo_fails_safely_if_recovery_also_leaks(isolated_bot):
    import asyncio

    bot, client = isolated_bot
    client.models.responses = [_text_response(RAW_DECLARATION), _text_response(RAW_DECLARATION)]
    update, context, _ = _telegram_update(reply_photo=True)
    asyncio.run(bot.message_handler(update, context))

    update.message.reply_text.assert_awaited_once()
    assert RAW_DECLARATION not in update.message.reply_text.await_args.args[0]
    assert "دوباره امتحان کن" in update.message.reply_text.await_args.args[0]
    assert bot.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_telegram_document_still_uses_the_real_extractor(isolated_bot):
    import asyncio

    bot, client = isolated_bot
    client.models.responses = [
        _tool_response("file_analyze", {"focus": "summary"}),
        _text_response("محتوای سند بررسی شد."),
    ]
    update, context, tg_file = _telegram_update(document=True)
    asyncio.run(bot.message_handler(update, context))

    update.message.reply_text.assert_awaited_once()
    assert update.message.reply_text.await_args.args[0] == "محتوای سند بررسی شد."
    assert tg_file.saved_path and not Path(tg_file.saved_path).exists()
    assert all("file_analyze" in _declared_names(config) for _, _, config in client.models.requests)
    function_response = client.models.requests[1][1][-1].parts[0].function_response
    assert function_response.name == "file_analyze"
    assert function_response.response["content"] == "document contents"
    assert tg_file.saved_path not in str(function_response.response)


def test_bot_can_quote_a_declaration_the_user_explicitly_wants_explained():
    answer_text = f"این خروجی یک اعلان ابزار است، نه نتیجهٔ تحلیل:\n`{RAW_DECLARATION}`"
    ns, client, _ = _runtime([_text_response(answer_text)])
    answer, _, _ = ns["ask_gemini"](
        "test-model", [{"role": "user", "content": f"این یعنی چی؟ {RAW_DECLARATION}"}]
    )
    assert answer == answer_text
    assert len(client.models.requests) == 1


def test_only_the_exact_current_user_declaration_can_be_quoted():
    supplied = "declaration:default_api:file_analyze{focus:{x:1},max_chars:2000}"
    invented = "declaration:default_api:file_analyze{focus:{x:1},max_chars:5000}"
    response = _text_response(f"{supplied}\n{invented}")
    assert has_textual_tool_declaration(response, user_text=supplied)
    assert not has_textual_tool_declaration(_text_response(supplied), user_text=supplied)
    # A declaration seen only in an older turn must not exempt the new answer.
    ns, client, _ = _runtime([_text_response(RAW_DECLARATION), _text_response("پاسخ سالم")])
    messages = [
        {"role": "user", "content": f"این یعنی چی؟ {RAW_DECLARATION}"},
        {"role": "assistant", "content": "یک اعلان ابزار است."},
        {"role": "user", "content": "این عکس چیه؟"},
    ]
    answer, _, _ = ns["ask_gemini"]("test-model", messages)
    assert answer == "پاسخ سالم"
    assert len(client.models.requests) == 2


def test_comparison_handles_quoted_braces_and_incomplete_output_safely():
    supplied = 'declaration:default_api:file_analyze{focus:"}",max_chars:2000}'
    different = 'declaration:default_api:file_analyze{focus:"}",max_chars:5000}'
    assert not has_textual_tool_declaration(_text_response(supplied), user_text=supplied)
    assert has_textual_tool_declaration(_text_response(different), user_text=supplied)
    assert has_textual_tool_declaration(_text_response(supplied[:-1]), user_text=supplied)


def test_later_bad_response_retries_with_tool_evidence_not_the_tool_action():
    executed = []
    ns, client, _ = _runtime(
        [
            _tool_response("web_search", {"query": "first"}),
            _text_response(RAW_DECLARATION),
            _text_response("پاسخ بر اساس نتیجهٔ جست‌وجو"),
        ],
        handlers={"web_search": lambda args: executed.append(args) or {"ok": True, "evidence": "found", "results": [{"url": "https://example.org/result", "evidence": "Verified text"}]}},
    )
    answer, _, events = ns["ask_gemini"]("test-model", _reply_photo_messages())
    assert answer == "پاسخ بر اساس نتیجهٔ جست‌وجو"
    assert len(executed) == 1 and len(events) == 1
    assert client.models.requests[1][1] == client.models.requests[2][1]
    tool_result = client.models.requests[2][1][-1].parts[0].function_response
    assert tool_result.response["evidence"] == "found"


def test_direct_telegram_photo_uses_inline_vision_and_recovers(isolated_bot):
    import asyncio
    from types import SimpleNamespace

    bot, client = isolated_bot
    client.models.responses = [_text_response(RAW_DECLARATION), _text_response("یک تصویر است.")]
    update, context, _ = _telegram_update(reply_photo=True)
    update.message.reply_to_message = None
    update.message.text = None
    update.message.caption = "این کیه؟"
    update.message.photo = [SimpleNamespace(file_id="photo-id")]
    asyncio.run(bot.message_handler(update, context))

    assert update.message.reply_text.await_args.args[0] == "یک تصویر است."
    for _, contents, config in client.models.requests:
        assert contents[-1].parts[-1].inline_data.data == IMAGE
        assert "file_analyze" not in _declared_names(config)


def test_reply_to_photo_sent_as_document_uses_inline_vision(isolated_bot):
    import asyncio
    from types import SimpleNamespace

    bot, client = isolated_bot
    client.models.responses = [_text_response("یک عکس است.")]
    update, context, _ = _telegram_update(reply_photo=True)
    previous = update.message.reply_to_message
    previous.photo = None
    previous.document = SimpleNamespace(
        file_id="photo-as-file", file_size=len(IMAGE), file_name="photo.jpg", mime_type="image/jpeg"
    )
    asyncio.run(bot.message_handler(update, context))

    context.bot.get_file.assert_awaited_once_with("photo-as-file")
    assert update.message.reply_text.await_args.args[0] == "یک عکس است."
    _, contents, config = client.models.requests[0]
    assert contents[-1].parts[-1].inline_data.data == IMAGE
    assert contents[-1].parts[-1].inline_data.mime_type == "image/jpeg"
    assert "file_analyze" not in _declared_names(config)


def test_reply_caption_cannot_whitelist_a_leaked_declaration(isolated_bot):
    import asyncio

    bot, client = isolated_bot
    client.models.responses = [_text_response(RAW_DECLARATION), _text_response("پاسخ سالم")]
    update, context, _ = _telegram_update(reply_photo=True)
    # The user asks about the picture, not the raw declaration in its caption.
    update.message.reply_to_message.caption = RAW_DECLARATION
    asyncio.run(bot.message_handler(update, context))

    assert len(client.models.requests) == 2
    assert update.message.reply_text.await_args.args[0] == "پاسخ سالم"
    assert RAW_DECLARATION not in bot.db.execute(
        "SELECT content FROM messages WHERE role='assistant'"
    ).fetchone()[0]


def test_telegram_can_discuss_a_declaration_the_current_user_supplied(isolated_bot):
    import asyncio

    bot, client = isolated_bot
    message = f"این یک اعلان ابزار است: `{RAW_DECLARATION}`"
    client.models.responses = [_text_response(message)]
    update, context, _ = _telegram_update()
    update.message.text = f"این یعنی چی؟ {RAW_DECLARATION}"
    asyncio.run(bot.message_handler(update, context))

    assert len(client.models.requests) == 1
    assert RAW_DECLARATION in update.message.reply_text.await_args.args[0]
