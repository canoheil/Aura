"""Offline tests for the web-search freshness gate.

``freshness_policy`` is a pure classifier, so it is imported directly. The
bot-side decisions (downgrades, the forced generation config, prompt gating) are
checked by extracting those functions from bot.py source, matching the repo's
convention of never importing bot.py in tests.
"""
from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
from unittest import mock

import pytest

import freshness_policy as fp
from freshness_policy import LEVEL_FORCE, LEVEL_HINT, LEVEL_OFF

ROOT = Path(__file__).resolve().parent


# ---------- the three levels ----------

@pytest.mark.parametrize("text", [
    "قیمت دلار چنده",
    "قیمت طلا امروز چند است",
    "نرخ یورو چقدر شده",
    "بیت کوین چند دلار است",
    "طلا چند شده",
    "قیمت مسکن در تهران چنده",
    "بلیط هواپیما چنده",
    "آخرین نسخه پایتون چیست",
    "what is the latest python version",
    "is python 2 still supported",
    "آپدیت جدید وارفریم چی اضافه کرد",
    "جدیدترین اخبار ایران",
    "news about openai today",
    "دیروز بازی چی شد",
    "چند روز پیش زلزله تهران چی شد",
    "نتیجه بازی پرسپولیس دیروز",
    "جدول لیگ برتر چطوره",
    "انتخابات آمریکا کی برگزار میشه",
    "رئیس جمهور آمریکا کیه",
    "who is the prime minister of uk",
    "مدیرعامل تلگرام کیه",
])
def test_time_dependent_questions_are_forced(text):
    assert fp.classify(text).level == LEVEL_FORCE, text


@pytest.mark.parametrize("text", [
    "قیمت چنده",          # an ask with no subject: ask back, do not pay
    "چنده",
    "چند شده",
    "به روز رسانی شد یا نه",
    "آیا هنوز پشتیبانی میشه",
    "خبر داری چی شد؟",
    "اون خبر رو دیدی؟",
    "وضعیت سرور چطوره",
    "is it open now",
])
def test_ambiguous_freshness_only_nudges(text):
    assert fp.classify(text).level == LEVEL_HINT, text


@pytest.mark.parametrize("text", [
    "سلام",
    "خوبی؟",
    "مرسی",
    "آره",
    "نه بابا ولش کن",
    "دیشب خواب دیدم",
    "سردمه",
    "هوا سرده",
    "اسمم چیه",
    "فردا میام",
    "ساعت چنده",
    "تاریخ چندم",
    "معنی کلمه perseverance",
    "difference between list and tuple in python",
    "redux چطور کار می‌کند",
    "یه داستان کوتاه بگو",
    "کد fibonacci بنویس",
    "ترجمه this sentence",
    "خلاصه کن",
    "چرا آسمون آبیه",
    "خطای این کد چیه",
    "react هوک چیه",
    "2+2 چند میشه",
    "برنامه‌نویسی چطوره",
    # owned by another tool
    "هوای تهران چطوره",
    "آب و هوا مشهد",
    "weather in berlin",
    "یه میم بده",
    "میم پرنده بفرست",
    "استیکر بفرست",
    # media asks
    "عکس گربه بساز",
    "لوگو بساز",
    "generate an image of a cat",
])
def test_stable_chat_and_other_tools_never_search(text):
    assert fp.classify(text).level == LEVEL_OFF, text


def test_explicit_search_command_overrides_every_veto():
    # Same words that would be off ("is stable") become a real request.
    for text in ("سرچ کن ببین قیمت تتر چنده", "search: iphone 17 release date", "جستجو: airbnb"):
        decision = fp.classify(text)
        assert decision.level == LEVEL_FORCE, text
    # even a "translate this" style veto cannot block an explicit ask
    assert fp.classify("ترجمه‌ش کن ولی اول سرچ کن").level == LEVEL_FORCE


def test_empty_and_noise():
    assert fp.classify("").level == LEVEL_OFF
    assert fp.classify("   ").level == LEVEL_OFF
    assert fp.classify("!؟;.").level == LEVEL_OFF


def test_deployment_extra_terms_force():
    assert fp.classify("وضعیت سرور ابریشم").level != LEVEL_FORCE
    decision = fp.classify("وضعیت سرور ابریشم", extra_force_terms=("ابریشم",))
    assert decision.level == LEVEL_FORCE
    assert decision.reason == "deployment"


def test_decision_helpers():
    force = fp.classify("قیمت دلار چنده")
    assert force.should_force and force.should_nudge
    off = fp.classify("سلام")
    assert not off.should_nudge and not off.should_force
    assert "level=force" in force.as_log_fields()


