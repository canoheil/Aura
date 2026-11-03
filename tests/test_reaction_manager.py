from reaction_manager import ReactionManager


class FakeResponse:
    def __init__(self, text: str):
        self.text = text


class FakeModels:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        return FakeResponse(self.text)


class FakeClient:
    def __init__(self, text: str):
        self.models = FakeModels(text)


def test_high_confidence_reactions():
    manager = ReactionManager(cooldown_seconds=45)
    assert manager.decide(chat_id=1, user_id=1, text="ممنون").reaction == "❤️"
    assert manager.decide(chat_id=1, user_id=2, text="مرسییی").reaction == "❤️"
    assert manager.decide(chat_id=1, user_id=3, text="😂😂😂").reaction == "😂"
    assert manager.decide(chat_id=1, user_id=4, text="این خیلی خفنه").reaction == "🔥"
    assert manager.decide(chat_id=1, user_id=5, text="امروز خیلی ناراحتم").reaction == "❤️"


def test_silence_for_normal_messages_and_questions():
    manager = ReactionManager(cooldown_seconds=45)
    assert manager.decide(chat_id=1, user_id=1, text="باشه").reaction is None
    assert manager.decide(chat_id=1, user_id=2, text="این خیلی خفنه، چطوری ساختیش؟").reaction is None
    assert manager.decide(chat_id=1, user_id=3, text="یه سؤال دارم").reaction is None


def test_cooldown_is_per_user_and_only_recorded_after_success():
    manager = ReactionManager(cooldown_seconds=45)
    assert manager.decide(chat_id=1, user_id=1, text="ممنون").reaction == "❤️"
    assert manager.decide(chat_id=1, user_id=1, text="مرسی").reaction == "❤️"
    manager.record_reaction(1, 1)
    assert manager.decide(chat_id=1, user_id=1, text="ممنون").reaction is None
    assert manager.decide(chat_id=1, user_id=2, text="ممنون").reaction == "❤️"


def test_ai_candidate_gate_is_conservative():
    manager = ReactionManager()
    assert manager.should_consider_ai("خیلی لطف کردی، دمت گرم")
    assert manager.should_consider_ai("😂 خب این یکی خوب بود")
    assert not manager.should_consider_ai("این کد را بررسی کن و مشکلش را پیدا کن")
    assert not manager.should_consider_ai("این خیلی خوبه، چطوری ساختیش؟")


def test_ai_reaction_semantic_classifier():
    manager = ReactionManager(cooldown_seconds=0)
    client = FakeClient('{"reaction":"😂","confidence":0.96}')
    decision = manager.decide_with_gemini(
        client=client,
        model="gemini-3.1-flash-lite",
        text="خخخخ این یکی خیلی خوب بود 😂",
    )
    assert decision.reaction == "😂"
    assert client.models.calls == 1


def test_ai_reaction_low_confidence_is_silence():
    manager = ReactionManager(cooldown_seconds=0)
    client = FakeClient('{"reaction":"🔥","confidence":0.40}')
    decision = manager.decide_with_gemini(
        client=client,
        model="gemini-3.1-flash-lite",
        text="خب بد نبود",
    )
    assert decision.reaction is None
