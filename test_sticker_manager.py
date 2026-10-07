"""Offline tests for the smart sticker policy layer (Stage 21).

Never imports bot.py: integration points are verified with AST/source checks,
mirroring test_import_resolution.py. No pytest fixtures are used so the suite
also runs under a plain function runner.
"""
import ast
import json
import os
import tempfile
from pathlib import Path

from sticker_manager import StickerManager, sticker_manager


ROOT = Path(__file__).resolve().parent

VALID_STICKERS = [
    {"position": 1, "tag": "angry_cute", "description": "Cute stubborn anger"},
    {"position": 2, "tag": "eating_tomato", "description": "Playful grudge"},
    {"position": 3, "tag": "sly", "description": "Sly, scheming look"},
    {"position": 4, "tag": "sad_cry", "description": "Sad and crying"},
    {"position": 5, "tag": "love", "description": "Thanks, affection"},
    {"position": 6, "tag": "like_wink", "description": "Approval"},
    {"position": 7, "tag": "excited", "description": "Excited and thrilled"},
    {"position": 8, "tag": "poker_face", "description": "Deadpan reaction"},
]

PACK_ITEMS = [(f"uid-{position}", f"fid-{position}") for position in range(1, 9)]


def _write_config(stickers=None, set_name="test_pack_by_bot"):
    payload = {"set_name": set_name, "stickers": VALID_STICKERS if stickers is None else stickers}
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(payload, handle)
    finally:
        handle.close()
    return handle.name


def _manager(config_path=None, stickers=None, **kwargs):
    kwargs.setdefault("cooldown_seconds", 60)
    kwargs.setdefault("group_cooldown_seconds", 120)
    path = config_path or _write_config(stickers=stickers)
    return StickerManager(config_path=path, **kwargs)


# ---------- config loading / validation ----------

def test_valid_config_loads():
    manager = _manager()
    assert manager.is_configured()
    assert not manager.is_ready()
    assert manager.set_name == "test_pack_by_bot"
    assert manager.config_error == ""
    assert manager.tags() == (
        "angry_cute", "eating_tomato", "sly", "sad_cry",
        "love", "like_wink", "excited", "poker_face",
    )


def test_missing_config_file_disables_feature_without_crash():
    manager = StickerManager(config_path=os.path.join(tempfile.gettempdir(), "aura_missing_stickers_config.json"))
    assert not manager.is_configured()
    assert not manager.is_ready()
    assert manager.tags() == ()
    assert manager.describe_for_prompt() == ""
    assert manager.config_error


def test_duplicate_tags_rejected():
    stickers = [dict(entry) for entry in VALID_STICKERS]
    stickers[1]["tag"] = "angry_cute"
    manager = _manager(stickers=stickers)
    assert not manager.is_configured()
    assert "Duplicate" in manager.config_error


def test_duplicate_positions_rejected():
    stickers = [dict(entry) for entry in VALID_STICKERS]
    stickers[1]["position"] = 1
    manager = _manager(stickers=stickers)
    assert not manager.is_configured()


def test_non_contiguous_positions_rejected():
    stickers = [dict(entry) for entry in VALID_STICKERS]
    stickers[2]["position"] = 9
    manager = _manager(stickers=stickers)
    assert not manager.is_configured()
    assert "contiguous" in manager.config_error


def test_missing_fields_rejected():
    stickers = [dict(entry) for entry in VALID_STICKERS]
    del stickers[0]["description"]
    manager = _manager(stickers=stickers)
    assert not manager.is_configured()

    stickers = [dict(entry) for entry in VALID_STICKERS]
    stickers[3]["position"] = "4"  # position must be an integer, not a string
    manager = _manager(stickers=stickers)
    assert not manager.is_configured()

    manager = _manager(stickers=[])
    assert not manager.is_configured()


# ---------- registry join ----------

def test_registry_joins_by_position():
    manager = _manager()
    assert manager.set_registry(PACK_ITEMS)
    assert manager.is_ready()
    assert manager.registry_error == ""
    entry = manager.get("sly")
    assert entry is not None
    assert entry.position == 3
    assert entry.file_id == "fid-3"
    assert entry.file_unique_id == "uid-3"
    assert entry.description == "Sly, scheming look"
    positioned = manager.entry_at_position(5)
    assert positioned is not None and positioned.tag == "love"


