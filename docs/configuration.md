# Configuration

Everything Aura reads at startup comes from `.env` (copied from
[`.env.example`](../.env.example)), and almost everything can also be changed at
runtime from Telegram with `/admin`.

The precedence for every setting is the same:

```
database override  →  environment variable  →  built-in default
```

so `/admin unset <key>` returns a value to its `.env` source, and a deployment
can keep secrets in `.env` while the owner tunes behaviour from the chat.

Nothing here is required beyond the token and one model key: every optional
feature disables itself and disappears from the prompt when its prerequisite is
missing.

## Required

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `GEMINI_API_KEY` **or** the `OPENAI_*` trio | One model provider ([details](providers.md)) |

## Administration

| Variable | Default | Purpose |
| --- | --- | --- |
| `FOUNDER_USER_ID` | — | Your Telegram ID. The founder always has `/admin`, `/settings`, and an unlimited quota |
| `ADMIN_USER_IDS` | — | Additional admins, comma-separated Telegram IDs |
| `AURA_SECRET_KEY` | generated | Fernet key used to encrypt provider keys stored from `/admin` |
| `BOT_NAME` | `Aura` | Name used in greetings and the prompt |

`/admin` works in **private chats only**, because it can show provider hostnames
and key hints. Any change is validated before it is stored — an out-of-range
number or an unknown key is rejected with the reason — and every change is
written to `app_settings_audit` with the actor's Telegram ID.

```
/admin                     open the panel
/admin set <key> <value>   change one setting
/admin unset <key>         drop the override (falls back to .env, then default)
/admin show <key>          explain a setting; secrets stay masked
/admin test                call the configured provider and report reachability
/admin audit               recent changes with who made them
```

Toggles can also be flipped with inline buttons. Provider, model, keys, agent
budgets, quota and persona settings apply on the next message; feature switches
and `MAX_CONCURRENT_CHATS` are read at startup and are marked as needing a
restart.

Secrets are never echoed back. `/admin show` prints a mask such as
`sk-a…9f3 (51 chars)`, and stored values are encrypted with `cryptography`
(installed by `requirements.txt`) using `AURA_SECRET_KEY`, or a key generated
once into `.aura_secret_key` next to the database. Rotating the key makes stored
keys unreadable — re-enter them; environment keys are unaffected.

## Identity and persona

| Variable | Default | Purpose |
| --- | --- | --- |
| `OWNER_NAME` | empty | What Aura calls the person who owns the deployment |
| `PERSONA_TONE` | `balanced` | `balanced`, `warm`, `professional`, `playful` |
| `PERSONA_NOTE` | empty | One line (≤ 400 chars) about how she should talk to you |
| `BOT_LANGUAGE` | `fa` | Default interface language for users who never chose one |

### Owner name

Aura does not need to know who you are to work:

```bash
OWNER_NAME=Sara
# or: /admin set owner_name Sara
```

When it is empty — the default — she simply has no name, and the prompt says so
explicitly, because a model that is not told will invent a plausible one. An
unset, blank or unusable value (only punctuation, control characters, more than
60 characters) is treated as empty and never breaks a reply.

The name is a label, not a credential: it never proves who is speaking, grants
no permissions, and does not change founder verification, which stays tied to
`FOUNDER_USER_ID`. It is not translated, shortened or replaced by a nickname,
and a user cannot set it by claiming to be the owner in chat.

### Persona

How warm, how formal, what she calls you — that is a deployment decision, not a
code change:

| Setting | Values | Meaning |
| --- | --- | --- |
| `persona_tone` | `balanced` (default), `warm`, `professional`, `playful` | how she sounds |
| `persona_note` | free text, up to 400 characters | one line from the owner |

```bash
PERSONA_TONE=warm
PERSONA_NOTE=با من خودمونی حرف بزن و کوتاه جواب بده
```

The default adds **nothing** to the prompt: an unconfigured deployment sends
exactly the prompt it always sent. The block that a configured persona produces
is the lowest-priority layer, placed after the identity and personality
boundaries, so it can never:

- change who Aura is, her creator, or her relationship to anyone;
- relax the honesty rules or let her invent facts, memories or capabilities;
- grant permissions, admin rights or tool access;
- override the interface language.

The note is the one place where owner-written text reaches the system prompt, so
it is treated as data: control characters, newlines and block markers are
stripped, the value is capped at 400 characters, an over-long value is rejected
at set time rather than silently cut, and the prompt tells the model to read it
as configuration rather than as instructions from the person it is talking to.
Persian half-spaces (ZWNJ) are preserved.

