"""Safe Telegram HTML formatting for Aura responses.

Stage 10 accepts a small Markdown-like subset from Gemini and renders it as
Telegram-supported HTML. Code spans/blocks are protected before emphasis is
parsed so formatting characters inside code remain literal.
"""

from __future__ import annotations

import html
import re

_ALLOWED_TAG_RE = re.compile(r"</?(?:b|strong|i|em|u|s|strike|del|code|pre|blockquote|tg-spoiler|tg-emoji)(?:\s+[^>]*)?>", re.I)
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def _protect(value: str, protected: list[str]) -> str:
    protected.append(value)
    return f"\x00{len(protected) - 1}\x00"


def _restore(value: str, protected: list[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return protected[idx] if 0 <= idx < len(protected) else ""

    # Repeat because a protected item can contain another placeholder.
    previous = None
    while previous != value:
        previous = value
        value = _PLACEHOLDER_RE.sub(repl, value)
    return value


def _protect_code(text: str, protected: list[str]) -> str:
    # Scan left-to-right so Markdown fences containing HTML and HTML code
    # containing backticks are each protected as a single literal unit.
    pattern = re.compile(
        r"```(?P<language>[A-Za-z0-9_+.-]+)?\r?\n?(?P<fenced>.*?)```"
        r"|<pre\s*>(?P<pre>.*?)</pre\s*>"
        r'|<code(?:\s+class="language-(?P<html_language>[A-Za-z0-9_+.-]+)")?\s*>(?P<html_code>.*?)</code\s*>'
        r"|(?<!`)`(?P<inline>[^`\n]+)`(?!`)",
        re.IGNORECASE | re.DOTALL,
    )

    def protect(match: re.Match[str]) -> str:
        if match.group("fenced") is not None:
            language = match.group("language") or ""
            code_text = match.group("fenced")
            # Remove only the fence-delimiter newline, not intentional blanks.
            if code_text.endswith("\r\n"):
                code_text = code_text[:-2]
            elif code_text.endswith("\n"):
                code_text = code_text[:-1]
            code = html.escape(code_text, quote=False)
            if language:
                return _protect(f'<pre><code class="language-{html.escape(language, quote=True)}">{code}</code></pre>', protected)
            return _protect(f"<pre>{code}</pre>", protected)

        if match.group("pre") is not None:
            body = match.group("pre")
            nested = re.fullmatch(
                r'<code(?:\s+class="language-([A-Za-z0-9_+.-]+)")?\s*>(.*?)</code\s*>',
                body, flags=re.IGNORECASE | re.DOTALL,
            )
            if nested:
                language = nested.group(1)
                escaped = html.escape(html.unescape(nested.group(2)), quote=False)
                opening = f'<code class="language-{language}">' if language else "<code>"
                return _protect(f"<pre>{opening}{escaped}</code></pre>", protected)
            return _protect(f"<pre>{html.escape(html.unescape(body), quote=False)}</pre>", protected)

        if match.group("html_code") is not None:
            code = html.escape(html.unescape(match.group("html_code")), quote=False)
            language = match.group("html_language")
            opening = f'<code class="language-{language}">' if language else "<code>"
            return _protect(f"{opening}{code}</code>", protected)

        return _protect(f"<code>{html.escape(match.group('inline'), quote=False)}</code>", protected)

    text = pattern.sub(protect, text)
    # Four-space/tab-indented lines are copy-sensitive Markdown code, even if
    # the model omitted fences. Keep their indentation and punctuation literal.
    indented = re.compile(
        r"(?m)(?:^(?:async\s+def|def|class|function)\s+[^\n\x00]+\n)?"
        r"(?:^(?: {4}|\t)[^\n\x00]*(?:\n|$))+"
    )
    return indented.sub(lambda match: _protect(html.escape(match.group(0), quote=False), protected), text)


def _sanitize_telegram_tag(raw: str) -> str | None:
    """Allow only Telegram-safe formatting/entity tags and safe attributes."""
    m = re.fullmatch(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9-]*)(?:\s+([^>]*?))?\s*(/?)>", raw)
    if not m:
        return None
    closing, name, attrs, self_closing = m.groups()
    name = name.lower()
    aliases = {"strong": "b", "em": "i", "strike": "s", "del": "s"}
    name = aliases.get(name, name)
    allowed = {"b", "i", "u", "s", "code", "pre", "blockquote", "tg-spoiler", "tg-emoji"}
    if name not in allowed or self_closing:
        return None
    if closing:
        return f"</{name}>"
    if not attrs:
        return f"<{name}>"
    if name == "tg-emoji":
        am = re.fullmatch(r'emoji-id\s*=\s*[\"\']([0-9]+)[\"\']', attrs, re.I)
        if am:
            return f'<tg-emoji emoji-id="{am.group(1)}">'
        return None
    if name == "code":
        am = re.fullmatch(r'class\s*=\s*[\"]language-([A-Za-z0-9_+.-]+)[\"]', attrs, re.I)
        if am:
            return f'<code class="language-{am.group(1)}">'
        return None
    return None


def _protect_existing_html(text: str, protected: list[str]) -> str:
    """Preserve only Telegram-safe tags; unsafe tags/attributes become text."""
    # Normalize common aliases to Telegram's compact tag set.
    text = re.sub(r"<\s*(strong)\s*>", "<b>", text, flags=re.I)
    text = re.sub(r"<\s*/\s*(strong)\s*>", "</b>", text, flags=re.I)
    text = re.sub(r"<\s*(em)\s*>", "<i>", text, flags=re.I)
    text = re.sub(r"<\s*/\s*(em)\s*>", "</i>", text, flags=re.I)
    text = re.sub(r"<\s*(strike|del)\s*>", "<s>", text, flags=re.I)
    text = re.sub(r"<\s*/\s*(strike|del)\s*>", "</s>", text, flags=re.I)

    tag_re = re.compile(r"</?[A-Za-z][^>]*>")

    def tag(match: re.Match[str]) -> str:
        raw = match.group(0)
        safe = _sanitize_telegram_tag(raw)
        return _protect(safe, protected) if safe else raw

    return tag_re.sub(tag, text)


def _apply_entities(text: str) -> str:
    """Render the Stage 12 entity subset before emphasis parsing."""
    # Markdown links. Only explicit http(s) and tg://user?id= links are emitted.
    link_re = re.compile(r"\[([^]\n]+)\]\(([^)\s]+)\)")

    def link(match: re.Match[str]) -> str:
        label = match.group(1)
        target = html.unescape(match.group(2))
        if re.fullmatch(r"https?://[^\s<>]+", target, re.I) or re.fullmatch(r"tg://user\?id=[0-9]+", target, re.I):
            return f'<a href="{html.escape(target, quote=True)}">{label}</a>'
        return match.group(0)

    text = link_re.sub(link, text)

    # Double pipes are Aura's spoiler syntax. Do not consume empty spoilers.
    text = re.sub(r"\|\|([^|\n]+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)
    return text


def _apply_markdown(text: str) -> str:
    text = _apply_entities(text)
    # Strong emphasis first, then single emphasis. These intentionally require
    # non-whitespace boundaries to avoid mangling ordinary underscores.
    text = re.sub(r"(?<!\w)\*\*([^*\n]+?)\*\*(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__([^_\n]+?)__(?!\w)", r"<u>\1</u>", text)
    text = re.sub(r"(?<!\w)~~([^~\n]+?)~~(?!\w)", r"<s>\1</s>", text)
    text = re.sub(r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)

    # Telegram quote blocks: consecutive lines beginning with > become one
    # blockquote. Formatting inside the quote has already been applied.
    lines = text.split("\n")
    out: list[str] = []
    quote: list[str] = []
    for line in lines:
        if re.match(r"^\s*&gt;\s?", line):
            quote.append(re.sub(r"^\s*&gt;\s?", "", line))
        elif re.match(r"^\s*>\s?", line):
            quote.append(re.sub(r"^\s*>\s?", "", line))
        else:
            if quote:
                out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
                quote = []
            out.append(line)
    if quote:
        out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
    return "\n".join(out)


def render_telegram_html(text: str) -> str:
    """Render Aura's supported formatting subset into safe Telegram HTML."""
    text = str(text or "")
    if not text.strip():
        return ""

    protected: list[str] = []
    text = _protect_code(text, protected)
    text = _protect_existing_html(text, protected)
    text = html.escape(text, quote=False)
    text = _apply_markdown(text)
    return _restore(text, protected)


def strip_formatting(text: str) -> str:
    """Return plain text after a Telegram HTML failure without corrupting code."""
    text = str(text or "")
    protected: list[str] = []

    def html_code(match: re.Match[str]) -> str:
        value = re.sub(r"</?(?:pre|code)(?:\s+[^>]*)?>", "", match.group(0), flags=re.IGNORECASE)
        return _protect(html.unescape(value), protected)

    # send_long_message passes rendered HTML here; protect its code bodies
    # before stripping emphasis markers such as __ or ** from the surrounding text.
    text = re.sub(r"<pre(?:\s+[^>]*)?>.*?</pre>|<code(?:\s+[^>]*)?>.*?</code>", html_code, text, flags=re.I | re.S)

    def fenced_code(match: re.Match[str]) -> str:
        code = match.group(1)
        if code.endswith("\r\n"):
            code = code[:-2]
        elif code.endswith("\n"):
            code = code[:-1]
        return _protect(code, protected)

    text = re.sub(r"```(?:[A-Za-z0-9_+.-]+)?\r?\n?(.*?)```", fenced_code, text, flags=re.S)
    text = re.sub(r"(?<!`)`([^`\n]+)`(?!`)", lambda match: _protect(match.group(1), protected), text)
    text = re.sub(
        r"(?m)(?:^(?:async\s+def|def|class|function)\s+[^\n\x00]+\n)?"
        r"(?:^(?: {4}|\t)[^\n\x00]*(?:\n|$))+",
        lambda match: _protect(match.group(0), protected), text,
    )
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = re.sub(r"(?m)^[ \t]*>[ \t]?", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return _restore(html.unescape(text), protected)


def split_telegram_html(text: str, limit: int = 4000) -> list[str]:
    """Split formatted HTML while preserving exact open tags across chunks."""
    if len(text) <= limit:
        return [text]

    tag_re = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)(?:\s+[^>]*)?>")
    allowed = {"b", "i", "u", "s", "code", "pre", "blockquote", "tg-spoiler", "tg-emoji"}
    tokens = re.findall(r"<[^>]+>|[^<]+", text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    stack: list[tuple[str, str]] = []

    def closing_len() -> int:
        return sum(len(f"</{name}>") for name, _ in reversed(stack))

    def opening_len() -> int:
        return sum(len(opening) for _, opening in stack)

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        chunks.append("".join(current) + "".join(f"</{name}>" for name, _ in reversed(stack)))
        current = [opening for _, opening in stack]
        current_len = opening_len()

    for token in tokens:
        match = tag_re.fullmatch(token)
        if match and match.group(2).lower() in allowed:
            closing, name = match.group(1), match.group(2).lower()
            if closing:
                if current and current_len + len(token) > limit:
                    flush()
                current.append(token)
                current_len += len(token)
                if stack and stack[-1][0] == name:
                    stack.pop()
                continue

            # Preserve the exact opening tag (including entity attributes).
            if current and current_len + len(token) + closing_len() > limit:
                flush()
            current.append(token)
            current_len += len(token)
            stack.append((name, token))
            continue

        while token:
            available = limit - current_len - closing_len()
            if available <= 0:
                flush()
                available = limit - current_len - closing_len()
            take = min(len(token), available)
            current.append(token[:take])
            current_len += take
            token = token[take:]
            if token:
                flush()

    if current:
        chunks.append("".join(current) + "".join(f"</{name}>" for name, _ in reversed(stack)))
    return chunks
