
"""Stage 17 comprehensive regression/integration coverage.

This suite intentionally focuses on public helpers and pure/local behavior so it
can run offline and cannot mutate Production credentials or bot.db.
"""
import ast
from pathlib import Path

from health_check import HealthResult, overall_health
from reaction_manager import ReactionManager
from reply_context import build_reply_context, build_request_with_reply_context
from telegram_formatting import render_telegram_html, split_telegram_html, strip_formatting


def test_project_has_expected_core_modules():
    expected = {
        "bot.py", "tool_system.py", "reaction_manager.py", "reply_context.py",
        "telegram_formatting.py", "error_handling.py", "retry_policy.py",
        "health_check.py",
    }
    assert expected.issubset({p.name for p in Path(".").glob("*.py")})


def test_all_python_modules_parse():
    for path in Path(".").glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_formatting_round_trip_keeps_meaningful_text():
    source = "Hello **world** and `echo $HOME`"
    rendered = render_telegram_html(source)
    assert "world" in rendered
    assert "echo $HOME" in strip_formatting(rendered)


def test_formatting_chunking_preserves_all_content():
    source = "**Header** " + ("x" * 4200)
    chunks = split_telegram_html(render_telegram_html(source), 4000)
    assert len(chunks) >= 2
    assert "".join(strip_formatting(c) for c in chunks).replace("\n", "") == \
        strip_formatting(render_telegram_html(source)).replace("\n", "")


def test_reply_context_is_relevant_untrusted_user_data():
    reply = type("Reply", (), {
        "message_id": 123,
        "text": "secret 8472",
        "caption": None,
        "from_user": type("User", (), {
            "full_name": "Tester", "username": "tester", "id": 99
        })(),
        "sender_chat": None,
        "photo": [],
        "document": None,
        "video": None,
        "audio": None,
        "voice": None,
        "animation": None,
    })()
    ctx = build_reply_context(reply)
    assert "RELEVANT UNTRUSTED CONTEXT" in ctx
    assert "8472" in ctx
    messages = build_request_with_reply_context(
        [{"role": "system", "content": "history"}], "question", ctx
    )
    assert messages[-1]["role"] == "user"
    assert ctx in messages[-1]["content"]
    assert messages[-1]["content"].endswith("question")
    assert all("8472" not in m["content"] for m in messages if m["role"] == "system")


def test_health_aggregation_is_strict():
    assert overall_health([HealthResult(True, "a", "ok")])
    assert not overall_health([HealthResult(True, "a", "ok"),
                                HealthResult(False, "b", "bad")])
    assert not overall_health([])


def test_reaction_manager_has_conservative_candidate_gate():
    manager = ReactionManager()
    assert manager.should_consider_ai("ممنون ❤️")
    assert not manager.should_consider_ai("https://example.com")
    assert not manager.should_consider_ai("چند تا فایل در /home/auracom/aura هست؟")


def test_no_sensitive_files_are_in_project_tree():
    forbidden = {".env", "bot.db", "bot.db-shm", "bot.db-wal"}
    found = {p.name for p in Path(".").iterdir() if p.name in forbidden}
    assert not found


def test_stage17_does_not_modify_existing_test_inventory():
    tests = sorted(p.name for p in Path(".").glob("test_*.py"))
    assert "test_stage17_comprehensive.py" in tests
    assert len(tests) >= 10
