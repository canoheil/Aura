from types import SimpleNamespace

from group_context import build_group_context


def user(uid, first, username=None):
    return SimpleNamespace(id=uid, first_name=first, last_name=None, username=username)


def test_speaker_is_separate_from_replied_author():
    speaker = user(100, "Ali", "ali")
    target = user(200, "Wizard", "wizard")
    msg = SimpleNamespace(from_user=speaker, reply_to_message=SimpleNamespace(from_user=target), entities=None, caption_entities=None, text="سلام")
    ctx = build_group_context(msg, is_group=True)
    assert ctx.speaker.user_id == 100
    assert ctx.replied_author.user_id == 200
    assert ctx.speaker.user_id != ctx.replied_author.user_id


def test_text_mention_keeps_verified_user_id():
    speaker = user(100, "Ali")
    mentioned = user(200, "Wizard")
    entity = SimpleNamespace(type="text_mention", offset=0, length=6, user=mentioned)
    msg = SimpleNamespace(from_user=speaker, reply_to_message=None, entities=[entity], caption_entities=None, text="Wizard")
    ctx = build_group_context(msg, is_group=True)
    assert ctx.mentioned_user_ids == (200,)


def test_username_mention_is_not_converted_to_identity():
    speaker = user(100, "Ali")
    entity = SimpleNamespace(type="mention", offset=0, length=7, user=None)
    msg = SimpleNamespace(from_user=speaker, reply_to_message=None, entities=[entity], caption_entities=None, text="@wizard")
    ctx = build_group_context(msg, is_group=True)
    assert ctx.mentioned_usernames == ("wizard",)
    assert ctx.mentioned_user_ids == ()


def test_prompt_labels_roles_explicitly():
    speaker = user(100, "Ali")
    target = user(200, "Wizard")
    msg = SimpleNamespace(from_user=speaker, reply_to_message=SimpleNamespace(from_user=target), entities=None, caption_entities=None, text="x")
    block = build_group_context(msg, is_group=True).prompt_block()
    assert "Current speaker" in block
    assert "Replied message author" in block
    assert "100" in block and "200" in block
