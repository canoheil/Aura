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
- Smart sticker sending (model-chosen tags from a personal sticker pack, with cooldowns)
- Configurable model and search settings
- Environment-based secrets and configuration

## Requirements

- Python 3.11+
- A Telegram Bot Token
- A Google AI Studio / Gemini API key
- Optional: a You.com API key for web search
- Optional: a Pollinations.ai API key for image generation and editing

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
