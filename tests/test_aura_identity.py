from aura_identity import AuraIdentity


def test_core_identity_is_authoritative_and_separates_model_from_aura():
    ctx = AuraIdentity(model="gemini-3.1-flash-lite").system_context()
    assert "Name: Aura" in ctx
    assert "Runtime: Telegram" in ctx
    assert "Google Gemini" in ctx
    assert "LLM provider and model are components Aura uses, not Aura's identity" in ctx
    assert "Aura is Google, Gemini" in ctx
    assert "created by the LLM provider" in ctx


def test_founder_context_does_not_expose_id():
    ctx = AuraIdentity().system_context(1733958251)
    assert "verified founder/creator relationship" in ctx
    assert "1733958251" not in ctx


def test_identity_is_immutable():
    identity = AuraIdentity()
    try:
        identity.name = "Gemini"
        changed = True
    except Exception:
        changed = False
    assert changed is False
