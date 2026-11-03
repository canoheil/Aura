# Model providers

Aura is not tied to one vendor. `LLM_PROVIDER` selects how the model is reached,
and only the selected provider needs a key — both can be filled in, and `/admin`
switches between them at runtime without a restart.

| Provider | What it is | Settings |
| --- | --- | --- |
| `gemini` (default) | native Google SDK, function calling, inline images | `GEMINI_API_KEY`, `GEMINI_MODEL` |
| `openai_compat` | anything speaking `POST /chat/completions` | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` |

```env
# native Google SDK
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.1-flash-lite

# or any OpenAI-compatible gateway
LLM_PROVIDER=openai_compat
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

`openai_compat` covers OpenAI, OpenRouter, Groq, DeepSeek, Together, Fireworks,
LM Studio, vLLM and Ollama. Point `OPENAI_BASE_URL` at the gateway root — the
`/chat/completions` path is appended for you.

| Gateway | Base URL |
| --- | --- |
| OpenAI | `https://api.openai.com/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| Ollama | `http://localhost:11434/v1` |

## The same runtime either way

Both paths run the *same* agent runtime: the same `ToolRegistry`, the same
`AgentRunner` budgets, duplicate-call suppression, forced web search on
time-sensitive questions, and the same refusal to answer a "current" question
without retrieved evidence. Switching a provider never weakens those guarantees,
and nothing in the handlers knows which provider is active.

## Timeouts and retries

| Setting | Default | Meaning |
| --- | --- | --- |
| `LLM_TIMEOUT_SECONDS` | `120` | One model request |
| `LLM_MAX_RETRIES` | `2` | Retries for transient provider failures |

`retry_policy.py` classifies failures — rate limits, transient 5xx, timeouts —
and only retries the ones that can succeed on a second attempt. A permanent
error (a bad key, an unknown model) is reported to the user in their language
instead of being retried into a longer wait.

## Keys

Provider choice, keys and models can be changed from Telegram with `/admin`.
Keys entered there are encrypted at rest with `cryptography`, using
`AURA_SECRET_KEY` or a key generated once into `.aura_secret_key` next to the
database. They are never returned to a caller: `/admin show` prints a mask such
as `sk-a…9f3 (51 chars)`.

If you rotate `AURA_SECRET_KEY`, keys stored in the database become unreadable
and must be re-entered. Keys supplied through the environment are unaffected.

## Degrading instead of failing

A deployment that only has an OpenAI-compatible key starts and runs without
`GEMINI_API_KEY`. Two extras are Gemini-only — AI-chosen message reactions and
the `/models` listing — and they degrade gracefully: the feature reports that it
is unavailable instead of failing a turn, and nothing else changes.

If a provider rejects a forced tool mode (some gateways do), Aura retries the
round without forcing and still requires search evidence before presenting a
current answer. See [Search and retrieval](search.md).
