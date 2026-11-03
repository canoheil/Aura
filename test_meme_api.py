"""Offline tests for the meme fetching feature.

Never imports Telegram/Gemini network state. All network I/O is mocked with a
fake ``requests.Session.get`` that returns synthetic JSON + image bytes.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

import meme_api
from meme_api import Meme, MemeClient, MemeError

ROOT = Path(__file__).resolve().parent


# ---------- helpers ----------

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
GIF_BYTES = b"GIF89a" + b"\x00" * 200


class FakeResp:
    def __init__(self, status_code=200, json_data=None, content=b"", iter_chunks=None):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self._chunks = iter_chunks
        self.headers = {}
        self.closed = False

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size=65536):
        if self._chunks is not None:
            yield from self._chunks
        else:
            yield self.content

    def close(self):
        self.closed = True


def _payload(url="https://i.redd.it/test.jpg", **overrides):
    base = {
        "postLink": "https://redd.it/abc",
        "subreddit": "memes",
        "title": "hi",
        "url": url,
        "nsfw": False,
        "spoiler": False,
        "author": "u",
        "ups": 10,
    }
    base.update(overrides)
    return base


# ---------- topic resolution ----------


def test_resolve_topic_subreddits_bird():
    assert meme_api.resolve_topic_subreddits("پرنده")[0] == "birdmemes"


def test_resolve_topic_subreddits_bird_persian_with_request():
    assert meme_api.resolve_topic_subreddits("یه میم پرنده بده")[0] == "birdmemes"


def test_resolve_topic_subreddits_programming():
    assert meme_api.resolve_topic_subreddits("برنامه نویسی")[0] == "ProgrammerHumor"


def test_resolve_topic_subreddits_english_bird():
    assert meme_api.resolve_topic_subreddits("birds are funny")[0] == "birdmemes"


def test_resolve_topic_subreddits_cat():
    assert meme_api.resolve_topic_subreddits("cat meme please")[0] == "catmemes"


def test_resolve_empty_returns_empty():
    assert meme_api.resolve_topic_subreddits("") == ()
    assert meme_api.resolve_topic_subreddits(None) == ()


def test_resolve_bare_subreddit():
    # Subreddit matching is case-insensitive; the API itself is case-insensitive.
    assert meme_api.resolve_topic_subreddits("ProgrammerHumor")[0].lower() == "programmerhumor"
    assert meme_api.resolve_topic_subreddits("r/ProgrammerHumor")[0].lower() == "programmerhumor"


# ---------- request detection ----------


def test_looks_like_meme_request_persian_simple():
    assert meme_api.looks_like_meme_request("یه میم بده") is True
    assert meme_api.looks_like_meme_request("میم بده") is True
    assert meme_api.looks_like_meme_request("یه میم پرنده بده") is True
    assert meme_api.looks_like_meme_request("یه میم بامزه بده") is True


def test_looks_like_meme_request_english():
    assert meme_api.looks_like_meme_request("send me a meme about cats") is True
    assert meme_api.looks_like_meme_request("gimme a meme") is True


def test_looks_like_meme_request_rejects_analysis():
    assert meme_api.looks_like_meme_request("میم یعنی چی؟") is False
    assert meme_api.looks_like_meme_request("این میم رو تحلیل کن") is False


def test_looks_like_meme_request_rejects_creation():
    assert meme_api.looks_like_meme_request("یه میم بساز از گربه") is False
    assert meme_api.looks_like_meme_request("برام یه میم درست کن") is False


def test_looks_like_meme_request_rejects_talk_about():
    assert meme_api.looks_like_meme_request("میم ها خیلی بامزه اند") is False


def test_could_involve_a_meme_is_off_for_ordinary_chat():
    # The advertisement switch must never fire on messages that are not about
    # memes: that is what kept fetch_meme in every request.
    for text in (
        "سلام، چطوری؟",
        "خوبی؟",
        "امروز حالت چطوره",
        "بریم سینما",
        "این چیه؟",
        "بگو ببینم",
        "داشتم میمون می‌دیدم",
        "میمیرم از خستگی",
        "Give me a joke",
        "show me how to code python",
        "What's the weather like?",
        "",
    ):
        assert meme_api.could_involve_a_meme(text) is False, text


def test_could_involve_a_meme_is_on_for_meme_turns():
    for text in (
        "یه میم بده",
        "میم بده",
        "یه میم پرنده بده",
        "send me a meme about cats",
        "میم ها خیلی بامزه اند",
        "این میم رو تحلیل کن",
        "یکی دیگه",
        "باز یکی دیگه",
        "another one",
        "one more please",
    ):
        assert meme_api.could_involve_a_meme(text) is True, text


def test_extract_meme_topic():
    assert meme_api.extract_meme_topic("یه میم پرنده بده") == "پرنده"
    assert meme_api.extract_meme_topic("send me a meme about cats") == "cats"
    assert meme_api.extract_meme_topic("یه میم بده") is None
    assert meme_api.extract_meme_topic("میم") is None


# ---------- Meme dataclass ----------


def test_meme_filename_and_safe_photo():
    m = Meme(data=JPEG_BYTES, mime_type="image/jpeg", subreddit="memes", title="t", author="a", source_url="https://i.redd.it/x.jpg", post_link="https://redd.it/abc", upvotes=1)
    assert m.filename.endswith(".jpg")
    assert m.telegram_safe_photo is True
    assert m.is_animation is False


def test_meme_gif_is_animation():
    m = Meme(data=GIF_BYTES, mime_type="image/gif", subreddit="memes", title="t", author="a", source_url="https://i.redd.it/x.gif", post_link="https://redd.it/abc", upvotes=1)
    assert m.is_animation is True
    assert m.telegram_safe_photo is False


# ---------- MemeClient ----------


def test_candidate_subreddits_explicit():
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), timeout=5)
    assert client.candidate_subreddits(subreddit="ProgrammerHumor") == ("ProgrammerHumor",)
    assert client.candidate_subreddits(topic="cats", subreddit="wholesomememes") == ("wholesomememes",)


def test_candidate_subreddits_invalid_name():
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), timeout=5)
    with pytest.raises(MemeError) as exc:
        client.candidate_subreddits(subreddit="bad name!")
    assert exc.value.code == "bad_subreddit"


def test_candidate_subreddits_allowlist():
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), allowed_subreddits=("wholesomememes",), timeout=5)
    # birdmemes not in allowlist -> should fall back to allowed pool
    candidates = client.candidate_subreddits(topic="پرنده")
    assert "wholesomememes" in candidates
    assert "birdmemes" not in candidates


def test_fetch_success():
    sess = Mock()
    pl = _payload()
    sess.get.side_effect = [FakeResp(json_data=pl), FakeResp(content=JPEG_BYTES, iter_chunks=[JPEG_BYTES])]
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), session=sess, timeout=5)
    meme = client.fetch(subreddit="memes")
    assert meme.title == "hi"
    assert meme.mime_type == "image/jpeg"
    assert len(meme.data) > 0


def test_fetch_rejects_nsfw():
    sess = Mock()
    pl = _payload(nsfw=True)
    sess.get.side_effect = [FakeResp(json_data=pl)]
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), session=sess, allow_nsfw=False, timeout=5)
    with pytest.raises(MemeError) as exc:
        client.fetch(subreddit="memes")
    assert exc.value.code == "nsfw"


def test_fetch_rerolls_a_rollable_failure():
    # NSFW is per post, not per subreddit: asking again can bring a clean meme.
    sess = Mock()
    bad = _payload(nsfw=True)
    good = _payload(url="https://i.redd.it/clean.jpg", title="clean")
    sess.get.side_effect = [
        FakeResp(json_data=bad),
        FakeResp(json_data=good),
        FakeResp(content=JPEG_BYTES, iter_chunks=[JPEG_BYTES]),
    ]
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), session=sess, timeout=5)
    meme = client.fetch(subreddit="memes", attempts_per_candidate=2)
    assert meme.source_url == "https://i.redd.it/clean.jpg"
    assert sess.get.call_count == 3


def test_fetch_rejects_video_url():
    sess = Mock()
    pl = _payload(url="https://v.redd.it/abc123")
    sess.get.side_effect = [FakeResp(json_data=pl)]
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), session=sess, timeout=5)
    with pytest.raises(MemeError) as exc:
        client.fetch(subreddit="memes")
    assert exc.value.code == "not_image"


def test_fetch_rejects_too_large():
    sess = Mock()
    pl = _payload()
    big = b"\xff\xd8\xff\xe0" + b"0" * (7 * 1024 * 1024)
    sess.get.side_effect = [
        FakeResp(json_data=pl),
        FakeResp(content=big, iter_chunks=[big[: 1024 * 1024], big[1024 * 1024 :]]),
    ]
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), session=sess, max_bytes=6 * 1024 * 1024, timeout=5)
    with pytest.raises(MemeError) as exc:
        client.fetch(subreddit="memes")
    assert exc.value.code == "too_large"


def test_fetch_dedup():
    sess = Mock()
    pl1 = _payload(url="https://i.redd.it/dup.jpg")
    pl2 = _payload(url="https://i.redd.it/new.jpg", title="new")
    # first fetch returns dup.jpg
    sess.get.side_effect = [FakeResp(json_data=pl1), FakeResp(content=JPEG_BYTES, iter_chunks=[JPEG_BYTES])]
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), session=sess, timeout=5)
    m1 = client.fetch(subreddit="memes")
    assert m1.source_url == "https://i.redd.it/dup.jpg"
    # The same dup.jpg comes back, but its bytes are not downloaded again: the
    # known URL is skipped straight away and the next roll is asked instead.
    sess.get.side_effect = [
        FakeResp(json_data=pl1),
        FakeResp(json_data=pl2),
        FakeResp(content=PNG_BYTES, iter_chunks=[PNG_BYTES]),
    ]
    sess.get.reset_mock()
    m2 = client.fetch(subreddit="memes", attempts_per_candidate=2)
    assert m2.source_url == "https://i.redd.it/new.jpg"
    assert sess.get.call_count == 3, "the repeated image must not be downloaded again"


def test_fetch_fallback_url():
    sess = Mock()
    pl = _payload()
    # primary base fails with 500, the mirror succeeds on the very next try
    sess.get.side_effect = [
        FakeResp(status_code=500, json_data=None),
        FakeResp(json_data=pl),
        FakeResp(content=JPEG_BYTES, iter_chunks=[JPEG_BYTES]),
    ]
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=("https://meme-api.aelx.de",), session=sess, timeout=5)
    meme = client.fetch(subreddit="memes")
    assert meme.title == "hi"
    assert sess.get.call_count == 3
    assert sess.get.call_args_list[1].args[0].startswith("https://meme-api.aelx.de")


def test_fetch_does_not_hang_on_a_rate_limiting_base():
    # 429 says "not now", not "this subreddit is wrong": go to the mirror at once.
    sess = Mock()
    pl = _payload()
    sess.get.side_effect = [
        FakeResp(status_code=429, json_data=None),
        FakeResp(json_data=pl),
        FakeResp(content=JPEG_BYTES, iter_chunks=[JPEG_BYTES]),
    ]
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=("https://meme-api.aelx.de",), session=sess, timeout=5)
    meme = client.fetch(topic="cats")
    assert meme.title == "hi"
    assert sess.get.call_args_list[1].args[0].startswith("https://meme-api.aelx.de")


def test_fetch_does_not_retry_an_unknown_subreddit():
    sess = Mock()
    sess.get.side_effect = [FakeResp(status_code=404, json_data=None)]
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), session=sess, timeout=5)
    with pytest.raises(MemeError) as exc:
        client.fetch(subreddit="nonexistentsubreddit123", attempts_per_candidate=2)
    assert exc.value.code == "not_found"
    assert sess.get.call_count == 1, "a missing subreddit cannot be fixed by asking again"


def test_fetch_404_maps_to_not_found():
    sess = Mock()
    sess.get.side_effect = [FakeResp(status_code=404, json_data=None)]
    client = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), session=sess, timeout=5)
    with pytest.raises(MemeError) as exc:
        client.fetch(subreddit="nonexistentsubreddit123")
    assert exc.value.code == "not_found"


def test_fetch_stops_at_the_time_budget(monkeypatch):
    # A slow service must be cut off by the total deadline, not only by the
    # per-request timeout: this fake spends 4s on every single call.
    clock = {"now": 1000.0}
    monkeypatch.setattr(meme_api.time, "monotonic", lambda: clock["now"])

    def slow_500(*args, **kwargs):
        clock["now"] += 4.0
        return FakeResp(status_code=500, json_data=None)

    sess = Mock()
    sess.get.side_effect = slow_500
    client = MemeClient(
        enabled=True,
        base_urls=("https://meme-api.com",),
        fallback_urls=(),
        session=sess,
        timeout=12,
        fetch_budget_seconds=10,
    )
    with pytest.raises(MemeError):
        client.fetch(topic="cats")
    # 10s budget / 4s per call: three calls fit, the fourth would overrun it.
    assert sess.get.call_count == 3, sess.get.call_count


def test_fetch_budget_defaults_and_clamps():
    assert meme_api.DEFAULT_FETCH_BUDGET_SECONDS == 20.0
    default = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=())
    assert default.fetch_budget_seconds == 20.0
    tiny = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), fetch_budget_seconds=1)
    assert tiny.fetch_budget_seconds == 5.0
    huge = MemeClient(enabled=True, base_urls=("https://meme-api.com",), fallback_urls=(), fetch_budget_seconds=999)
    assert huge.fetch_budget_seconds == 60.0


def test_meme_disabled():
    client = MemeClient(enabled=False, base_urls=("https://meme-api.com",), fallback_urls=(), timeout=5)
    assert client.is_enabled is False
    with pytest.raises(MemeError) as exc:
        client.fetch(subreddit="memes")
    assert exc.value.code == "disabled"


# ---------- bot integration (source checks, no Telegram import) ----------


def test_tool_system_has_cache_results_flag():
    source = (ROOT / "tool_system.py").read_text(encoding="utf-8")
    assert "cache_results" in source
    assert "if tool.cache_results" in source


def test_bot_registers_fetch_meme_without_cache():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert 'name="fetch_meme"' in source
    assert "cache_results=False" in source
    assert "_MEME_TOOL_DESCRIPTION" in source
    assert "def _build_fetch_meme_declaration" in source
    assert "def _tool_fetch_meme" in source


def test_bot_vision_injection():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "_pop_tool_vision_payload" in source
    assert "_model_image" in source
    assert "vision_media" in source


def test_bot_meme_prompt_section():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "def _memes_prompt_section" in source
    assert "fetch a real meme" in source.lower()


def test_bot_advertises_the_meme_tool_only_on_meme_turns():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "could_involve_a_meme" in source
    # ask_gemini forwards the per-turn switch into the config builder.
    assert "allow_meme_tool=allow_meme_tool" in source
    assert "allow_meme_tool=meme_turn" in source
    # the declaration is filtered out, and a hallucinated call is refused.
    assert 'item.name != "fetch_meme"' in source
    assert 'args.get("_meme_allowed") is False' in source
    assert 'runtime_context["meme_allowed"]' in source


def test_bot_does_not_paint_the_meme_section_into_every_prompt():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    build = source.split("def build_system_prompt")[1].split("def is_founder")[0]
    # The standing system prompt must not describe the meme tool: only a turn
    # that can use it may mention it.
    assert "_memes_prompt_section()" not in build
    # ... and the section is injected per turn instead.
    handler = source.split("async def message_handler")[1]
    assert "could_involve_a_meme(text)" in handler
    assert "_memes_prompt_section()" in handler


def test_bot_meme_env_validation():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "MEMES_ENABLED" in source
    assert "MEME_API_BASE_URL" in source
    assert "MEME_ALLOW_NSFW" in source
    assert "MEME_MAX_BYTES" in source


def test_bot_passes_the_fetch_budget_to_the_client():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert 'os.getenv("MEME_FETCH_BUDGET_SECONDS", "20")' in source
    assert "MEME_FETCH_BUDGET_SECONDS must be between 5 and 60 seconds" in source
    assert "fetch_budget_seconds=MEME_FETCH_BUDGET_SECONDS" in source
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "MEME_FETCH_BUDGET_SECONDS=20" in env


def test_bot_delivery_handles_meme():
    assert "is_animation" in (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "reply_animation" in (ROOT / "bot.py").read_text(encoding="utf-8")


def test_env_example_has_meme_vars():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ("MEMES_ENABLED", "MEME_API_BASE_URL", "MEME_API_FALLBACK_URL", "MEME_MAX_BYTES", "MEME_TOOL_MAX_CALLS"):
        assert var in env


def test_normalize_subreddit():
    assert meme_api.normalize_subreddit("r/ProgrammerHumor") == "ProgrammerHumor"
    assert meme_api.normalize_subreddit("/r/memes/") == "memes"
    assert meme_api.normalize_subreddit("  memes  ") == "memes"
