"""Safe, evidence-backed presentation of web results in Telegram answers."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def _has_text_evidence(event: dict, *, item_key: str, fields: tuple[str, ...]) -> bool:
    result = event.get("result")
    if not isinstance(result, dict) or result.get("ok") is not True:
        return False
    items = result.get(item_key)
    if not isinstance(items, list):
        return False
    valid_urls = set(search_source_urls([event], limit=10))
    for item in items:
        if not isinstance(item, dict) or str(item.get("url") or "").strip() not in valid_urls:
            continue
        for field in fields:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, list) and any(isinstance(part, str) and part.strip() for part in value):
                return True
    return False


def has_web_evidence(events: list[dict]) -> bool:
    """Require a real search URL plus retrieved text, not merely a link."""
    return any(
        isinstance(event, dict) and event.get("tool") == "web_search"
        and _has_text_evidence(event, item_key="results", fields=("evidence", "description", "snippet", "snippets", "highlights", "content"))
        for event in events or []
    )


def has_any_web_evidence(events: list[dict]) -> bool:
    """Accept a search snippet or successfully opened page with content."""
    return has_web_evidence(events) or any(
        isinstance(event, dict) and event.get("tool") == "web_open"
        and _has_text_evidence(event, item_key="pages", fields=("content", "markdown"))
        for event in events or []
    )


def search_source_urls(events: list[dict], *, limit: int = 5) -> list[str]:
    """Collect only valid URLs actually returned by successful web tools."""
    urls: list[str] = []
    seen: set[str] = set()
    if limit <= 0:
        return urls
    for event in events or []:
        if not isinstance(event, dict) or event.get("tool") not in {"web_search", "web_open"}:
            continue
        result = event.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        items = result.get("results") if event["tool"] == "web_search" else result.get("pages")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str):
                continue
            url = url.strip()
            if not url or len(url) > 1000 or re.search(r"[\s\x00-\x1f<>]", url):
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                    continue
                if parsed.username is not None or parsed.password is not None:
                    continue
                # Accessing .port validates invalid values without opening a URL.
                _ = parsed.port
            except ValueError:
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)
                if len(urls) >= limit:
                    return urls
    return urls


def remove_generated_source_section(answer: str, *, user_text: str = "") -> str:
    """Remove model-written source lists, preserving fences and user quotes.

    Tool events, not model prose, supply retrieved URLs. A source section
    copied from the user's own message is left alone; a later code fence or
    section heading is never swallowed as part of a source list.
    """
    lines = answer.splitlines(keepends=True)
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            if stripped.count("```") % 2:
                in_fence = not in_fence
            continue
        if in_fence or not re.fullmatch(
            r"(?:#{1,6}\s*)?(?:🔎\s*)?(?:\*\*)?(?:web\s+sources|retrieved\s+sources|sources|"
            r"منابع(?:\s+وب|\s+بازیابی[\u200c -]شده)?)(?:\s*[:：])?(?:\*\*)?\s*[:：]?",
            stripped,
            flags=re.IGNORECASE,
        ):
            continue
        end = index + 1
        while end < len(lines):
            next_line = lines[end].strip()
            if next_line.startswith("```") or re.match(r"^#{1,6}\s+", next_line):
                break
            if not next_line:
                # A blank line can end the source section. Only cross it if
                # the next nonblank line is clearly another URL, so later
                # explanation or code-like data is not discarded.
                lookahead = end + 1
                while lookahead < len(lines) and not lines[lookahead].strip():
                    lookahead += 1
                if lookahead == len(lines):
                    end = lookahead
                    break
                if not re.search(r"https?://", lines[lookahead], flags=re.IGNORECASE):
                    break
                end = lookahead
                continue
            end += 1
        section = "".join(lines[index:end]).strip()
        if user_text and section and section in user_text:
            continue
        before = "".join(lines[:index]).rstrip("\n")
        after = "".join(lines[end:]).lstrip("\n")
        return "\n\n".join(part for part in (before, after) if part).strip("\r\n")
    return answer
