# Deployment

Aura is a single Python process using Telegram long polling. There is no webhook
to register, no public port to open and no TLS certificate to manage — the bot
dials out to Telegram, not the other way round.

## The minimum

```bash
python bot.py
```

Keep `.env` and `bot.db` on the server and deploy only the application files and
dependencies. Nothing server-specific belongs in the repository.

If your provider is a hosted service, that is the only outbound requirement:

| Destination | Needed for |
| --- | --- |
| `api.telegram.org` | always |
| `generativelanguage.googleapis.com` or your gateway | always |
| `api.you.com` | web search (optional) |
| `geocoding-api.open-meteo.com`, `api.open-meteo.com` | weather (optional, keyless) |
| `meme-api.com` and its mirror | memes (optional, keyless) |
| `image.pollinations.ai` | image generation (optional) |
| Reddit/YouTube URLs | memes and YouTube analysis (optional) |

## Running as a service

Two portable scripts ship with the repository. Both resolve their paths from
their own location, so a checkout works anywhere:

```bash
./aura_service.sh start        # also: stop, restart, status
AURA_DIR=/srv/aura ./aura_watchdog.sh    # from cron, e.g. every minute
```

| Variable | Purpose | Default |
| --- | --- | --- |
| `AURA_DIR` | project directory | directory of the script |
| `AURA_PYTHON` | interpreter to run | `.venv/bin/python`, else `python3` |
| `AURA_PIDFILE` | PID file | `$AURA_DIR/aura.pid` |
| `AURA_LOGFILE` | service log | `$AURA_DIR/aura.log` |
| `AURA_SERVICE` | service script the watchdog controls | `$AURA_DIR/aura_service.sh` |

`aura_service.sh status` exits with `3` when Aura is stopped, so monitoring and
`set -e` scripts can rely on it.

For a real init system, prefer a systemd unit over the shell wrapper:

```ini
[Unit]
Description=Aura Telegram assistant
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/srv/aura
EnvironmentFile=/srv/aura/.env
ExecStart=/srv/aura/.venv/bin/python /srv/aura/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## cPanel and other shared hosts

Aura runs on shared hosting because it needs nothing but Python and a writable
directory. With cPanel's Python app manager, point the application at the
checkout and run `python bot.py` as the startup command, then restart the app
after deploying. `passenger_wsgi.py` is included for hosts that insist on a WSGI
entry point; it answers a plain "It works!" page and does not start the bot.

On a host that runs the bot for you, deploy the files, restart the app, and
watch `aura.log` for the first line after startup.

## Concurrency

`MAX_CONCURRENT_CHATS` (default 4, clamped to 1–32) bounds concurrent Telegram
updates. Commands and messages in the same chat are processed in order, while
different chats progress independently. Paid web calls are serialized per
service so the daily quota checks stay safe.

Run **one** long-polling instance against one database. Telegram will not deliver
the same update to two pollers, but two processes sharing `bot.db` will still
fight over the reminder worker and the SQLite write lock.

## Health checks

`health_check.py` exposes read-only local probes that are safe to run from cron
or a monitoring script:

```python
from health_check import check_database, check_process_identity, overall_health

results = [check_process_identity("/srv/aura/bot.py"), check_database("/srv/aura/bot.db")]
print(overall_health(results))   # True only if every probe passed
```

`check_database` runs `PRAGMA quick_check` on a read-only connection and reports
the schema version. A database written by a newer release is reported as
unhealthy rather than "current", because the application will refuse to start
against it.

## Upgrades

```bash
cp bot.db bot.db.bak      # always
git pull
source .venv/bin/activate && pip install -r requirements.txt
./aura_service.sh restart
```

Migrations run at startup and are forward-only; see
[Data and privacy](data-and-privacy.md#schema-versions-and-migrations). A
database from a newer release is refused instead of being served by older code,
so downgrading means restoring the backup you just took.

`requirements-pinned.txt` pins the exact versions a release was validated
against — use it when you want a reproducible install instead of the ranges in
`requirements.txt`.

## Logs

Aura logs to stdout, so where the output goes depends on the runner: the service
script writes `aura.log`, systemd writes the journal, cPanel writes its own file.

Log lines carry chat and message IDs, tool names, byte counts, latencies and
search queries — enough to explain a slow or refused turn — but never provider
keys and never message bodies. A trace is started per update and reports its own
latency, so a slow turn is visible without dumping what the user wrote.
