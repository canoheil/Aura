from aura_self_identity import AuraSelfIdentityBoundary


def test_self_identity_separates_aura_from_gemini_and_google():
    ctx = AuraSelfIdentityBoundary().system_context()
    assert "Aura is not Google and Aura is not Gemini." in ctx
    assert "LLM provider used by the application: Google Gemini" in ctx
    assert "underlying component" in ctx


def test_self_identity_explains_telegram_runtime():
    ctx = AuraSelfIdentityBoundary().system_context()
    assert "Runtime platform: Telegram" in ctx
    assert "connected the Aura application to Telegram" in ctx


def test_self_identity_does_not_invent_creator_name():
    ctx = AuraSelfIdentityBoundary().system_context()
    assert "developer/application creator" in ctx
    assert "Canoheil" not in ctx


def test_bot_injects_boundary():
    source = open("bot.py", encoding="utf-8").read()
    assert "AuraSelfIdentityBoundary" in source
    assert 'AuraSelfIdentityBoundary().system_context()' in source
