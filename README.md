# Aura — Production Gemini Build

Aura is a personal Telegram AI assistant powered by Google's Gemini API.

This repository contains the public application code and configuration template. Secrets, the local SQLite database, logs, PID files, and virtual environments are intentionally excluded.

## Features

- Gemini-powered Telegram conversations
- Persistent conversation history in SQLite
- Long-term memory
- Guarded You.com web search
- Group-chat triggers and speaker awareness
- Telegram image handling / vision input
- Image generation from text and image-to-image editing via Pollinations.ai (optional)
- Quota-free vector image creation: Aura writes SVG herself and it is rendered locally to PNG
- Current conditions and 3-day forecast for any city via Open-Meteo (keyless)
- Real Reddit memes fetched with a keyless API, then commented on from the actual picture
- Smart sticker sending (model-chosen tags from a personal sticker pack, with cooldowns)
- Configurable model and search settings
- Environment-based secrets and configuration

## Requirements

- Python 3.11+
- A Telegram Bot Token
- A Google AI Studio / Gemini API key
- Optional: a You.com API key for web search
- Optional: a Pollinations.ai API key for image generation and editing
- Optional: `resvg-py` (already in `requirements.txt`) for local SVG rendering

## Installation

Clone the repository and create a Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the environment template:

```bash
cp .env.example .env
```

Then edit `.env` and set your real credentials.

### Required variables

```env
TELEGRAM_BOT_TOKEN=...
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.1-flash-lite
```

### Optional variables

- `BOT_NAME`
- `FOUNDER_USER_ID`
- `YOU_API_KEY`
- `WEB_SEARCH_ENABLED`
- `WEB_SEARCH_DAILY_LIMIT`
- `WEB_SEARCH_MAX_RESULTS`
- `WEB_SEARCH_CACHE_TTL`
- `WEB_SEARCH_CURRENT_CACHE_TTL`
- `WEB_SEARCH_TIMEOUT`
- `MAX_HISTORY_MESSAGES`
- `REQUEST_TIMEOUT`
- `STICKERS_ENABLED`
- `STICKER_SET_NAME`
- `STICKER_CONFIG_PATH`
- `STICKER_COOLDOWN_SECONDS`
- `STICKER_GROUP_COOLDOWN_SECONDS`
- `STICKER_TOOL_MAX_CALLS`
- `POLLINATIONS_API_KEY`
- `POLLINATIONS_ENABLED`
- `POLLINATIONS_IMAGE_MODEL`
- `POLLINATIONS_IMAGE_FALLBACK_MODEL`
- `POLLINATIONS_EDIT_MODEL`
- `POLLINATIONS_EDIT_FALLBACK_MODEL`
- `POLLINATIONS_TIMEOUT`
- `POLLINATIONS_MAX_PROMPT_CHARS`
- `POLLINATIONS_IMAGE_WIDTH`
- `POLLINATIONS_IMAGE_HEIGHT`
- `POLLINATIONS_TOOL_MAX_CALLS`
- `POLLINATIONS_UPLOAD_PRIVACY`
- `SVG_IMAGE_ENABLED`
- `SVG_IMAGE_TOOL_MAX_CALLS`
- `SVG_IMAGE_MAX_SIDE`
- `SVG_IMAGE_MAX_OUTPUT_BYTES`
- `SVG_IMAGE_BACKGROUND`
- `WEB_SEARCH_ENFORCE`
- `WEB_SEARCH_ENFORCE_SOFT_LIMIT`
- `WEB_SEARCH_FORCE_TERMS`
- `WEATHER_ENABLED`
- `WEATHER_GEOCODE_URL`
- `WEATHER_FORECAST_URL`
- `WEATHER_TIMEOUT`
- `WEATHER_CACHE_TTL`
- `WEATHER_TOOL_MAX_CALLS`
- `MEMES_ENABLED`
- `MEME_API_BASE_URL`
- `MEME_API_FALLBACK_URL`
- `MEME_API_TIMEOUT`
- `MEME_FETCH_BUDGET_SECONDS`
- `MEME_MAX_BYTES`
- `MEME_ALLOW_NSFW`
- `MEME_ALLOWED_SUBREDDITS`
- `MEME_TOOL_MAX_CALLS`

## Stickers