# ---------- vocabulary hygiene ----------

def test_persian_terms_survive_normalization():
    """A term that normalization would rewrite can never match, so it is dead weight."""
    for terms in (fp._SUBJECT_HINTS, fp._STABLE_ASKS, fp._OTHER_TOOL_TERMS, fp._CREATION_TERMS):
        for term in terms:
            assert fp.normalize(term) == term, term


def test_subject_detection_needs_something_to_look_up():
    # Policy vocabulary alone is not a subject; a named thing (from the curated
    # list or any content word) is.
    assert fp.content_tokens("قیمت دلار چنده") == []
    assert not fp.has_subject("قیمت چنده")
    assert fp.has_subject("قیمت مسکن چنده")          # curated subject
    assert fp.has_subject("قیمت اپیلاتور براون چنده")  # unknown noun still counts
    assert not fp.has_subject("چند شده")


def test_long_message_is_truncated_not_rejected():
    long = "قیمت دلار چنده " + ("بلا " * 400)
    assert fp.classify(long).level == LEVEL_FORCE


# ---------- bot-side decisions ----------

def _source_and_functions():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    funcs = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return source, funcs


def _exec(name, namespace):
    _, funcs = _source_and_functions()
    module = ast.Module(body=[funcs[name]], type_ignores=[])
    exec(compile(module, f"<bot.py:{name}>", "exec"), namespace)
    return namespace[name]


def test_soft_limit_derives_from_daily_limit():
    source, _ = _source_and_functions()
    assert "WEB_SEARCH_ENFORCE_SOFT_LIMIT" in source
    ns = {"WEB_SEARCH_ENFORCE_SOFT_LIMIT": 0, "WEB_SEARCH_DAILY_LIMIT": 100}
    assert _exec("_enforce_soft_limit", ns)() == 80
    ns = {"WEB_SEARCH_ENFORCE_SOFT_LIMIT": 12, "WEB_SEARCH_DAILY_LIMIT": 100}
    assert _exec("_enforce_soft_limit", ns)() == 12
    # unlimited daily quota means no derived forcing ceiling either
    ns = {"WEB_SEARCH_ENFORCE_SOFT_LIMIT": 0, "WEB_SEARCH_DAILY_LIMIT": 0}
    assert _exec("_enforce_soft_limit", ns)() == 0


def test_freshness_downgrades_when_search_cannot_run():
    ns = {
        "freshness_policy": fp,
        "WEB_SEARCH_ENFORCE": False,
        "WEB_SEARCH_ENABLED": True,
        "YOU_API_KEY": "k",
        "TOOL_REGISTRY": mock.Mock(names=lambda: ["web_search"]),
        "WEB_SEARCH_FORCE_TERMS": (),
        "_search_lookup_available": lambda: True,
        "_enforce_soft_limit": lambda: 80,
        "get_web_search_usage": lambda: 0,
        "logger": logging.getLogger("t"),
    }
    downgrade = _exec("freshness_for", ns)
    assert downgrade("قیمت دلار چنده").level == LEVEL_HINT
    assert downgrade("قیمت دلار چنده").reason.endswith("-switch-off")

    ns["WEB_SEARCH_ENFORCE"] = True
    ns["_search_lookup_available"] = lambda: False
    assert downgrade("قیمت دلار چنده").reason.endswith("-unavailable")

    ns["_search_lookup_available"] = lambda: True
    ns["get_web_search_usage"] = lambda: 999
    assert downgrade("قیمت دلار چنده").reason.endswith("-ceiling")

    ns["get_web_search_usage"] = lambda: 5
    assert downgrade("قیمت دلار چنده").level == LEVEL_FORCE


def test_forced_config_only_when_required():
    """The mandatory mode is attached to the forcing round and to nothing else."""
    from google.genai import types

    ns = {
        "types": types,
        "TOOL_REGISTRY": mock.Mock(declarations=lambda: []),
    }
    build = _exec("_agent_generation_config", ns)
    plain = build("sys", require_web_search=False)
    forced = build("sys", require_web_search=True)
    assert plain.tool_config is None
    cfg = forced.tool_config.function_calling_config
    assert str(cfg.mode).endswith("ANY")
    assert list(cfg.allowed_function_names) == ["web_search"]
    assert plain.tools == forced.tools


