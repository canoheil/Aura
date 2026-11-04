# Contributing to Aura

Thanks for taking the time to contribute. Aura is a self-hosted Telegram
assistant: it holds live credentials, spends real API quota, and talks to real
people, so the rules below exist to keep changes safe as well as useful.

## Getting started

```bash
git clone https://github.com/canoheil/Aura.git
cd Aura
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime deps + pytest
cp .env.example .env                  # then fill in your own credentials
```

You do **not** need real credentials to run the tests: the suite is hermetic and
uses fake Telegram/Gemini clients.

## Running the tests

```bash
cd /path/to/Aura                     # any working directory works
python -m pytest                     # whole suite
python -m pytest tests/test_answer_quality.py -v
python -m pytest -k reminder         # a subset
```

Everything must pass before a pull request is merged; CI runs the same command.
When you change behavior, add or update a test that fails without your fix.

If a test needs credentials it is written wrong — the suite must stay offline and
must never call Telegram, Gemini, You.com, Pollinations or mimir endpoints.

## Linting

`ruff` is the only linter, configured in `pyproject.toml`. It does not format
code (yet): the rule set is deliberately limited to mistakes — unused imports,
undefined names, syntax errors, import order — so the diff of a pull request
stays about the change.

```bash
ruff check .        # what CI runs
ruff check . --fix  # apply the automatic fixes (mainly import order)
```

`bot.py` is one large module and `ruff format` would rewrite it wholesale. If you
want to adopt the formatter, do it in its own pull request with no other changes.

## Pre-commit hooks (optional, recommended)

The same checks can run before you commit, so CI rarely surprises you:

```bash
pip install -r requirements-dev.txt
pre-commit install                        # ruff + file hygiene on commit
pre-commit install --hook-type pre-push   # the full test suite before push
pre-commit run --all-files                # run everything once, on demand
```

Install the hooks from **inside the active virtualenv**: the test hook runs
`python -m pytest`, and `python` must be the interpreter that has the project's
dependencies. One hook is specific to this project: `repo_guard.py` refuses to
commit `.env`, `bot.db`, `.aura_secret_key` or any other database file, even when
`git add -f` is used.

## Code layout

| File | Responsibility |
| --- | --- |
| `bot.py` | Telegram handlers, prompts, Gemini calls, delivery |
| `tool_system.py` | Tool registry and the bounded agent loop |
| `answer_quality.py` | Evidence checks and cleanup of model output |
| `freshness_policy.py` | Decides when a search is mandatory |
| `reminders.py` | Reminder parsing, storage, delivery |
| `business_mode.py` | Secretary mode: the policy for answering someone else's messages, with no Telegram imports |
| `group_memory.py` | Read-only digest of what a shared group already said, plus the membership cache |
| `telegram_formatting.py` | HTML rendering and chunking |
| `*_image.py`, `weather_api.py`, `meme_api.py`, `sticker_manager.py` | Optional capabilities |

One trap is worth knowing on day one: python-telegram-bot runs **at most one
handler per group**, and the group-access guard is a `TypeHandler(Update)` that
matches every update. Registering anything beside it in the same group makes that
handler dead code — which is how a `BusinessConnectionHandler` spent a release
never running. Each kind of update gets its own group (see
[Architecture](docs/architecture.md#the-other-entry-point)), and a test asserts the
ordering.

Application modules stay flat at the repository root; the tests live in `tests/`,
one `test_*.py` file per module or feature. `pyproject.toml` adds the repository
root to `sys.path`, so a test imports `bot`, `i18n` or `reminders` directly, and
reads committed files through a module-level `ROOT` path
(`Path(__file__).resolve().parent.parent`) rather than the current directory.

## Documentation

`README.md` is the front door and stays short: what Aura is, why it is
different, how to start it in five commands, and a table of links. Long-form
material lives in `docs/`, one topic per file, and `README.fa.md` mirrors the
front page in Persian.

Three rules are enforced by `tests/test_docs.py`, so documentation rots loudly
instead of quietly:

- every relative link in these files must resolve;
- every page in `docs/` must be linked from `README.md`;
- every setting in `settings_store.SPECS` must appear in `.env.example`.

When you add a feature, update the document that owns it rather than appending a
new section to the README.

## Style

- Python 3.11+, standard library first. `ruff check .` must pass.
- Follow the style of the file you are editing; keep diffs focused.
- Prefer one clear function over clever indirection; this code is read during
  incidents.
- Log actionable context (`logger.warning("... | chat=%s", ...)`), never secrets,
  tokens, full user messages or database paths.
- Any user-facing string belongs in Persian unless the change is explicitly about
  localization.

## Safety rules that are not negotiable

- **Never commit secrets.** `.env`, tokens, API keys, `bot.db`, logs and PID files
  stay out of Git. `git add -A` after a shell session is how this usually breaks.
- **Web and file content is untrusted data.** It must never be able to instruct
  the model to call a tool or change identity. Do not weaken that framing.
- **No unverified claims of action.** If the bot says a reminder was scheduled or
  an image was sent, a tool result must prove it.
- **Costs are bounded.** Every new tool needs `max_calls_per_run`, and every paid
  request path needs a quota or cache story.
- **Do not add hidden network calls** at import time or in tests.

## Pull requests

1. Branch from `main`.
2. Keep one concern per pull request; describe what and why in the template.
3. Include tests and update `README.md` when behavior or configuration changes.
4. Be patient during review — maintainers verify changes against a live bot.

Commit messages: imperative mood, short summary line, optional body explaining
the trade-off.

## Reporting bugs and proposing features

Use the issue templates. For anything security-related, follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.

Participation is covered by the [Code of Conduct](CODE_OF_CONDUCT.md); report
unacceptable behaviour privately, the same way as a security issue.

## Translations

Persian is the primary language of the bot's voice and prompt. Improvements to
Persian phrasing are welcome; if you want to introduce a second language, open an
issue first so the configuration shape can be agreed on.

## License

By contributing you agree that your contribution is licensed under the MIT
License in [LICENSE](LICENSE), and that you have the right to submit it.
