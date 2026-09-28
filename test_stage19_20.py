from pathlib import Path


def test_pinned_requirements_are_exact_and_match_runtime_requirements():
    lines = [x.strip() for x in Path("requirements-pinned.txt").read_text().splitlines() if x.strip() and not x.startswith("#")]
    assert len(lines) == 8
    assert all("==" in line and ">" not in line and "<" not in line for line in lines)
    base = [x.strip() for x in Path("requirements.txt").read_text().splitlines() if x.strip()]
    names = {x.split("==")[0].split(">=")[0].split("<")[0] for x in lines}
    assert names == {x.split(">=")[0].split("<")[0] for x in base}


def test_observability_helpers_are_dependency_free():
    from observability import RequestTrace
    trace = RequestTrace.start()
    assert len(trace.request_id) == 12
    assert trace.latency_ms >= 0


def test_bot_contains_request_observability_without_sensitive_payload_logging():
    source = Path("bot.py").read_text()
    assert "Message lifecycle completed" in source
    assert "Gemini request completed" in source
    assert "TELEGRAM_BOT_TOKEN" not in source[source.index("Gemini request completed"):source.index("Gemini request completed") + 300]
