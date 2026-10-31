import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
from unittest.mock import Mock

from chat_update_processor import ChatUpdateProcessor
from meme_api import MemeClient
from weather_api import WeatherClient
from test_agent_tool_response import isolated_bot


def _update(chat_id):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), effective_user=None)


def test_different_chats_progress_and_same_chat_stays_in_order():
    async def run():
        processor = ChatUpdateProcessor(3)
        first_started, release, second_started, other_done = (asyncio.Event() for _ in range(4))
        order = []

        async def first():
            order.append("first-start")
            first_started.set()
            await release.wait()
            order.append("first-end")

        async def second():
            order.append("second")
            second_started.set()

        async def other():
            order.append("other")
            other_done.set()

        t1 = asyncio.create_task(processor.process_update(_update(1), first()))
        await asyncio.wait_for(first_started.wait(), 1)
        t2 = asyncio.create_task(processor.process_update(_update(1), second()))
        t3 = asyncio.create_task(processor.process_update(_update(2), other()))
        await asyncio.wait_for(other_done.wait(), 1)
        assert not second_started.is_set()
        release.set()
        await asyncio.gather(t1, t2, t3)
        assert order.index("first-end") < order.index("second")
        assert processor._locks == {}

    asyncio.run(run())


def test_maximum_number_of_updates_and_cancelled_waiter_cleanup():
    async def run():
        processor = ChatUpdateProcessor(2)
        release, two_started = asyncio.Event(), asyncio.Event()
        active = 0
        highwater = 0

        async def work():
            nonlocal active, highwater
            active += 1
            highwater = max(active, highwater)
            if active == 2:
                two_started.set()
            try:
                await release.wait()
            finally:
                active -= 1

        tasks = [asyncio.create_task(processor.process_update(_update(i), work())) for i in range(3)]
        await asyncio.wait_for(two_started.wait(), 1)
        assert highwater == 2
        release.set()
        await asyncio.gather(*tasks)
        assert highwater == 2
        assert processor._locks == {}

        first_started, release2 = asyncio.Event(), asyncio.Event()

        async def holder():
            first_started.set()
            await release2.wait()

        first = asyncio.create_task(processor.process_update(_update(9), holder()))
        await first_started.wait()
        waiter = asyncio.create_task(processor.process_update(_update(9), work()))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        release2.set()
        await first
        assert processor._locks == {}

    asyncio.run(run())


def test_weather_and_meme_owned_sessions_are_per_thread():
    clients = (WeatherClient(), MemeClient())
    barrier = threading.Barrier(2)

    def get_sessions():
        sessions = tuple(client.session for client in clients)
        barrier.wait(timeout=2)
        return sessions

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(get_sessions)
        second = pool.submit(get_sessions)
        left, right = first.result(), second.result()
    assert all(a is not b for a, b in zip(left, right))


def test_concurrent_paid_searches_cannot_overshoot_daily_limit(isolated_bot, monkeypatch):
    bot, _ = isolated_bot
    monkeypatch.setattr(bot, "YOU_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "WEB_SEARCH_DAILY_LIMIT", 1)
    first_entered, release = threading.Event(), threading.Event()
    response = Mock(status_code=200)
    response.json.return_value = {"results": {"web": [{"url": "https://example.org/", "title": "Result"}]}}
    calls = []

    def post(*_args, **_kwargs):
        calls.append(1)
        first_entered.set()
        assert release.wait(2)
        return response

    monkeypatch.setattr(bot.requests, "post", post)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bot._tool_web_search, {"query": "one", "freshness": "day"})
        assert first_entered.wait(2)
        second = pool.submit(bot._tool_web_search, {"query": "two", "freshness": "day"})
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]
    assert len(calls) == 1
    assert bot.get_web_search_usage() == 1
    assert sorted(item["ok"] for item in results) == [False, True]


def test_application_uses_bounded_processor(isolated_bot, monkeypatch):
    bot, _ = isolated_bot
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "123456:dummy")
    application = bot.build_application()
    assert isinstance(application.update_processor, ChatUpdateProcessor)
    assert application.update_processor.max_concurrent_updates == bot.MAX_CONCURRENT_CHATS
    assert application.post_init is bot.application_post_init
    assert application.post_stop is bot.stop_reminder_scheduler
    assert application.post_shutdown is bot.stop_reminder_scheduler
