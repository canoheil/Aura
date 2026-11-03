# Reminders

Reminders are **one-shot**, saved in `bot.db`, and scoped to the requesting user
and chat. The default timezone is `Asia/Tehran`; each user can choose another
IANA timezone, and existing reminders keep their original UTC instant when the
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
`دقیقع`) are supported.

## When it is not sure

Parsing is conservative: if the time or the task is unclear, Aura asks instead of
claiming a reminder was created.

If only the time is missing, a short follow-up such as `دو دقیقه دیگه` completes
the same request. These incomplete conversations:

- are scoped to the same user and the same chat,
- expire after ten minutes,
- are cleared by `/new` or a restart,
- are *not* registered reminders.

After a change of topic, repeat the complete request rather than relying on an
ambiguous follow-up. A short `لغوش کن` can cancel the most recently referenced
reminder in that same user/chat context; when the target is unclear, use its ID
with `/cancelreminder`.

## The reminder tool

Aura also has a real reminder tool for less formulaic requests. It must quote the
user's own task and time, and the application validates them and commits to
SQLite **before** reporting success. `/reminders` is the authoritative status.

- Repeating schedules are not supported.
- Nonexistent or repeated local times at daylight-saving transitions are
  rejected; an explicit UTC offset, e.g. `2030-10-27 02:30+02:00`, disambiguates
  them.
- At most 100 pending reminders are allowed per user.

## Delivery

After a successful commit, the model can phrase a short confirmation using
Aura's normal personality and verified identity context. At the due time it can
also generate a brief, task-specific reminder. Confirmations and deliveries do
not automatically add the timestamp, timezone, ID or a second copy of the task;
those details remain available through `/reminders`. Both are extra model calls.

A conservative wording check rejects obvious changes or omissions of task
details; it is not a semantic guarantee. If generation fails, times out, or does
not pass that check, Aura uses a short fallback with the saved task so the
reminder can still be delivered.

In an allowed group, the application creates a Telegram text mention from the
stored requester ID and an escaped display-name snapshot — never an ID chosen by
the model. If Telegram rejects the mention, it tries a plain-text delivery
instead. Older reminders are migrated automatically and use a generic label when
no display name was stored.

## Reliability

A background worker resumes overdue reminders when the bot starts. It retries
transient Telegram failures; permanent failures appear in `/reminders`, where
they can be cancelled.

Delivery requires the bot to be running and able to send to the chat. Run **one**
long-polling Aura instance against a database. A crash after Telegram accepts a
message but before SQLite records success can cause a duplicate on restart, and
cancellation cannot recall a send already in progress.

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `REMINDER_DEFAULT_TIMEZONE` | `Asia/Tehran` | Timezone for users who never chose one |
| `REMINDER_POLL_SECONDS` | `3` | How often the worker checks for due reminders |