def test_agent_loop_unforces_after_first_round():
    source, _ = _source_and_functions()
    assert "require_web_search: bool = False" in source
    assert "if round_number and forced_search_round:" in source
    # A forced round that errors must never break the reply.
    assert "continuing without forcing" in source
    assert 'operation_name=f"agent-round-{round_number}-unforced"' in source


def test_gate_runs_only_for_unowned_turns():
    source, funcs = _source_and_functions()
    handler = ast.unparse(funcs["message_handler"])
    assert "dedicated_tool_turn" in handler
    assert "require_web_search=require_web_search" in handler
    # comments are dropped by unparse, so the gate body is read from the file
    gate = source.split("Freshness gate:")[1].split("# Private runtime context")[0]
    assert "if text and not dedicated_tool_turn:" in gate
    assert "require_web_search = True" in gate
    assert 'logger.info("Freshness gate |' in gate
    # a media/document/video turn can never be forced into a search
    assert "image_requested or document_requested or media_requested or recent_image or reply_has_media or active_video_url" in source
    # weather and meme turns mark themselves owned before the gate runs
    weather_block = source.split("The same nudge for weather")[1].split("The same nudge for memes")[0]
    meme_block = source.split("The same nudge for memes")[1].split("Freshness gate")[0]
    assert "dedicated_tool_turn = True" in weather_block
    assert "dedicated_tool_turn = True" in meme_block


def test_prompt_sections_are_gated():
    source, funcs = _source_and_functions()
    build = ast.unparse(funcs["build_system_prompt"])
    assert "_date_anchor_section()" in build
    assert 'if WEB_SEARCH_ENABLED and YOU_API_KEY' in build.split("_date_anchor_section")[1][:400]
    assert "_freshness_prompt_section()" in build
    freshness_src = ast.unparse(funcs["_freshness_prompt_section"])
    for needle in ("prices", "releases", "standings", "Do not search for", "quota spent"):
        assert needle in freshness_src
    date_src = ast.unparse(funcs["_date_anchor_section"])
    assert "Today is" in date_src and "UTC" in date_src


def test_search_tool_description_states_the_duty():
    source, funcs = _source_and_functions()
    body = ast.unparse(funcs["_build_tool_search_declaration"])
    assert "Your training data has a fixed end date" in body
    assert "answer nothing" in body


def test_env_example_documents_the_switches():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ("WEB_SEARCH_ENFORCE", "WEB_SEARCH_ENFORCE_SOFT_LIMIT", "WEB_SEARCH_FORCE_TERMS"):
        assert var in env


def test_config_validation_rejects_nonsense():
    source, _ = _source_and_functions()
    assert "WEB_SEARCH_ENFORCE_SOFT_LIMIT cannot be negative" in source
    assert "WEB_SEARCH_ENFORCE_SOFT_LIMIT cannot exceed WEB_SEARCH_DAILY_LIMIT" in source


def test_no_network_or_io_inside_the_classifier():
    """The policy layer must stay importable offline, like the other tool modules."""
    source = (ROOT / "freshness_policy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "dataclasses", "logging", "re", "typing"}, imported


# ---------- the forced round, end to end ----------

def _agent_namespace(*, client, registry, runner, max_rounds=3):
    """Run bot.py's real agent loop against fakes, without importing bot.py.

    Importing bot.py would open bot.db in the project tree, so the five functions
    the loop needs are extracted from the source and executed with the surrounding
    globals supplied by hand.
    """
    import base64
    from google.genai import types

    from retry_policy import call_with_retry, extract_status_code
    from tool_system import AgentRunState, has_textual_tool_declaration
    from answer_quality import has_web_evidence, has_any_web_evidence
    from observability import RequestTrace

    _, funcs = _source_and_functions()
    ns = {
        "types": types,
        "base64": base64,
        "Optional": __import__("typing").Optional,
        "AGENT_MAX_ROUNDS": max_rounds,
        "GEMINI_MAX_RETRIES": 0,
        "GEMINI_CLIENT": client,
        "AGENT_RUNNER": runner,
        "TOOL_REGISTRY": registry,
        "AgentRunState": AgentRunState,
        "has_textual_tool_declaration": has_textual_tool_declaration,
        "has_web_evidence": has_web_evidence,
        "has_any_web_evidence": has_any_web_evidence,
        "RequestTrace": RequestTrace,
        "call_with_retry": call_with_retry,
        "extract_status_code": extract_status_code,
        "logger": logging.getLogger("test-freshness"),
        "GeminiAPIError": type("GeminiAPIError", (Exception,), {}),
    }
    for name in (
        "_agent_generation_config",
        "_gemini_contents_from_messages",
        "_pop_tool_vision_payload",
        "_tool_response_payload",
        "ask_gemini",
    ):
        module = ast.Module(body=[funcs[name]], type_ignores=[])
        exec(compile(module, f"<bot.py:{name}>", "exec"), ns)
    return ns


class _FakeModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.configs = []

    def generate_content(self, *, model, contents, config):
        self.configs.append(config)
        outcome = self._responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, responses):
        self.models = _FakeModels(responses)


