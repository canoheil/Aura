from identity_models import IdentityObservation, PersonIdentity, PersonIdentityStore


def test_telegram_id_is_anchor_and_names_are_metadata():
    p = PersonIdentity(42, canonical_name="Wizard", aliases=("wiz", "Wizard"), confidence=2)
    assert p.telegram_user_id == 42
    assert p.aliases == ("Wizard", "wiz")
    assert p.confidence == 1.0


def test_username_and_display_name_history_keeps_changes():
    store = PersonIdentityStore()
    store.observe_telegram_user(42, username="old", display_name="Old Name")
    store.observe_telegram_user(42, username="new", display_name="New Name")
    p = store.get(42)
    assert p is not None
    assert [x.value for x in p.username_history] == ["old", "new"]
    assert [x.value for x in p.display_name_history] == ["Old Name", "New Name"]


def test_repeated_observation_does_not_grow_history():
    store = PersonIdentityStore()
    for _ in range(20):
        store.observe_telegram_user(42, username="same", display_name="Same")
    p = store.get(42)
    assert len(p.username_history) == 1
    assert len(p.display_name_history) == 1


def test_aliases_are_not_identity_keys():
    store = PersonIdentityStore()
    p = store.set_canonical_name(42, "Wizard", authority="self_claim", confidence=.3)
    assert store.get(42) is p
    assert store.get(43) is None


def test_invalid_user_id_rejected():
    try:
        PersonIdentity(0)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid user id accepted")
