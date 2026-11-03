# Aura — Production Gemini Build

[![tests](https://github.com/canoheil/Aura/actions/workflows/tests.yml/badge.svg)](https://github.com/canoheil/Aura/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Aura is a personal Telegram AI assistant powered by Google's Gemini API.

This repository contains the public application code and configuration template. Secrets, the local SQLite database, logs, PID files, and virtual environments are intentionally excluded.

Aura is open source under the [MIT License](LICENSE). Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md), and report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Third-party attributions live in [NOTICE](NOTICE).

## Features

- Gemini-powered Telegram conversations
- Persistent conversation history in SQLite
- Long-term memory
- Guarded You.com web search
- Group-chat triggers and speaker awareness
- Telegram photos, image documents, direct voice/audio/video and short-term image follow-ups
- Persistent one-shot reminders with a per-user timezone, listing and cancellation
- Bounded concurrency across chats, with sequential updates within each chat
- Image generation from text and image-to-image editing via Pollinations.ai (optional)
- Quota-free vector image creation: Aura writes SVG herself and it is rendered locally to PNG
- Current conditions and 3-day forecast for any city via Open-Meteo (keyless)
- Real Reddit memes fetched with a keyless API, then commented on from the actual picture
- Smart sticker sending (model-chosen tags from a personal sticker pack, with cooldowns)
- Persian and English interfaces, switched per user with `/language`
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

`requirements-pinned.txt` holds the exact versions the current build was
validated against; use it instead of `requirements.txt` when you need a
reproducible environment.

### Required variables

```env
TELEGRAM_BOT_TOKEN=...
```

plus **one** model provider:

```env
# native Google SDK
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.1-flash-lite

# or any OpenAI-compatible gateway
LLM_PROVIDER=openai_compat
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

### Optional variables

- `BOT_NAME`
- `BOT_LANGUAGE`
- `OWNER_NAME`
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
- `MAX_CONCURRENT_CHATS`
- `RECENT_IMAGE_CONTEXT_TTL`
- `REMINDER_DEFAULT_TIMEZONE`
- `REMINDER_POLL_SECONDS`
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

## Model providers

Aura is not tied to one vendor. `LLM_PROVIDER` selects how the model is reached,
and only the selected provider needs a key.

| Provider | What it is | Typical settings |
| --- | --- | --- |
| `gemini` (default) | native Google SDK, function calling, inline images | `GEMINI_API_KEY`, `GEMINI_MODEL` |
| `openai_compat` | anything speaking `POST /chat/completions` | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` |

`openai_compat` covers OpenAI, OpenRouter, Groq, DeepSeek, Together, Fireworks,
LM Studio, vLLM and Ollama (`http://localhost:11434/v1`). Point
`OPENAI_BASE_URL` at the gateway root — the `/chat/completions` path is appended.

Both paths run the *same* agent runtime: the same `ToolRegistry`, the same
`AgentRunner` budgets, duplicate-call suppression, forced web search on
time-sensitive questions, and the same refusal to answer a "current" question
without retrieved evidence. Switching a provider never weakens those guarantees.

Provider choice, keys and models can be changed from Telegram with `/admin`
(see below); changes take effect on the next message, no restart needed. Keys
entered there are encrypted at rest with `cryptography` using a key from
`AURA_SECRET_KEY` or a generated `.aura_secret_key` file.

A deployment that only has an OpenAI-compatible key starts and runs without
`GEMINI_API_KEY`. The two Gemini-only extras — AI-chosen message reactions and
the `/models` listing — degrade gracefully instead of failing a turn.

## Administration from Telegram

`/admin` opens a panel for the founder (`FOUNDER_USER_ID`) and anyone listed in
`ADMIN_USER_IDS` (comma-separated Telegram user IDs). It works in private chats
only, because it can show provider hostnames and key hints.

```
/admin                     open the panel
/admin set <key> <value>   change one setting
/admin unset <key>         drop the override (falls back to .env, then default)
/admin show <key>          explain a setting; secrets stay masked
/admin test                call the configured provider and report reachability
/admin audit               recent changes with who made them
```

Toggles can also be flipped with the inline buttons. Settings are validated on
write — an out-of-range number or an unknown key is rejected with the reason,
not stored. Every change is recorded in `app_settings_audit`.

What is editable: provider, keys, models and timeouts; the agent round and
tool-call budgets; feature switches (web search and its daily limit, weather,
memes, stickers, image generation, SVG, reactions, rolling summaries); the
default timezone and language. Precedence is always
**database override → environment → default**, and `unset` returns a value to
its `.env` source.

Two classes of change behave differently:

- provider, model, keys and agent budgets apply immediately;
- feature switches and `MAX_CONCURRENT_CHATS` are read at startup and marked as
  needing a restart.

Secrets are never echoed back. `/admin show` prints a mask such as
`sk-a…9f3 (51 chars)`, and the stored value is encrypted when `cryptography` is
installed (`pip install -r requirements.txt` installs it). If you rotate
`AURA_SECRET_KEY`, stored keys become unreadable and must be re-entered —
environment keys are unaffected.

## Owner name (optional)

Aura does not need to know who you are to work, but a self-hosted deployment can
tell her your name:

```bash
OWNER_NAME=Sara
```

or, at runtime, `/admin set owner_name Sara`. When the value is set, Aura uses
that name when she talks about her owner. When it is empty — the default — she
simply has no name: the prompt tells her so explicitly, because a model that is
not told will invent a plausible one. An unset, blank or unusable value (only
punctuation, control characters, more than 60 characters) is treated as empty
and never breaks a reply.

The name is a label, not a credential: it never proves who is speaking, grants
no permissions, and does not change founder verification, which stays tied to
`FOUNDER_USER_ID`. It is not translated, shortened or replaced by a nickname,
and a user cannot set it by claiming to be the owner in chat.

## Languages

Aura speaks Persian and English. Persian is the default, so an untouched
deployment behaves exactly as before.

```
/language            show the current language
/language en         switch to English
/language fa         switch back to Persian
```

The choice is stored per Telegram user (`user_language` table in `bot.db`) and
applies to commands, errors, reminder confirmations and delivery wordings,
weather reports and media notices. Precedence is
**`/language` choice → stored preference → `BOT_LANGUAGE` → `fa`**.

`BOT_LANGUAGE` (or `/admin set bot_language en`) sets the deployment default for
everyone who has not chosen a language. It is a change to Aura's own voice, not
to the model: the selected language is written into the system prompt, so Aura
answers in it instead of guessing from the user's last message. Content the user
wrote — task text, quotes, names — is never translated.

Translations live in `i18n.py`. Every key carries both languages, and the test
suite fails on a missing translation, a differing placeholder set, or a Persian
string passed straight to Telegram instead of through the catalog. To add a
language: extend `i18n.LANGUAGES`, add the code to `LANGUAGE_NAMES` and fill in
`CATALOG`; `i18n.missing_translations()` lists what is still empty.

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

Telegram photos, supported image documents (JPEG, PNG, WebP, HEIC/HEIF), and
direct or replied-to audio, voice, video, video notes and animation are supplied to Gemini
inline when the selected model accepts their MIME type. Photo/image input is
limited to 8 MiB; inline audio/video is limited to the smaller of 12 MiB and
`FILE_MAX_SIZE_MB`, and Telegram's own download limit also applies. Other
documents continue through the `file_analyze` tool. After a photo, Aura keeps
only its Telegram `file_id` (not image bytes), scoped to the same chat and user,
for up to `RECENT_IMAGE_CONTEXT_TTL` seconds (default 600). Recognizable short
image follow-ups can therefore reuse it without another reply; `/new` or a bot
restart clears this temporary context. Follow-up recognition is conservative
and will not cover every phrasing.

If Gemini emits an unexpected textual tool declaration instead of an answer, Aura
retries once with the same media, evidence and tool restrictions. If it remains
invalid, Aura returns an error rather than executing or storing the raw output.
The retry adds one model request only when this problem occurs. A declaration
explicitly supplied by the user can still be quoted in a technical discussion.

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
`web_search`; if the provider rejects forced mode, Aura retries normally but
still requires successful search evidence before presenting a current answer.
If the search is unavailable, empty, or fails, Aura says it cannot verify the
answer instead of presenting a remembered fact as current. The same evidence
check applies when forcing is downgraded by the soft ceiling. On `hint` Aura is
told to search unless she is sure; `off` adds nothing.

Web tool results stay internal. Aura sends only her answer, never a separate
“retrieved sources” message, so search evidence grounds the reply without
appending a link list the user did not ask for. Model-generated source sections
are discarded unless copied from the user's own text; if nothing else remains,
Aura says plainly that she found no clear answer instead of referencing sources
that are not shown. Code indentation and literal arrays such as `[1]` are
preserved.

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
service. Only a compact summary reaches the model — never the raw payload. For
an explicit weather question with a named city («هوای تهران چطوره؟»,
«آب و هوا مشهد», "weather in Berlin"), Aura resolves and fetches the conditions
before the model answers; talk-about sentences ("درباره هواشناسی چیکار کنم",
"چند درجه سرد شده") and image/meme requests stay out of it. When geocoding returns multiple places with
the same name, Aura asks the user to select a numbered location rather than
silently choosing the first; the selection is scoped to that chat and user for
five minutes. In a group, reply to Aura's question with the number. An explicit
comma-separated region also works, e.g. “weather in Springfield, Massachusetts”.

`meme_api.py` registers `fetch_meme`, which picks a real Reddit post from
meme-api.com (with a mirror fallback), downloads the image bytes with a hard
size cap, refuses NSFW/spoiler posts and non-images, skips memes already sent
recently, and maps topic words to sensible subreddits
("پرنده" → `birdmemes`). The fetched bytes are delivered after the text reply and
shown to Gemini in the same agent turn, so Aura comments on the actual picture.
The tool and its guidance are only advertised on turns that are actually about
memes (a request, a question about one, or a short «یکی دیگه» follow-up), so an
ordinary chat message never carries the extra tool declaration, the extra model
round trip, or a Reddit download. If Telegram rejects both the image and its
document fallback, Aura attempts to notify the user instead of silently
claiming delivery.

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

## Reminders

Reminders are **one-shot**, saved in `bot.db`, and scoped to the requesting user
and chat. The default timezone is `Asia/Tehran`; each user can choose another
IANA timezone. Existing reminders retain their original UTC instant when the
timezone changes.

```text
۱۰ دقیقه دیگه یادم بنداز آب بخورم
دو دقیقه دیگه یادم بنداز آب بخورم
پنج دقیقع دیگه یادم بیار فلان کارو بکنم
یادم بنداز فردا ساعت ۹:۳۰ قرص بخورم
/remind in 10m آب بخورم
/remind 2030-11-01 09:30 جلسه
/reminders
/cancelreminder 12
/timezone Europe/Berlin
```

Explicit dates use the **Gregorian calendar** and 24-hour time. Persian written
numbers and common informal variants (including `یه ربع`, `نیم ساعت` and
`دقیقع`) are supported. Parsing remains conservative: if the time or task is
unclear, Aura asks instead of claiming a reminder was created. If only the time
is missing, a short follow-up such as `دو دقیقه دیگه` completes the same
request. These incomplete conversations are scoped to the same user and chat,
expire after ten minutes, and are cleared by `/new` or a restart; they are not
registered reminders. After a change of topic, repeat the complete request
rather than relying on an ambiguous follow-up. A short `لغوش کن` can cancel
the most recently referenced reminder in that same user/chat context; when the
target is unclear, use its ID with `/cancelreminder`.

Aura also has a real reminder tool for less formulaic requests. It must quote
the user's own task and time, and the application validates them and commits to
SQLite before reporting success. Check `/reminders` for authoritative status.
Repeating schedules are not supported. Nonexistent or repeated local times at
daylight saving transitions are rejected; an explicit UTC offset, e.g.
`2030-10-27 02:30+02:00`, disambiguates them. At most 100 pending reminders are
allowed per user.

After a successful commit, Gemini can phrase a short confirmation using Aura's
normal personality and verified identity context. At the due time it can also
generate a brief, task-specific reminder. Confirmations and deliveries do not
automatically add the timestamp, timezone, ID, or a second copy of the task;
those details remain available through `/reminders`. These are extra model
calls. A conservative wording check rejects obvious changes or omissions of
task details; it is not a semantic guarantee. If generation fails, times out,
or does not pass that check, Aura uses a short fallback with the saved task so
the reminder can still be delivered. In an allowed group the application creates
a Telegram text mention from the stored requester ID and an escaped display-name
snapshot, never an ID chosen by the model. If Telegram rejects the mention, it
tries a plain-text delivery instead. Older reminders are migrated automatically
and use a generic label if no display name was stored.

A background worker resumes overdue reminders when the bot starts. It retries
transient Telegram failures; permanent failures appear in `/reminders`, where
they can be cancelled. Delivery requires the bot to be running and able to send
to the chat. Run only one long-polling Aura instance against a database. A
crash after Telegram accepts a message but before SQLite records success can
cause a duplicate on restart; cancellation cannot recall a send already in
progress.

## Concurrency

`MAX_CONCURRENT_CHATS` defaults to 4 and is clamped to 1–32. It bounds concurrent
Telegram updates; commands and messages in the same chat are processed in order,
while different chats can progress independently. Paid web calls are serialized
per service to keep the existing daily quota checks safe.

## Run

```bash
python bot.py
```

Aura uses Telegram long polling, so no Telegram webhook is required.

## Tests

The suite is hermetic: it uses fake Telegram and Gemini clients, needs no API
keys, and makes no network calls.

```bash
pip install -r requirements-dev.txt   # runtime dependencies + pytest
python -m pytest
```

Everything must pass before a pull request is merged; the same command runs in
CI (`.github/workflows/tests.yml`). Tests live next to the modules they cover and
are named `test_*.py`. When you change behavior, add a test that fails without
your fix.

## Running as a service

`aura_service.sh` (start/stop/restart/status) and `aura_watchdog.sh` (cron
restart guard) resolve their paths from their own location, so a checkout works
anywhere:

```bash
./aura_service.sh start
AURA_DIR=/srv/aura ./aura_watchdog.sh    # from cron, e.g. every minute
```

Overrides, if you keep data outside the checkout:

| Variable | Purpose | Default |
| --- | --- | --- |
| `AURA_DIR` | project directory | directory of the script |
| `AURA_PYTHON` | interpreter to run | `.venv/bin/python`, else `python3` |
| `AURA_PIDFILE` | PID file | `$AURA_DIR/aura.pid` |
| `AURA_LOGFILE` | service log | `$AURA_DIR/aura.log` |
| `AURA_SERVICE` | service script the watchdog controls | `$AURA_DIR/aura_service.sh` |

`aura_service.sh status` exits with `3` when Aura is stopped, so monitoring and
`set -e` scripts can rely on it.

For a real init system, prefer a systemd unit over the shell wrapper.

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

If a credential is ever exposed, revoke/rotate it immediately. For reporting a
vulnerability, use the private process in [SECURITY.md](SECURITY.md) — please do
not open a public issue with exploit details.

## Project status

This repository represents the Production Gemini architecture of Aura. It is
under active development on `main`; only `main` is supported.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 canoheil and Aura
contributors.

Bundled third-party assets and dependency licenses are listed in
[NOTICE](NOTICE). The Vazirmatn fonts in `assets/fonts/` are distributed under
the SIL Open Font License 1.1, reproduced in `assets/fonts/OFL.txt`.
