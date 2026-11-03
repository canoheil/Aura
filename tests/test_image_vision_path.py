import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot.py"

def _source():
    return BOT.read_text(encoding="utf-8")

def test_photo_path_uses_raw_bytes_not_data_url():
    source = _source()
    assert "image_bytes, image_mime, caption = await download_telegram_photo" in source
    assert "build_image_messages(history, prompt_text, image_bytes, image_mime)" in source
    assert "image_url, caption = await download_telegram_photo" not in source

def test_build_image_messages_is_gemini_native():
    tree = ast.parse(_source())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_image_messages")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "image_bytes" in names
    assert "mime_type" in names
    assert any(
        isinstance(n, ast.Constant) and n.value == "media_bytes"
        for n in ast.walk(fn)
    )
    assert any(
        isinstance(n, ast.Constant) and n.value == "mime_type"
        for n in ast.walk(fn)
    )

def test_gemini_converter_accepts_media_bytes():
    source = _source()
    assert 'elif kind == "media_bytes":' in source
    assert "types.Part.from_bytes(data=bytes(data), mime_type=mime_type)" in source

def test_reply_media_builder_exists_and_uses_native_media():
    source = _source()
    assert "def build_reply_media_request(" in source
    assert '"type": "media_bytes"' in source