Smart sticker sending maps English tags from `stickers_config.json` (committed, no secrets) onto a Telegram sticker pack fetched with `getStickerSet` at startup. Sticker `file_id`s are cached in `bot.db` (`sticker_cache`) only as a fallback and are never exposed to the model. If the pack cannot be loaded, the feature disables itself and normal text replies continue.

## Images

Image generation is optional and stays completely disabled until
`POLLINATIONS_API_KEY` is set. Aura then gains a `generate_image` tool, so natural
requests such as «عکس بساز از یک گربه فضانورد» work without a command. If the
request is vague («می‌خوام برام یه عکس بسازی»), Aura asks what the image should
show instead of generating something arbitrary.

You can also use:

```text
/image یک فضاپیمای قدیمی در مه
```

To edit an image, send a photo with an edit caption such as «آسمان را بنفش کن»
or «این را کارتونی کن», or reply to a photo with `/image <change>`. Aura uploads
the source image to Pollinations media storage, passes the public URL through the
image-to-image `image=` parameter, and sends the result back. Ambiguous captions
(«این عکس چیه؟») continue to use Gemini vision unchanged. Pollinations stores
uploads as unlisted public URLs for up to 30 days; `POLLINATIONS_UPLOAD_PRIVACY`
(default `true`) strips EXIF/GPS metadata before the file leaves the bot.

The defaults are `flux` for generation and `kontext` for editing, with automatic
fallbacks to `seedream` and `nanobanana`. All model names, timeouts, dimensions,
and prompt limits are configurable in `.env`; see `.env.example`.

No additional Python package is required — the client uses `requests`, which is
already a dependency.


## Vector images (SVG)

Gemini can already write SVG markup, so Aura can turn that into a real picture
without any image service. When `resvg-py` is importable, Aura registers a
`create_svg_image` tool: the model writes the SVG, `svg_image.py` validates it
and rasterises it to PNG locally, and the finished image is delivered exactly
like a generated photo.

This path is free, offline, unlimited, and independent of any image-generation
quota. Aura's system prompt tells her to prefer it for logos, icons, charts,
diagrams, infographics, UI mockups, posters, badges, flat illustrations, and
anything that needs exact Persian/English text — and to offer it proactively
when `generate_image` hits a quota, credit, rate-limit, or availability error.
Photorealistic requests stay with the Pollinations photo models.

Rendering rules enforced by `svg_image.py`:

- The markup is cleaned up first: code fences and XML/DOCTYPE prologs are
  stripped, `<script>` and `<foreignObject>` elements are removed, inline
  `font-family` declarations are dropped, and any external reference
  (`http(s)` links, `@import`, `<image href>` to a remote host) is rejected.
- `width`/`height` (px, pt, pc, mm, cm, in) or `viewBox` must define a canvas.
  Small drawings are scaled up to a readable PNG, large ones are capped by
  `SVG_IMAGE_MAX_SIDE`.
- Persian text works out of the box: `resvg` performs Arabic shaping and the
  bidi algorithm itself, so text stays logical and is never pre-reversed. The
  bundled Vazirmatn fonts in `assets/fonts/` (OFL licensed) are registered with
  the renderer and applied through a stylesheet, because a bare server usually
  has no Persian font installed.
- Not supported by the renderer: `<foreignObject>`, animation, external images
  and fonts, and some advanced SVG filters.

If `resvg-py` is missing, the tool is not registered, Aura never mentions the
capability, and everything else keeps working. Set `SVG_IMAGE_ENABLED=false` to
turn it off explicitly.

## Freshness gate (why Aura searches without being told)

A model cannot notice that its own knowledge is stale: it does not know what day
it is, so "who is the president" and "what is the latest release" both feel
answerable from memory and the answer that comes back is whatever was true when
training ended. `freshness_policy.py` makes that call in the application instead:
it looks at the user's exact wording and returns one of three decisions.

| Decision | Meaning | Example |
| --- | --- | --- |
| `force` | time-dependent question about a named subject | «قیمت دلار چنده»، "latest Python version" |
| `hint` | time-dependent wording, subject unclear | «قیمت چنده»، «آیا هنوز پشتیبانی میشه» |
| `off` | stable knowledge, chit-chat, or another tool owns it | «معنی این کلمه»، «یه میم بده» |

On `force` the first agent round is generated with Gemini's tool mode pinned to
`web_search`, so the model cannot answer from memory at all; later rounds are
normal. On `hint` Aura is told to search unless she is sure. On `off` nothing is
added and the turn costs no quota.

