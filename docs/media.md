# Media

Aura can read the media people actually send, and can produce three different
kinds of picture — each with its own cost, quality and privacy profile.

## What comes in

| Telegram message | How it reaches the model |
| --- | --- |
| Photo | Inline image bytes (JPEG), up to 8 MiB |
| Image document (JPEG, PNG, WebP, HEIC/HEIF) | Inline, same path as a photo |
| Sticker (static, animated, video) | The still image a sticker is made of — see [Stickers](#stickers) |
| Voice, audio, video, video note, animation | Inline media, when the selected model accepts the MIME type |
| Any other document | The `file_analyze` tool extracts text on demand |
| Public YouTube link | Passed to the model as a `video_url` part; the provider fetches it (native Gemini only) |

Inline audio and video are limited to the smaller of 12 MiB and
`FILE_MAX_SIZE_MB`; Telegram's own download limit applies on top of that.
Documents go through `FILE_MAX_SIZE_MB` (default 15) and
`FILE_MAX_EXTRACTED_CHARS` (default 60 000) before anything is handed to the
model.

Replying to a message with a photo, video, audio, animation or sticker analyses
*that* message, with the reply text as the question.

### Recent image context

After a photo, Aura keeps only its Telegram `file_id` — never the bytes —
scoped to the same chat and user, for `RECENT_IMAGE_CONTEXT_TTL` seconds
(default 600). A recognizable short follow-up («این چیه؟», «بزرگ‌ترش کن») can
therefore reuse the image without another reply. `/new` or a bot restart clears
it. Follow-up recognition is deliberately conservative and will not cover every
phrasing; when in doubt, reply to the photo.

### When the model misbehaves

If Gemini emits an unexpected textual tool declaration instead of an answer,
Aura retries once with the same media, evidence and tool restrictions. If it
remains invalid, Aura returns an error rather than executing or storing the raw
output. The retry costs one extra model request only when this problem occurs. A
declaration explicitly supplied by the user can still be quoted in a technical
discussion.

## Stickers

### Sending

Smart sticker sending maps English tags from `stickers_config.json` (committed,
no secrets) onto a Telegram sticker pack fetched with `getStickerSet` at
startup. Sticker `file_id`s are cached in `bot.db` (`sticker_cache`) only as a
fallback and are never exposed to the model. The model chooses a tag; the
application decides whether the cooldown allows sending it. If the pack cannot
be loaded, the feature disables itself and normal text replies continue.

`STICKER_CONFIG_PATH` points at a different pack file, `STICKER_SET_NAME`
overrides the set name, and the cooldowns are `STICKER_COOLDOWN_SECONDS` (60 in
private chats) and `STICKER_GROUP_COOLDOWN_SECONDS` (120 in groups).

### Reading

Reading goes the other way, and only one of Telegram's three sticker formats is
an image a model can read directly:

| Format | What is sent to the model |
| --- | --- |
| Static (WebP) | The sticker itself, full resolution |
| Animated (`.tgs`, gzipped Lottie) | Telegram's still thumbnail |
| Video (`.webm`, VP9 with alpha) | Telegram's still thumbnail |

There is no Lottie renderer in the dependency list, and adding one — plus a
glyph exporter for the shapes — would be a large amount of new surface for a
feature whose whole point is "what does this sticker express". Telegram ships a
representative still, so that is what the model gets, and the prompt states
plainly that it is looking at **one frame** and must never claim to have watched
the animation.

The sender's emoji and the pack name travel with the request as text. The MIME
type is sniffed from the downloaded bytes rather than assumed, because Telegram
serves thumbnails as WebP *or* JPG; bytes that are not an image are refused with
a localized message instead of being sent. A wordless sticker is stored as
`[sticker]`, and in groups a sticker is not an invitation: Aura still only looks
at it when she was addressed.

## Images

Photo generation is optional and stays completely disabled until
`POLLINATIONS_API_KEY` is set. Aura then gains a `generate_image` tool, so
natural requests such as «عکس بساز از یک گربه فضانورد» work without a command. If
the request is vague («می‌خوام برام یه عکس بسازی»), Aura asks what the image
should show instead of generating something arbitrary.

```text
/image یک فضاپیمای قدیمی در مه
```

To edit an image, send a photo with an edit caption such as «آسمان را بنفش کن» or
«این را کارتونی کن», or reply to a photo with `/image <change>`. Aura uploads
the source image to Pollinations media storage, passes the public URL through
the image-to-image `image=` parameter, and sends the result back. Ambiguous
captions («این عکس چیه؟») continue to use vision unchanged.

Pollinations stores uploads as unlisted public URLs for up to 30 days.
`POLLINATIONS_UPLOAD_PRIVACY` (default `true`) strips EXIF/GPS metadata before
the file leaves the bot.

The defaults are `flux` for generation and `kontext` for editing, with automatic
fallbacks to `seedream` and `nanobanana`. Model names, timeouts, dimensions and
prompt limits are all configurable — see `.env.example`. No extra Python package
is needed; the client uses `requests`.

## Vector images (SVG)

A model can already write SVG markup, so Aura can turn that into a real picture
without any image service. When `resvg-py` is importable, Aura registers a
`create_svg_image` tool: the model writes the SVG, `svg_image.py` validates it
and rasterises it to PNG locally, and the finished image is delivered exactly
like a generated photo.

This path is free, offline, unlimited, and independent of any image-generation
quota. Aura's prompt tells her to prefer it for logos, icons, charts, diagrams,
infographics, UI mockups, posters, badges, flat illustrations, and anything that
needs exact Persian or English text — and to offer it proactively when
`generate_image` hits a quota, credit, rate-limit or availability error.
Photorealistic requests stay with the photo models.

Rendering rules enforced by `svg_image.py`:

- The markup is cleaned first: code fences and XML/DOCTYPE prologs are stripped,
  `<script>` and `<foreignObject>` elements are removed, inline `font-family`
  declarations are dropped, and any external reference (`http(s)` links,
  `@import`, `<image href>` to a remote host) is rejected.
- `width`/`height` (px, pt, pc, mm, cm, in) or `viewBox` must define a canvas.
  Small drawings are scaled up to a readable PNG; large ones are capped by
  `SVG_IMAGE_MAX_SIDE` (default 1536).
- Persian text works out of the box: `resvg` performs Arabic shaping and the bidi
  algorithm itself, so text stays logical and is never pre-reversed. The bundled
  Vazirmatn fonts in `assets/fonts/` (OFL licensed) are registered with the
  renderer and applied through a stylesheet, because a bare server usually has
  no Persian font installed.
- Not supported: `<foreignObject>`, animation, external images and fonts, and
  some advanced SVG filters.

If `resvg-py` is missing, the tool is not registered, Aura never mentions the
capability, and everything else keeps working. `SVG_IMAGE_ENABLED=false` turns it
off explicitly; `SVG_IMAGE_MAX_OUTPUT_BYTES` and `SVG_IMAGE_BACKGROUND` bound the
result.

## Memes

`meme_api.py` registers `fetch_meme`, which picks a real Reddit post from
meme-api.com (with a mirror fallback), downloads the image bytes with a hard size
cap, refuses NSFW/spoiler posts and non-images, skips memes already sent
recently, and maps topic words to sensible subreddits («پرنده» → `birdmemes`).

The fetched bytes are delivered after the text reply and shown to the model in
the same agent turn, so Aura comments on the actual picture. The tool and its
guidance are only advertised on turns that are actually about memes — a request,
a question about one, or a short «یکی دیگه» follow-up — so an ordinary chat
message never carries the extra tool declaration, the extra model round trip, or
a Reddit download. If Telegram rejects both the image and its document fallback,
Aura notifies the user instead of silently claiming delivery.

A fetch is bounded by one total deadline, `MEME_FETCH_BUDGET_SECONDS` (default
20 s, range 5–60), in addition to `MEME_API_TIMEOUT` per request: a dead or
hanging service can slow one meme request, but never for minutes. Inside that
budget an unknown subreddit is not retried, an unreachable or rate-limiting
mirror is abandoned for the fallback, and a post whose image was already sent is
discarded before its bytes are downloaded.

Set `MEME_ALLOW_NSFW=true` to accept NSFW-tagged posts (spoilers stay blocked
either way) and `MEME_ALLOWED_SUBREDDITS` to pin a whitelist.

## Turning features off

| Variable | Effect |
| --- | --- |
| `MEMES_ENABLED=false` | Drops `fetch_meme` and its prompt guidance |
| `WEATHER_ENABLED=false` | Drops `get_weather` and its prompt guidance |
| `SVG_IMAGE_ENABLED=false` | Drops `create_svg_image` |
| `STICKERS_ENABLED=false` | Drops `send_sticker` |
| `POLLINATIONS_ENABLED=false` | Drops image generation and editing |

A disabled feature is never advertised to the model, so Aura does not promise
something she cannot do.