def test_registry_count_mismatch_disables_without_crash():
    manager = _manager()
    assert not manager.set_registry(PACK_ITEMS[:7])
    assert manager.is_configured()
    assert not manager.is_ready()
    assert manager.get("sly") is None
    assert "mismatch" in manager.registry_error.lower()
    # A later correct payload re-enables the feature.
    assert manager.set_registry(PACK_ITEMS)
    assert manager.is_ready()


def test_registry_rejects_empty_or_duplicate_identifiers():
    manager = _manager()
    broken = list(PACK_ITEMS)
    broken[1] = broken[0]
    assert not manager.set_registry(broken)
    assert not manager.is_ready()

    manager = _manager()
    broken = list(PACK_ITEMS)
    broken[2] = ("", "fid-3")
    assert not manager.set_registry(broken)
    assert not manager.is_ready()


def test_registry_requires_config():
    manager = StickerManager(config_path=os.path.join(tempfile.gettempdir(), "aura_missing_stickers_config.json"))
    assert not manager.set_registry(PACK_ITEMS)
    assert not manager.is_ready()


# ---------- prompt surface ----------

def test_prompt_description_lists_tags_without_file_ids():
    manager = _manager()
    manager.set_registry(PACK_ITEMS)
    prompt = manager.describe_for_prompt()
    for tag in manager.tags():
        assert f"{tag}:" in prompt
    assert "Sly, scheming look" in prompt
    # file_ids are private: they must never leak into model-facing text.
    assert "fid-" not in prompt
    assert "uid-" not in prompt


# ---------- cooldown / reservation ----------

def test_check_and_reserve_denied_when_not_ready():
    manager = _manager()
    allowed, reason = manager.check_and_reserve(1, 10, is_group=False, explicit=False, now=1000.0)
    assert not allowed
    assert reason


def test_private_cooldown():
    manager = _manager(cooldown_seconds=60)
    manager.set_registry(PACK_ITEMS)
    assert manager.check_and_reserve(1, 10, is_group=False, explicit=False, now=1000.0) == (True, "")
    allowed, reason = manager.check_and_reserve(1, 10, is_group=False, explicit=False, now=1030.0)
    assert not allowed and reason
    assert manager.check_and_reserve(1, 10, is_group=False, explicit=False, now=1060.0)[0]


def test_group_cooldown_is_longer():
    manager = _manager(cooldown_seconds=60, group_cooldown_seconds=120)
    manager.set_registry(PACK_ITEMS)
    assert manager.check_and_reserve(-100, 20, is_group=True, explicit=False, now=2000.0)[0]
    # 100s later a private chat would be allowed again, a group must not be.
    assert manager.check_and_reserve(-100, 20, is_group=True, explicit=False, now=2100.0)[0] is False
    assert manager.check_and_reserve(-100, 20, is_group=True, explicit=False, now=2120.0)[0]


def test_explicit_bypasses_cooldown_but_keeps_hard_minimum():
    manager = _manager(cooldown_seconds=60)
    manager.set_registry(PACK_ITEMS)
    assert manager.check_and_reserve(1, 10, is_group=False, explicit=True, now=1000.0)[0]
    # Normal request inside the cooldown is denied...
    assert manager.check_and_reserve(1, 10, is_group=False, explicit=False, now=1001.0)[0] is False
    # ...an explicit request bypasses the cooldown but not the 3s flood guard.
    assert manager.check_and_reserve(1, 10, is_group=False, explicit=True, now=1002.0)[0] is False
    assert manager.check_and_reserve(1, 10, is_group=False, explicit=True, now=1003.5)[0]


def test_cooldown_is_per_chat_user_pair():
    manager = _manager(cooldown_seconds=60)
    manager.set_registry(PACK_ITEMS)
    assert manager.check_and_reserve(1, 10, is_group=False, explicit=False, now=1000.0)[0]
    assert manager.check_and_reserve(1, 11, is_group=False, explicit=False, now=1000.5)[0]
    assert manager.check_and_reserve(2, 10, is_group=False, explicit=False, now=1001.0)[0]
    assert manager.check_and_reserve(1, 10, is_group=False, explicit=False, now=1001.5)[0] is False


