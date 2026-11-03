from error_handling import classify_gemini_error, classify_telegram_error, generic_user_error


def test_gemini_503_is_safe_and_not_raw():
    err = classify_gemini_error(503, "503 UNAVAILABLE high demand")
    assert "پاسخ‌گو نیست" in err.message
    assert "503" not in err.message
    assert err.log_level == "warning"


def test_gemini_rate_limit():
    err = classify_gemini_error(429, "resource exhausted")
    assert "زیاد شده" in err.message
    assert err.log_level == "warning"


def test_gemini_auth_error():
    err = classify_gemini_error(401, "invalid api key")
    assert "دسترسی" in err.message
    assert "api key" not in err.message.lower()


def test_gemini_unknown_error_is_generic():
    err = classify_gemini_error(None, "some internal stack trace")
    # The wording names "the model service", not one vendor: Aura can run on
    # Gemini or any OpenAI-compatible gateway.
    assert err.message == "❌ در ارتباط با سرویس مدل خطایی رخ داد. لطفاً دوباره امتحان کن."
    assert "stack trace" not in err.message


def test_telegram_rate_limit():
    err = classify_telegram_error("RetryAfter", "retry after 5")
    assert "محدودیت ارسال" in err.message


def test_telegram_bad_request_is_safe():
    err = classify_telegram_error("BadRequest", "Can't parse reactiontype: secret details")
    assert "پردازش کند" in err.message
    assert "secret" not in err.message


def test_generic_error_does_not_expose_logs():
    err = generic_user_error()
    assert "log" not in err.message.lower()
    assert "جزئیات" not in err.message


def test_errors_are_localized_without_changing_the_default():
    assert "model service" in classify_gemini_error(None, "", lang="en").message
    assert "سرویس مدل" in classify_gemini_error(None, "").message
    assert "Telegram" in classify_telegram_error("Forbidden", "", lang="en").message
