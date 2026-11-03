from memory_bridge import MemoryBridge, looks_like_identity_claim


def test_identity_claims_are_detected():
    assert looks_like_identity_claim("من ویزاردم")
    assert looks_like_identity_claim("My name is Wizard")
    assert looks_like_identity_claim("آیدی من @foo است")


def test_normal_memory_is_allowed():
    ok, reason = MemoryBridge().validate_memory_content("من پایتون را دوست دارم")
    assert ok and reason == "من پایتون را دوست دارم"


def test_identity_memory_is_rejected():
    ok, reason = MemoryBridge().validate_memory_content("من اسمم Wizard است")
    assert not ok and reason == "identity_claim"


def test_owner_is_message_scoped():
    bridge = MemoryBridge()
    assert bridge.resolve_owner(speaker_user_id=10, explicit_owner_user_id=10).user_id == 10
    assert bridge.resolve_owner(speaker_user_id=10, explicit_owner_user_id=20) is None
    assert bridge.resolve_owner(speaker_user_id=None) is None