Cost is bounded on purpose:

- a forced search only happens for `force`, which additionally requires a real
  subject, so bare follow-ups never pay;
- the prompt spells out what must not be searched (opinions, code,
  translation, weather, memes) so ordinary chat stays search-free;
- cached answers cost nothing (`WEB_SEARCH_CACHE_TTL`), duplicate identical
  calls inside a turn are merged by the agent runner, and `WEB_SEARCH_DAILY_LIMIT`
  remains a hard stop;
- `WEB_SEARCH_ENFORCE_SOFT_LIMIT` stops forcing for the rest of the day once that
  many searches were spent (0 = 80% of the daily limit), leaving the remaining
  quota for questions that genuinely need it;
- `WEB_SEARCH_ENFORCE=false` keeps the prompt guidance and turns the forcing off.

`WEB_SEARCH_FORCE_TERMS` adds comma-separated words of your own (a game name, a
ticker, a channel-specific product) to the force vocabulary.

## Weather and memes

Both features are keyless, need no extra dependencies, and run fine on shared
hosting: they are plain HTTPS calls with `requests`.

`weather_api.py` registers a `get_weather` tool. Aura passes the city exactly as
the user wrote it (Persian or English); the module resolves it with the
Open-Meteo geocoding API, fetches current conditions plus a 3-day forecast, maps
WMO weather codes to Persian descriptions, and caches a city for
`WEATHER_CACHE_TTL` seconds so repeated questions cannot hammer the free
service. Only a compact summary reaches the model — never the raw payload. A
local hint recognises explicit questions («هوای تهران چطوره؟»,
«آب و هوا مشهد»، "weather in Berlin") and makes sure the tool is actually called,
while talk-about sentences ("درباره هواشناسی چیکار کنم", "چند درجه سرد شده") and
image/meme requests stay out of it.

`meme_api.py` registers `fetch_meme`, which picks a real Reddit post from
meme-api.com (with a mirror fallback), downloads the image bytes with a hard
size cap, refuses NSFW/spoiler posts and non-images, skips memes already sent
recently, and maps topic words to sensible subreddits
("پرنده" → `birdmemes`). The fetched bytes are delivered after the text reply and
shown to Gemini in the same agent turn, so Aura comments on the actual picture.
The tool and its guidance are only advertised on turns that are actually about
memes (a request, a question about one, or a short «یکی دیگه» follow-up), so an
ordinary chat message never carries the extra tool declaration, the extra model
round trip, or a Reddit download.

A fetch is bounded by one total deadline, `MEME_FETCH_BUDGET_SECONDS` (default
20s, 5-60), in addition to `MEME_API_TIMEOUT` per request: a dead or hanging
service can slow one meme request, but never for minutes. Inside that budget an
unknown subreddit is not retried, an unreachable or rate-limiting mirror is
abandoned for the fallback, and a post whose image was already sent is discarded
before its bytes are downloaded.
Set `MEME_ALLOW_NSFW=true` to accept NSFW-tagged posts (spoilers stay blocked
either way), and `MEME_ALLOWED_SUBREDDITS` to pin a whitelist.

Set `WEATHER_ENABLED=false` or `MEMES_ENABLED=false` to drop the tools and their
prompt guidance entirely; nothing else changes.

## Run

```bash
python bot.py
```

Aura uses Telegram long polling, so no Telegram webhook is required.

## Database and memory

Aura stores conversation history, web-search cache/usage, and long-term memory in `bot.db`.

**Do not commit `bot.db` to GitHub.**

If you are updating an existing Aura installation, keep its existing `bot.db`. The application is designed to preserve and migrate the existing database schema non-destructively.

## Deployment

For a server deployment, keep `.env` and `bot.db` on the server and deploy only the application files and dependencies.

If using a process manager such as cPanel/UBIC, configure the service to run:

```text
python bot.py
```

Do not place server-specific paths, API keys, bot tokens, or production database files in this repository.

## Security

Never commit:

- `.env`
- Telegram bot tokens
- Gemini API keys
- You.com API keys
- Pollinations.ai API keys
- `bot.db`
- logs containing API URLs or credentials

If a credential is ever exposed, revoke/rotate it immediately.

## Project status

This repository represents the Production Gemini architecture of Aura.