class _FakeRegistry:
    def names(self):
        return ["web_search", "get_weather"]

    def declarations(self):
        return []


class _FakeRunner:
    def __init__(self):
        self.calls = []

    def execute_tool(self, state, name, args):
        self.calls.append((name, dict(args)))
        result = {"ok": True, "results": [{"title": "طلای ۱۸ عیار", "url": "https://example.org/gold", "snippet": "هر گرم ۴٬۲۰۰٬۰۰۰ ریال"}]}
        state.events.append({"tool": name, "result": result})
        return result


def _text_response(text):
    return mock.Mock(text=text, usage_metadata=None, function_calls=[], candidates=[])


def _tool_call_response(name, args):
    call = mock.Mock()
    call.name = name
    call.args = args
    call.id = "call-1"
    response = mock.Mock()
    response.function_calls = [call]
    response.usage_metadata = None
    response.text = None
    response.candidates = [mock.Mock(content=mock.Mock(role="model", parts=[]))]
    return response


def _forced(config):
    tool_config = getattr(config, "tool_config", None)
    if tool_config is None:
        return None
    return tool_config.function_calling_config


def test_first_round_is_forced_and_later_rounds_are_free():
    client = _FakeClient([
        _tool_call_response("web_search", {"query": "قیمت طلا"}),
        _text_response("امروز هر گرم طلای ۱۸ عیار ۴٬۲۰۰٬۰۰۰ ریال است."),
    ])
    runner = _FakeRunner()
    ns = _agent_namespace(client=client, registry=_FakeRegistry(), runner=runner)
    answer, _usage, events = ns["ask_gemini"](
        "test-model", [{"role": "user", "content": "قیمت طلا چنده"}], require_web_search=True
    )
    assert "۴٬۲۰۰٬۰۰۰" in answer
    assert runner.calls and runner.calls[0][0] == "web_search"
    assert len(events) == 1
    forced = _forced(client.models.configs[0])
    assert forced is not None, "the first round must force a search"
    assert list(forced.allowed_function_names) == ["web_search"]
    assert str(forced.mode).endswith("ANY")
    assert _forced(client.models.configs[1]) is None, "later rounds must stay free"


def test_no_forcing_when_the_gate_is_off():
    client = _FakeClient([_text_response("سلام")])
    ns = _agent_namespace(client=client, registry=_FakeRegistry(), runner=_FakeRunner())
    answer, _usage, _events = ns["ask_gemini"]("test-model", [{"role": "user", "content": "سلام"}])
    assert answer == "سلام"
    assert all(_forced(config) is None for config in client.models.configs)


def test_forcing_never_breaks_the_reply():
    """An upstream that rejects the mandatory mode falls back to a normal round."""
    client = _FakeClient([
        RuntimeError("400 cannot use ANY mode here"),
        _text_response("قیمت طلا حدود ۴٬۲۰۰٬۰۰۰ ریال است."),
    ])
    ns = _agent_namespace(client=client, registry=_FakeRegistry(), runner=_FakeRunner())
    answer, _usage, _events = ns["ask_gemini"](
        "test-model", [{"role": "user", "content": "قیمت طلا چنده"}], require_web_search=True
    )
    assert "۴٬۲۰۰٬۰۰۰" not in answer
    assert "نمی‌تونم" in answer
    assert len(client.models.configs) >= 2
    assert _forced(client.models.configs[-1]) is None


def test_forcing_is_skipped_when_search_tool_is_absent():
    client = _FakeClient([_text_response("می‌دونم")])
    registry = mock.Mock(names=lambda: ["get_weather"], declarations=lambda: [])
    ns = _agent_namespace(client=client, registry=registry, runner=_FakeRunner())
    ns["ask_gemini"]("test-model", [{"role": "user", "content": "قیمت طلا"}], require_web_search=True)
    assert all(_forced(config) is None for config in client.models.configs)


def test_personal_identity_recall_is_not_a_forced_web_query():
    assert not fp.classify("یادم بیار تو کی هستی").should_force
    assert fp.classify("یادم بیار قیمت دلار چنده").should_force