## Languages

```text
/language            show the current language
/language en         switch to English
/language fa         switch back to Persian
```

The choice is stored per Telegram user (`user_language` in `bot.db`) and applies
to commands, errors, reminder confirmations and delivery wordings, weather
reports and media notices. Precedence is
**`/language` choice → stored preference → `BOT_LANGUAGE` → `fa`**.

The selected language is written into the system prompt, so Aura answers in it
instead of guessing from the user's last message. Content the user wrote — task
text, quotes, names — is never translated.

Translations live in `i18n.py`. Every key carries both languages, and the test
suite fails on a missing translation, a differing placeholder set, or a Persian
string passed straight to Telegram instead of through the catalog. To add a
language: extend `i18n.LANGUAGES`, add the code to `LANGUAGE_NAMES`, and fill in
`CATALOG`; `i18n.missing_translations()` lists what is still empty.

## Model and agent budgets

See [Model providers](providers.md) for `LLM_PROVIDER`, the keys, the models and
the retry behaviour.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_MAX_ROUNDS` | `5` | Model rounds per turn |
| `AGENT_MAX_TOOL_CALLS` | `10` | Tool calls per turn |
| `MAX_HISTORY_MESSAGES` | `20` | Messages replayed from history |
| `REQUEST_TIMEOUT` | `120` | Telegram-side request timeout |
| `CONTEXT_MANAGER_ENABLED` | `true` | Rolling conversation summaries |
| `CONTEXT_COMPACT_THRESHOLD` | `24` | Messages before a summary is made |
| `CONTEXT_RECENT_MESSAGES` | `16` | Recent messages kept verbatim |
| `CONTEXT_SUMMARY_MAX_CHARS` | `6000` | Cap for the stored summary |
| `MAX_CONCURRENT_CHATS` | `4` | Concurrent Telegram updates, clamped to 1–32 |
| `RECENT_IMAGE_CONTEXT_TTL` | `600` | How long a recent photo's `file_id` stays usable |

## Feature switches

| Variable | Default | Feature |
| --- | --- | --- |
| `WEB_SEARCH_ENABLED` | `true` | Web search ([details](search.md)) |
| `WEATHER_ENABLED` | `true` | Weather ([details](search.md#weather)) |
| `MEMES_ENABLED` | `true` | Memes ([details](media.md#memes)) |
| `STICKERS_ENABLED` | `true` | Smart sticker sending ([details](media.md#stickers)) |
| `POLLINATIONS_ENABLED` | `true` | Photo generation (needs a key) |
| `SVG_IMAGE_ENABLED` | `true` | Vector images ([details](media.md#vector-images-svg)) |
| `REACTION_AI_ENABLED` | `true` | Model-chosen reactions (Gemini only) |
| `CONTEXT_MANAGER_ENABLED` | `true` | Rolling summaries |

Each switch removes both the tool and its prompt section, so Aura never
advertises a capability she cannot use.

## Per-user limits

Aura is often deployed on a public link with a single API key. Two optional caps
keep one account from spending the whole day's budget before anyone else gets an
answer:

```bash
USER_DAILY_TURNS=40      # model turns per Telegram user per UTC day; 0 = unlimited
USER_DAILY_TOKENS=150000 # token budget, when the provider reports usage; 0 = unlimited
```

or at runtime: `/admin set user_daily_turns 40`. Both default to **0, meaning
unlimited** — enabling a quota is an explicit decision, never a side effect of
upgrading.

- The window is the UTC calendar day. `/usage` shows today's counter, the limits
  and when they reset, in the user's own reminder timezone.
- The founder and anyone in `ADMIN_USER_IDS` are never limited, and their usage
  is not counted.
- When a limit is reached, the turn is refused **before** the model is called,
  with a message that says what was used and when it resets. Commands keep
  working: a capped user can still run `/usage`, `/deletemydata`, `/new` and the
  rest.
- `/image` is counted too, since it is the most expensive action available.
- A counter that cannot be read (a corrupt value, a missing row) is treated as
  unlimited rather than blocking everyone.

The counter lives in `user_usage`, one row per user per day; rows for other days
are pruned at startup, and `/deletemydata` removes it with everything else.

## Reference

[`.env.example`](../.env.example) is the annotated list of every variable,
grouped by feature, with the default the code falls back to. It is kept in step
with the code by the test suite: a new setting without a template entry fails.
