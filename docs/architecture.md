# Architecture

Aura is a flat set of Python modules around one long-running process. The
application modules sit at the repository root — `bot.py` is run directly, not
installed as a package — and the tests live in `tests/`.

## The turn

```
Telegram update
      │
      ▼
bot.py handler ──▶ classify (text? photo? sticker? reminder? command?)
      │
      ▼
prompt assembly ──▶ identity · boundaries · persona · language · date anchor ·
                    feature sections that are actually available
      │
      ▼
agent loop (tool_system.py) ──▶ model ⇄ tool registry
      │                            web_search · web_open · get_weather ·
      │                            fetch_meme · generate_image · create_svg_image ·
      │                            file_analyze · memory_* · schedule_reminder ·
      │                            send_sticker
      ▼
answer quality gate ──▶ delivery (HTML, chunked) ──▶ SQLite
```

The loop has a fixed budget — `AGENT_MAX_ROUNDS` (5) and `AGENT_MAX_TOOL_CALLS`
(10) — plus a per-tool cap such as `WEB_SEARCH_TOOL_MAX_CALLS`. Identical tool
calls inside one turn are merged so a model that repeats itself does not pay
twice. The loop stops as soon as the evidence is sufficient; there is no second
"planner" model call.

## The other entry point

There is a second place a message can arrive — the owner's Telegram **Business
account**, where a friend writes privately while the owner is away
([details](business.md)). It is a separate entry point, not a separate assistant:

```text
business_message ──▶ handler group -2 ──▶ attachment? ──▶ group context?
                                              │              (memberships verified)
                                              ▼
                                    the same prompt, the same agent loop
```

Three things are worth knowing before changing anything near it:

- **It has its own handler groups.** python-telegram-bot runs *at most one
  handler per group* (`process_update` breaks after the first match), and the
  group-access guard is a `TypeHandler(Update)` that matches everything. Anything
  registered beside the guard in group `-1` is therefore dead code — which is
  exactly how the `BusinessConnectionHandler` was silently ignored once. The
  layout is: `-3` business connections, `-2` business messages, `-1` the guard
  alone, `0` commands and ordinary messages. `tests/test_business_mode.py`
  asserts the ordering so it cannot come back.
- **It subtracts instead of forking.** The turn is `build_system_prompt()` plus a
  situational layer, run through `ask_model()`. The only difference is
  `BUSINESS_EXCLUDED_TOOLS` (memory and reminders), removed from the tool
  declarations *and* from the runner — hiding a declaration is not a guarantee,
  refusing the execution is.
- **Context crosses one way.** `group_memory.py` may read what was said in a group
  into a private reply, and has no write path at all. Membership is verified with
  `get_chat_member` and cached, so "is this person in that group" is answered by
  Telegram rather than guessed.

## The prompt is layered

Every layer has a different authority, and they are appended in this order:

| Layer | Source | Can it be changed from chat? |
| --- | --- | --- |
| Base character and rules | `SYSTEM_PROMPT` in `bot.py` | No |
| Interface language | `i18n.system_instruction()` | No — a per-user setting |
| Verified identity | `aura_identity.py` | No — founder ID and settings only |
| Self-identity boundary | `aura_self_identity.py` | No |
| Personality boundary | `aura_personality_boundary.py` | No |
| Owner persona | `persona.py` | Yes, `/admin set persona_*` — style only |
| Feature sections | sticker, image, SVG, weather, freshness | Only by enabling the feature |

Feature sections are added **only when the capability exists**, so Aura is never
told about a tool she cannot call. The same rule keeps the meme tool out of
ordinary turns: `fetch_meme` takes no required arguments, so a standing
declaration made the model look for a reason to use it on every reply.

The persona layer is the lowest-priority one and says so in as many words: it may
change tone, never identity, honesty, permissions or the interface language.

## Why the freshness gate is code, not prompt

A prompt line saying "search when the question is current" is a suggestion; a
model that does not know today's date cannot follow it reliably. So the decision
is made in the application (`freshness_policy.py`), and the model is only told
what was decided. See [Search and retrieval](search.md).

The same principle runs through the rest of the codebase: anything that must be
true — a reminder was committed, a photo was delivered, a quota was spent — is
checked by the application and reported to the model, not the other way round.

## Answer quality

`answer_quality.py` holds the rules that keep a reply honest and copy-safe:

- web evidence counts only when it is a real result with a URL and text;
- a model-generated "sources" section is removed unless it quotes the user;
- if nothing survives and the question needed evidence, Aura says she could not
  verify instead of producing a confident answer;
- code blocks, indentation and literal arrays survive untouched.

`telegram_formatting.py` renders the Markdown-like syntax to Telegram HTML,
splits long answers on safe boundaries, and falls back to plain text rather than
failing a delivery.

## State

One SQLite file, one connection, one re-entrant lock. Tables are created by the
module that owns them; structural changes go through `migrations.py`. Every
setting has a single registry entry (`settings_store.SPECS`) that drives
validation, the `/admin` panel, the environment fallback and the template.

## Languages

`i18n.py` is a flat catalog: every key carries every language, and the suite
fails on a missing translation, a differing placeholder set, or a Persian string
passed straight to Telegram instead of through the catalog. Persian is the
default; the interface language is stored per user and also written into the
system prompt so the model does not have to infer it.

## Testing

The suite is hermetic: fake Telegram and model clients, an in-memory SQLite
database, no network. A single fixture (`isolated_bot` in
`tests/conftest.py`) loads the real `bot.py` against those fakes, which means the
handler tests exercise production code paths rather than a reimplementation.

Two conventions are worth knowing before adding a test:

- anything that must never regress — a guard rail, a privacy promise, a quota —
  gets a test that fails without the fix, not a comment;
- a test that reads source (`bot.py`, `.env.example`, the requirements files)
  resolves it from `ROOT`, the repository root, so the suite passes from any
  working directory.

## Adding a capability

1. Put the logic in its own module with no Telegram imports, so it can be tested
   directly.
2. Register a tool in `tool_system` from `setup_tool_registry`, with an explicit
   per-run cap.
3. Add the prompt section, gated on the feature actually being available.
4. Add the setting to `settings_store.SPECS` **and** `.env.example` — a test
   fails if the template is missing a setting.
5. Add the failure copy to `i18n.py` in both languages.
6. Wire delivery in `bot.py`, and keep the turn honest when the tool fails.
