"""Helpers for Telegram reply-context handling.

Stage 8: textual reply targets only. Media download/vision belongs to Stage 9.
"""
from typing import Optional


def reply_target_display_name(reply) -> str:
    """Return a useful display label for a replied-to sender."""
    sender = getattr(reply, "from_user", None)
    if sender:
        full = " ".join(
            x for x in [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
            if x
        ).strip()
        if full:
            return full
        username = getattr(sender, "username", None)
        if username:
            return f"@{username}"
        return str(getattr(sender, "id", "unknown"))

    sender_chat = getattr(reply, "sender_chat", None)
    if sender_chat:
        title = getattr(sender_chat, "title", None) or getattr(sender_chat, "username", None)
        if title:
            return str(title)

    return "Unknown sender"


def build_reply_context(reply) -> Optional[str]:
    """Build high-priority context for the exact Telegram message being replied to.

    Stage 8 intentionally handles textual replies only. Media/file downloading is
    deferred to the dedicated media stage so this layer stays small and reliable.
    """
    if reply is None:
        return None

    text = (getattr(reply, "text", None) or getattr(reply, "caption", None) or "").strip()
    message_id = getattr(reply, "message_id", None)
    author = reply_target_display_name(reply)

    media_labels = []
    if getattr(reply, "photo", None):
        media_labels.append("image")
    if getattr(reply, "document", None):
        media_labels.append("document")
    if getattr(reply, "video", None):
        media_labels.append("video")
    if getattr(reply, "audio", None):
        media_labels.append("audio")
    if getattr(reply, "voice", None):
        media_labels.append("voice message")
    if getattr(reply, "sticker", None):
        media_labels.append("sticker")
    if getattr(reply, "animation", None):
        media_labels.append("animation")

    lines = [
        "[REPLIED-TO MESSAGE — HIGH PRIORITY CONTEXT]",
        f"Author: {author}",
    ]
    if message_id is not None:
        lines.append(f"Telegram message ID: {message_id}")

    if text:
        lines.append("Content:")
        lines.append(text)
    elif media_labels:
        lines.append("Content: [media attached — detailed media analysis will be handled separately]")
    else:
        lines.append("Content: [message has no textual content]")

    if media_labels:
        lines.append(f"Media type(s): {', '.join(media_labels)}")

    return "\n".join(lines)


def build_request_with_reply_context(
    history: list[dict[str, str]],
    current_user_message: str,
    reply_context: Optional[str],
) -> list[dict]:
    """Compose history + exact reply target + current message in clear order."""
    messages = list(history)
    if reply_context:
        messages.append({"role": "system", "content": reply_context})
    messages.append({"role": "user", "content": current_user_message})
    return messages


def build_reply_media_request(
    history: list[dict[str, str]],
    current_prompt: str,
    media_bytes: bytes,
    mime_type: str,
) -> list[dict]:
    """Build one request containing the replied-to media and the current question."""
    messages = list(history)
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": current_prompt or "محتوای رسانه‌ای که به آن ریپلای کردم را بررسی کن."},
            {"type": "media_bytes", "data": media_bytes, "mime_type": mime_type},
        ],
    })
    return messages