def test_state_eviction_at_cap():
    manager = _manager(cooldown_seconds=60, max_state_entries=128)
    manager.set_registry(PACK_ITEMS)
    assert manager.check_and_reserve(0, 1, is_group=False, explicit=False, now=1000.0)[0]
    # Fill past the cap so the oldest key (0, 1) is evicted.
    for index in range(2, 300):
        assert manager.check_and_reserve(0, index, is_group=False, explicit=False, now=1000.0 + index * 0.001)[0]
    # The evicted key is no longer cooling down, while a recent key still is.
    assert manager.check_and_reserve(0, 1, is_group=False, explicit=False, now=1010.0)[0]
    assert manager.check_and_reserve(0, 299, is_group=False, explicit=False, now=1010.0)[0] is False


# ---------- invalidate ----------

def test_invalidate_drops_broken_entry():
    manager = _manager()
    manager.set_registry(PACK_ITEMS)
    manager.invalidate("uid-3")
    assert manager.get("sly") is None
    assert manager.get("love") is not None
    assert manager.is_ready()
    # Invalidating everything disables sending until the next registry load.
    for unique_id, _file_id in PACK_ITEMS:
        manager.invalidate(unique_id)
    assert not manager.is_ready()
    manager.invalidate("")  # no-op, must not raise
    manager.invalidate("unknown-uid")  # no-op, must not raise


# ---------- repo config + bot.py integration (source-level only) ----------

def test_repo_config_loads_with_real_pack():
    # The committed stickers_config.json must be valid for the exported singleton.
    assert sticker_manager.is_configured()
    assert sticker_manager.set_name == "sYMA1791362612_by_stickwebot"
    assert sticker_manager.tags()[0] == "angry_cute"
    assert len(sticker_manager.tags()) == 8
    assert not sticker_manager.is_ready()  # registry needs Telegram at runtime


def _bot_source():
    return (ROOT / "bot.py").read_text(encoding="utf-8")


def test_bot_registers_send_sticker_tool():
    tree = ast.parse(_bot_source())
    registered = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "register":
            for keyword in node.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    registered.append(keyword.value.value)
    assert "send_sticker" in registered


def test_bot_sends_stickers_via_reply_sticker():
    tree = ast.parse(_bot_source())
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "reply_sticker" in attributes
    assert "get_sticker_set" in attributes


def test_bot_imports_sticker_manager_singleton():
    tree = ast.parse(_bot_source())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)
    assert "sticker_manager" in imported


def test_bot_declares_sticker_cache_table():
    assert "CREATE TABLE IF NOT EXISTS sticker_cache" in _bot_source()


def test_bot_sets_httpx_and_httpcore_loggers_to_warning():
    """bot.py must quiet httpx/httpcore so Telegram URLs (and the bot token) stay out of INFO logs."""
    source = _bot_source()
    tree = ast.parse(source)
    quieted = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        if not isinstance(node.iter, (ast.Tuple, ast.List)):
            continue
        names = [
            elt.value
            for elt in node.iter.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
        if "httpx" not in names or "httpcore" not in names:
            continue
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Call):
                continue
            if not (isinstance(stmt.func, ast.Attribute) and stmt.func.attr == "setLevel"):
                continue
            if not stmt.args:
                continue
            level = stmt.args[0]
            is_warning = (
                isinstance(level, ast.Attribute) and level.attr == "WARNING"
            ) or (
                isinstance(level, ast.Constant) and level.value in (30, "WARNING")
            )
            if is_warning:
                quieted.update(names)
    assert {"httpx", "httpcore"} <= quieted


def test_bot_sticker_guidance_drops_one_in_five_quota():
    source = _bot_source()
    lowered = source.lower()
    assert "one reply in five" not in lowered
    assert "one in five" not in lowered
    start = source.index("def _stickers_prompt_section")
    rest = source[start:]
    end = rest.find("\ndef ", 1)
    section = rest if end < 0 else rest[:end]
    assert "user_requested=true" in section
