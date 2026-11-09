# Search and retrieval

Aura has two keyless retrieval tools (weather, memes) and one that needs a key
(web search). This document covers the web search path, why it sometimes runs
without being asked, and how its cost is contained.

## The freshness gate

A model cannot notice that its own knowledge is stale: it does not know what day
it is, so "who is the president" and "what is the latest release" both feel
answerable from memory and the answer that comes back is whatever was true when
training ended.

`freshness_policy.py` makes that call in the application instead: it looks at the
user's exact wording and returns one of three decisions.

| Decision | Meaning | Example |
| --- | --- | --- |
| `force` | time-dependent question about a named subject | «قیمت دلار چنده», "latest Python version" |
| `hint` | time-dependent wording, subject unclear | «قیمت چنده», «آیا هنوز پشتیبانی می‌شه» |
| `off` | stable knowledge, chit-chat, or another tool owns it | «معنی این کلمه», «یه میم بده» |

On `force` the first agent round is generated with the provider's tool mode
pinned to `web_search`. If the provider rejects forced mode, Aura retries
normally but still requires successful search evidence before presenting a
current answer. If the search is unavailable, empty or failing, Aura says it
cannot verify the answer instead of presenting a remembered fact as current. The
same evidence check applies when forcing is downgraded by the soft ceiling.

On `hint` Aura is told to search unless she is sure. On `off` nothing is added.

## What the user sees

Web tool results stay internal. Aura sends only her answer, never a separate
"retrieved sources" message, so search evidence grounds the reply without
appending a link list the user did not ask for.

Model-generated source sections are discarded unless they were copied from the
user's own text; if nothing else remains, Aura says plainly that she found no
clear answer instead of referencing sources that are not shown. Code indentation
and literal arrays such as `[1]` are preserved.

## Cost is bounded on purpose

- A forced search only happens for `force`, which additionally requires a real
  subject, so bare follow-ups never pay.
- The prompt spells out what must not be searched (opinions, code, translation,
  weather, memes) so ordinary chat stays search-free.
- Cached answers cost nothing (`WEB_SEARCH_CACHE_TTL`), duplicate identical calls
  inside a turn are merged by the agent runner, and `WEB_SEARCH_DAILY_LIMIT`
  remains a hard stop.
- `WEB_SEARCH_ENFORCE_SOFT_LIMIT` stops forcing for the rest of the day once that
  many searches were spent (0 = 80% of the daily limit), leaving the remaining
  quota for questions that genuinely need it.
- `WEB_SEARCH_ENFORCE=false` keeps the prompt guidance and turns the forcing off.

`WEB_SEARCH_FORCE_TERMS` adds comma-separated words of your own (a game name, a
ticker, a channel-specific product) to the force vocabulary.

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `YOU_API_KEY` | — | Enables web search; without it the tools are not registered |
| `WEB_SEARCH_ENABLED` | `true` | Master switch |
| `WEB_SEARCH_DAILY_LIMIT` | `100` | Hard stop for paid searches per day |
| `WEB_SEARCH_MAX_RESULTS` | `10` | Results per query |
| `WEB_SEARCH_CACHE_TTL` | `3600` | Cache lifetime for stable queries |
| `WEB_SEARCH_CURRENT_CACHE_TTL` | `300` | Cache lifetime for current facts |
| `WEB_SEARCH_TIMEOUT` | `20` | One request |
| `WEB_SEARCH_TOOL_MAX_CALLS` | `4` | Searches per turn |
| `WEB_SEARCH_ENFORCE` | `true` | Force a search on time-sensitive questions |
| `WEB_SEARCH_ENFORCE_SOFT_LIMIT` | `0` | Stop forcing after N searches (0 = 80%) |
| `WEB_SEARCH_FORCE_TERMS` | — | Extra terms that count as time-sensitive |
| `WEB_SEARCH_SAFESEARCH` | `moderate` | Provider filter |
| `WEB_SEARCH_COUNTRY`, `WEB_SEARCH_LANGUAGE` | — | Locale hints |
| `WEB_SEARCH_INCLUDE_DOMAINS`, `WEB_SEARCH_EXCLUDE_DOMAINS`, `WEB_SEARCH_BOOST_DOMAINS` | — | Domain filters |
| `WEB_CONTENTS_ENABLED` | `true` | Open a specific page for deeper verification |
| `WEB_CONTENTS_DAILY_LIMIT` | `10` | Page opens per day |
| `WEB_CONTENTS_CACHE_TTL` | `3600` | How long an opened page stays cached |
| `WEB_CONTENTS_TIMEOUT` | `20` | Timeout for one page fetch |
| `WEB_SEARCH_FRESHNESS` | `auto` | Provider freshness hint |
| `WEB_SEARCH_EXTRACTION` | `highlights` | What the provider returns: `none`, `highlights` or `full_page` |
| `WEB_SEARCH_CRAWL_TIMEOUT` | `10` | Seconds the provider may spend reading a page (1–60) |
| `WEB_SEARCH_OFFSET` | `0` | Result offset, for paging past the first page of hits |

Opening pages has its own budget (`WEB_CONTENTS_*`) because it costs more than a
search result snippet.

## Weather

Weather has [its own page](weather.md), including the card Aura draws beside a
forecast; the short version follows.

Weather is keyless, needs no extra dependency, and runs fine on shared hosting:
it is a pair of plain HTTPS calls with `requests`.

`weather_api.py` registers a `get_weather` tool. Aura passes the city exactly as
the user wrote it (Persian or English); the module resolves it with the Open-Meteo
geocoding API, fetches current conditions plus a 3-day forecast, maps WMO weather
codes to Persian descriptions, and caches a city for `WEATHER_CACHE_TTL` seconds
so repeated questions cannot hammer the free service. Only a compact summary
reaches the model — never the raw payload.

For an explicit weather question with a named city («هوای تهران چطوره؟»,
«آب و هوا مشهد», "weather in Berlin"), Aura resolves and fetches the conditions
**before** the model answers, so a city with a famous namesake cannot be
silently swapped. Talk-about sentences ("درباره هواشناسی چیکار کنم", "چند درجه
سرد شده") and image/meme requests stay out of that path.

When geocoding returns multiple places with the same name, Aura asks the user to
select a numbered location rather than silently choosing the first; the
selection is scoped to that chat and user for five minutes. In a group, reply to
Aura's question with the number. An explicit comma-separated region also works,
e.g. "weather in Springfield, Massachusetts".

| Variable | Default | Purpose |
| --- | --- | --- |
| `WEATHER_ENABLED` | `true` | Master switch |
| `WEATHER_GEOCODE_URL` | Open-Meteo | Geocoding endpoint |
| `WEATHER_FORECAST_URL` | Open-Meteo | Forecast endpoint |
| `WEATHER_TIMEOUT` | `8` | Total timeout for geocoding + forecast |
| `WEATHER_CACHE_TTL` | `600` | Cache lifetime per city |
| `WEATHER_TOOL_MAX_CALLS` | `2` | Calls per turn |
| `WEATHER_CARD_ENABLED` | `true` | The forecast card ([details](weather.md#the-weather-card)) |
