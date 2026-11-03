# Data and privacy

Aura keeps everything it knows in one SQLite file, `bot.db`, next to the
application. There is no analytics service, no telemetry endpoint and no
third-party database: data leaves the machine only inside the request needed to
answer a message.

## What is stored

| Data | Keyed by | Why |
| --- | --- | --- |
| Conversation history and summaries | user (private chat) or chat (group) | Context for the next message |
| Long-term memory | user | Things the user asked her to remember |
| Reminders, reminder timezone | user + chat | Delivering the reminder |
| Language choice | user | Answering in the right language |
| Usage counters (`user_usage`) | user + UTC day | Optional per-user quotas |
| Allowed groups | chat | Group access control |
| Settings and their audit trail | — | `/admin` |
| Sticker cache | — | Fallback when Telegram's pack listing fails |
| Web caches | query | Avoiding duplicate paid calls |

Conversation history, memory and reminders can be deleted by the person they
belong to; the web caches, settings and the group whitelist are shared state and
are deliberately not part of that.

## `/deletemydata`

```text
/deletemydata            show exactly what is stored, with counts
/deletemydata confirm    really delete it (valid for 5 minutes)
/deletemydata cancel     change your mind
```

- The preview is the promise: both messages are rendered from the same
  `CATEGORIES` list in `data_deletion.py`, and a test fails if a personal table is
  added without being listed there.
- The wipe is one transaction across every table, so a failure leaves the data
  intact instead of half-deleted.
- It only works in a **private chat**. A group's history is shared and contains
  other members' messages, so clearing it from one member's command would delete
  everyone's.
- Shared, non-personal state is untouched: the web-search cache and its daily
  counters, `app_settings`/`app_settings_audit`, `allowed_groups` and the sticker
  cache.
- The confirmation is written in the language the user was using, even though
  the language row itself is gone by then.

Deleting rows does not shrink the file on disk (SQLite reuses the freed pages).
To reclaim space on a server:

```bash
sqlite3 bot.db 'VACUUM'
```

Backups are the deployment's responsibility: Aura cannot delete copies that
already exist in an operator's `bot.db.bak`.

## Schema versions and migrations

Every table is created with `CREATE TABLE IF NOT EXISTS` by the module that owns
it, so a fresh database and an existing one converge on the same schema. Changes
that cannot be expressed that way (`ALTER TABLE`, backfills, data fixes) live in
`migrations.py` instead, as an ordered list tracked by SQLite's own
`PRAGMA user_version`.

```bash
sqlite3 bot.db 'PRAGMA user_version'         # current version, 0 = never migrated
sqlite3 bot.db 'SELECT * FROM schema_migrations'
```

- `migrate()` runs at startup, before the stores are opened, and applies only
  what the database has not seen yet.
- Each step commits together with its version bump and its `schema_migrations`
  row. A failure rolls the step back — including its `ALTER` — and leaves the
  counter untouched, so a crash mid-upgrade can simply be retried.
- A database written by a **newer** release is refused at startup instead of
  being served by older code. `health_check.check_database()` reports the same
  state (`schema v2/3 (current)`) and flags that case as unhealthy.
- Downgrading is not supported: restore the `bot.db` backup you took before the
  upgrade, or upgrade the code again.

To add a migration, append one entry with the next version number and an
`apply(connection)` that is safe on both a fresh and an old database. Never edit
or reorder an existing entry: databases in the field have already run it.

```bash
cp bot.db bot.db.bak      # take a copy before upgrading a production instance
```

## Secrets

Never commit:

- `.env`
- Telegram bot tokens
- Gemini or OpenAI-compatible API keys
- You.com and Pollinations.ai API keys
- `bot.db`
- logs containing API URLs or credentials

`.gitignore` covers all of them, and `repo_guard.py` refuses a commit that gets
around it with `git add -f`; CI runs the same check. If a credential is ever
exposed, revoke and rotate it immediately.

Provider keys entered through `/admin` are stored encrypted at rest with
`cryptography`, using `AURA_SECRET_KEY` or a key generated into
`.aura_secret_key` beside the database. They are never returned to the caller —
only a mask such as `sk-a…9f3 (51 chars)`.

For reporting a vulnerability, use the private process in
[SECURITY.md](../SECURITY.md) rather than a public issue.
