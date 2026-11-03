from identity_authority import (
    AuthorityLevel,
    ClaimDecision,
    IdentityAuthority,
    IdentityClaim,
)


def test_verified_identity_cannot_be_overwritten_by_self_claim():
    authority = IdentityAuthority()
    authority.register_verified(100, "Wizard")

    claim = IdentityClaim(
        speaker_user_id=100,
        target_user_id=100,
        claimed_name="Pancake",
        authority=AuthorityLevel.SELF_CLAIM,
    )
    result = authority.evaluate_claim(claim)

    assert result.decision is ClaimDecision.REJECT
    assert authority.get(100).canonical_name == "Wizard"


def test_self_claim_is_provisional_and_does_not_mutate_identity():
    authority = IdentityAuthority()
    claim = IdentityClaim(
        speaker_user_id=100,
        target_user_id=100,
        claimed_name="Wizard",
        authority=AuthorityLevel.SELF_CLAIM,
    )

    result = authority.evaluate_claim(claim)

    assert result.decision is ClaimDecision.ESTABLISH
    assert authority.get(100) is None


def test_user_cannot_rename_another_user_with_ordinary_claim():
    authority = IdentityAuthority()
    authority.register_established(200, "Ben")
    claim = IdentityClaim(
        speaker_user_id=100,
        target_user_id=200,
        claimed_name="Wizard",
        authority=AuthorityLevel.OTHER_USER_CLAIM,
    )

    result = authority.evaluate_claim(claim)

    assert result.decision is ClaimDecision.PENDING
    assert authority.get(200).canonical_name == "Ben"


def test_joke_never_changes_identity():
    authority = IdentityAuthority()
    authority.register_established(100, "Wizard")
    claim = IdentityClaim(
        speaker_user_id=100,
        target_user_id=100,
        claimed_name="Teyfan",
        authority=AuthorityLevel.JOKE_OR_SPECULATION,
        is_joke=True,
    )

    result = authority.evaluate_claim(claim)

    assert result.decision is ClaimDecision.IGNORE
    assert authority.get(100).canonical_name == "Wizard"


def test_authoritative_claim_can_be_applied():
    authority = IdentityAuthority()
    claim = IdentityClaim(
        speaker_user_id=1,
        target_user_id=100,
        claimed_name="Wizard",
        authority=AuthorityLevel.FOUNDER_ADMIN_CONFIRMED,
    )

    record = authority.apply_authoritative_claim(claim)

    assert record.telegram_user_id == 100
    assert record.canonical_name == "Wizard"
    assert record.authority is AuthorityLevel.FOUNDER_ADMIN_CONFIRMED


def test_authoritative_claim_cannot_be_applied_to_conflict_with_verified_identity():
    authority = IdentityAuthority()
    authority.register_verified(100, "Wizard")
    claim = IdentityClaim(
        speaker_user_id=1,
        target_user_id=100,
        claimed_name="Pancake",
        authority=AuthorityLevel.FOUNDER_ADMIN_CONFIRMED,
    )

    try:
        authority.apply_authoritative_claim(claim)
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting authoritative claim should not overwrite verified identity")

    assert authority.get(100).canonical_name == "Wizard"


def test_same_authority_conflict_cannot_silently_overwrite():
    authority = IdentityAuthority()
    authority.register_verified(100, "Wizard")
    claim = IdentityClaim(
        speaker_user_id=1,
        target_user_id=100,
        claimed_name="Pancake",
        authority=AuthorityLevel.VERIFIED,
    )

    result = authority.evaluate_claim(claim)

    assert result.decision is ClaimDecision.REJECT
    assert authority.get(100).canonical_name == "Wizard"
