from aura_personality_boundary import AuraPersonalityBoundary


def test_personality_does_not_override_facts():
    text = AuraPersonalityBoundary().system_context()
    assert "Personality affects tone" in text
    assert "does not change facts" in text


def test_no_fabricated_experiences_or_memories():
    text = AuraPersonalityBoundary().system_context()
    assert "Never invent memories" in text
    assert "fictional experiences as real events" in text


def test_identity_claims_need_authoritative_context():
    text = AuraPersonalityBoundary().system_context()
    assert "authoritative application context verifies it" in text
    assert "roleplay" in text


def test_previous_false_response_can_be_corrected():
    text = AuraPersonalityBoundary().system_context()
    assert "correct it plainly" in text


def test_memory_is_not_identity_authority():
    text = AuraPersonalityBoundary().system_context()
    assert "memory stores context" in text
    assert "authoritative identity comes from the application identity layer" in text
