"""Let Aura actually look at the stickers people send her.

Telegram delivers three sticker formats and only one of them is an image the
model can read directly:

================  ==========================  ==============================
format            what it is                  what we can send
================  ==========================  ==============================
static            one WebP image              the sticker itself
animated (``.tgs``) gzipped Lottie vector     only its still thumbnail
video (``.webm``)  VP9/WebM clip with alpha   only its still thumbnail
================  ==========================  ==============================

There is no Lottie renderer in the dependency list, and adding one (plus a
glyph exporter for the shapes) would be a large amount of new surface for a
feature whose whole point is "what does this sticker express". Telegram ships a
representative still for animated and video stickers, so that thumbnail is what
we send. The model is told, in the prompt, that it is looking at one frame —
otherwise it happily claims to have watched the animation.

The sticker's own emoji and pack name travel with the request as text: the
sender's chosen emoji is often the clearest statement of intent, and it costs
nothing.

MIME types are sniffed from the downloaded bytes rather than assumed: Telegram
thumbnails are documented as WebP *or* JPG, and a wrong ``mime_type`` makes the
model call fail before the model ever sees the picture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

STATIC = "static"
ANIMATED = "animated"
VIDEO = "video"

#: Formats we can only see one frame of, and the prompt must say so.
STILL_ONLY = (ANIMATED, VIDEO)


@dataclass(frozen=True)
class StickerPlan:
    """Which file to download for one sticker, and how to describe it."""

    file_id: str
    kind: str
    file_size: int = 0
    from_thumbnail: bool = False
    emoji: str = ""
    set_name: str = ""
    mime_type: str = "image/webp"

    @property
    def still_only(self) -> bool:
        return self.kind in STILL_ONLY


def _clean(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def plan_sticker(sticker) -> Optional[StickerPlan]:
    """Choose the file that represents ``sticker`` as an image.

    Returns ``None`` when there is nothing readable: no sticker, no file id, or
    an animated/video sticker without a thumbnail. Callers turn that into a
    localized message instead of calling the model with an empty attachment.
    """
    if sticker is None:
        return None

    emoji = _clean(getattr(sticker, "emoji", ""), 16)
    set_name = _clean(getattr(sticker, "set_name", ""), 64)
    is_animated = bool(getattr(sticker, "is_animated", False))
    is_video = bool(getattr(sticker, "is_video", False))

    if not is_animated and not is_video:
        file_id = str(getattr(sticker, "file_id", "") or "")
        if not file_id:
            return None
        # A static sticker is itself a WebP image, so the full resolution is
        # available and no thumbnail is needed.
        return StickerPlan(
            file_id=file_id,
            kind=STATIC,
            file_size=int(getattr(sticker, "file_size", 0) or 0),
            emoji=emoji,
            set_name=set_name,
            mime_type="image/webp",
        )

    thumbnail = getattr(sticker, "thumbnail", None)
    thumb_id = str(getattr(thumbnail, "file_id", "") or "")
    if not thumb_id:
        return None
    return StickerPlan(
        file_id=thumb_id,
        kind=VIDEO if is_video else ANIMATED,
        file_size=int(getattr(thumbnail, "file_size", 0) or 0),
        from_thumbnail=True,
        emoji=emoji,
        set_name=set_name,
        # Unknown until the bytes arrive: Telegram thumbnails are WebP or JPG.
        mime_type="",
    )


def sniff_image_mime(raw: bytes) -> str:
    """Identify an image from its magic bytes; ``""`` when it is not one."""
    if not raw:
        return ""
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:4] in {b"GIF8", b"GIF9"}:
        return "image/gif"
    return ""


def describe_for_model(plan: StickerPlan, user_text: str = "") -> str:
    """Model-facing framing for the attached sticker image.

    Written as an instruction because the model otherwise reads a sticker as a
    photograph and answers "what a nice picture of a cat" instead of reacting to
    what the sender meant.
    """
    lines = [
        "[ATTACHED: a Telegram sticker — an expressive picture the user sent as a reaction, "
        "not a photograph they took]",
        f"- Sticker format: {plan.kind}"
        + (" (only one still frame of the animation is attached)" if plan.still_only else ""),
    ]
    if plan.set_name:
        lines.append(f"- Sticker pack: {plan.set_name}")
    if plan.emoji:
        lines.append(f"- Emoji the sender sent with it: {plan.emoji}")
    lines.append(
        "Respond to it as the reaction it is: name what it expresses in a sentence or two and "
        "answer naturally. Do not describe it like a photo. Never claim to have watched the "
        "animation or list details that a single frame cannot show."
    )
    lines.append(
        "If the sticker contains text, quote it exactly before answering."
    )
    prompt = "\n".join(lines)
    if user_text.strip():
        return f"{prompt}\n\nCURRENT MESSAGE:\n{user_text.strip()}"
    return f"{prompt}\n\nThe user sent this sticker without any words."
