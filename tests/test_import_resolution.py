import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _imported_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split('.')[0] for alias in node.names)
    return names


def test_bot_imports_all_telegram_formatting_symbols_it_uses():
    source = (ROOT / 'bot.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    imported = _imported_names(tree)
    required = {'render_telegram_html', 'strip_formatting', 'split_telegram_html'}
    assert required <= imported


def test_telegram_formatting_exports_required_runtime_symbols():
    namespace = {}
    exec((ROOT / 'telegram_formatting.py').read_text(encoding='utf-8'), namespace)
    required = {'render_telegram_html', 'strip_formatting', 'split_telegram_html'}
    assert required <= set(namespace)
