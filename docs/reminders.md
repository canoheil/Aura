# Reminders

Reminders are **one-shot**, saved in `bot.db`, and scoped to the requesting user
and chat. The default timezone is `Asia/Tehran`; each user can choose another
IANA timezone, and existing reminders keep their original UTC instant when the
timezone changes.

```text
۱۰ دقیقه دیگه یادم بنداز آب بخورم
یک ساعت و نیم دیگه یادم بنداز چای دم کنم
یادم بنداز فردا ساعت ۹ صبح قرص بخورم
یادم بنداز ساعت ۹ شب به بابا زنگ بزنم
یادم بنداز دوشنبه ۱۷:۰۰ جلسه دارم
یادم بنداز ۲۸ آبان ساعت ۹ صبح تولد سارا
/remind in 10m آب بخورم
/remind 2030-11-01 09:30 جلسه
/reminders
/cancelreminder 12
/timezone Europe/Berlin
```

The parser accepts the four ways people name a time:

- **A duration** — `۱۰ دقیقه دیگه`, `نیم ساعت`, `یه ربع`, `دو ساعت و سی دقیقه`.
  Written numbers, `دقیقع`, and compound durations joined with `و` all work.
- **A day and a clock** — `فردا ساعت ۹`, `دوشنبه ۱۷:۰۰`, `۲۸ آبان ساعت ۹ صبح`.
  Stated in either order: `ساعت ۹ صبح فردا` is the same request.
- **A clock alone** — `ساعت ۹ شب`, `۱۵:۳۰`, `at 9 pm`. This means the next time
  that clock is true, today or tomorrow, which is also what it means in speech.
- **A calendar date** — Gregorian `2026-11-01 09:30` (24-hour, optional UTC
  offset) or Solar Hijri `۲۸ آبان` / `۲۸ آبان ۱۴۰۶` (the current Hijri year when
  the year is omitted).

Persian clock words map to real hours rather than to labels: `۹ صبح` is 09:00,
`۹ شب` is 21:00, `۱۲ شب` is midnight of the next day, and `۱۲ ظهر` is noon.
Weekday names mean the next occurrence — a weekday whose time has already passed
this week rolls forward seven days.

## When it is not sure

Parsing is conservative: if the time or the task is unclear, Aura asks instead of
claiming a reminder was created. The question is specific — a request such as
`فردا صبح` is recognised as a day without a clock, and Aura asks *at what time*
instead of asking for the whole request again.

A short follow-up completes the same request, and the two halves are joined the
way a person would say them:

```text
You:  یادم بنداز فردا صبح زنگ بزنم به مامان
Aura: چه ساعتی؟ مثلاً «۹ صبح» یا «۱۷:۳۰». هنوز ثبتش نکردم.
You:  ۹ صبح
Aura: باشه — چهارشنبه ۱۳ آبان ۱۴۰۵، ساعت ۹:۰۰ صبح یادت می‌ندازم.
```

These incomplete conversations:

- are scoped to the same user and the same chat,
- expire after ten minutes,
- are cleared by `/new` or a restart,
- are *not* registered reminders.

Two explicit times in one message (`فردا ۹:۰۰ و ۱۴:۰۰`), an approximate
duration (`تقریباً پنج دقیقه`), or an alternative (`پنج یا ده دقیقه`) are refused
rather than resolved by picking one.

After a change of topic, repeat the complete request rather than relying on an
ambiguous follow-up. A short `لغوش کن` can cancel the most recently referenced
reminder in that same user/chat context; when the target is unclear, use its ID
with `/cancelreminder`.

## The reminder tool

Aura also has a real reminder tool for less formulaic requests. It must quote the
user's own task and time, and the application validates them and commits to
SQLite **before** reporting success. `/reminders` is the authoritative status.

### When the grammar cannot read the time

Some phrasings are outside the parser: `سه هفته دیگه`, `نیمهٔ ماه`, a regional
expression. For a message that names time but that the grammar cannot resolve,
Aura asks a model for a reading — and then checks it, because a model's
confidence is not evidence:

1. The quoted time must be a **verbatim fragment** of the user's own message.
2. The application tries its own grammar on that quote first. If it succeeds,
   **the quote wins** and the model's arithmetic is discarded.
3. Only for a quote the grammar cannot read does the model's `local_datetime`
   matter — and then every reading inside the quote must corroborate it: a
   written hour, a spoken hour, صبح/عصر/شب, spoken minutes, a weekday, a Hijri
   or Gregorian date, or a quantity with a period.
4. A quote with **nothing checkable** in it is refused. `هفته دیگه` has no
   anchor, so it can be neither confirmed nor denied; Aura explains that the
   time is too vague to pin down instead of choosing a day on the user's behalf.
5. The instant must still be in the future, inside the five-year bound, and a
   valid local time in the user's zone.

A rejected proposal produces a question, never a reminder. If the model is
unreachable or slow, the parser's answer stands, which is the same behaviour as
before this feature existed.

Every accepted proposal is logged with the phrase it came from, so a surprising
reminder can be traced to the words that caused it.

- Repeating schedules are not supported.
- Nonexistent or repeated local times at daylight-saving transitions are
  rejected; an explicit UTC offset, e.g. `2030-10-27 02:30+02:00`, disambiguates
  them.
- At most 100 pending reminders are allowed per user.

## After it fires

The delivery message carries the two decisions that are actually available in
that moment:

```text
⏰ یادت نره: قرص بخورم
[ ⏰ ۱۰ دقیقه بعد ]  [ ✅ انجام شد ]
```

- **⏰ ۱۰ دقیقه بعد** requeues the same reminder — same text, same row — for ten
  minutes later. The label carries the actual delay, so raising
  `REMINDER_SNOOZE_MINUTES` changes both. It can be pressed repeatedly, and is
  the honest answer to "not now, but soon".
- **✅ انجام شد** closes it. A finished reminder cannot be delivered again and
  drops out of `/reminders`.

The confirmation, while the reminder is still pending, carries the third useful
decision instead — **✖️ لغو این یادآور** — because a typo is cheap to fix until
the thing actually fires.

A button only ever names a row and an action (`rm:done:12`); the person and the
chat come from the Telegram update, and the row is looked up scoped to both. In a
group that means the reminder's owner can press it and nobody else can: anyone
else gets a private "that reminder isn't yours" and nothing changes. Pressing a
button also makes that reminder the subject of the next message, so a follow-up
«ثبت شده؟» or «لغوش کن» refers to it.

After a press, the outcome is appended to the message and the keyboard is
removed, so the same decision cannot be made twice. Edits fail after two days;
when that happens the action still counts and the toast still reports it.

## Delivery

After a successful commit, the model can phrase a short confirmation using
Aura's normal personality and verified identity context — but the **instant is
always printed by the application**, in the user's own calendar and numerals
(`چهارشنبه ۱۳ آبان ۱۴۰۵، ساعت ۹:۰۰ صبح`). A confirmation is the moment when a
misunderstanding is still cheap to fix, so it must say when.

At the due time the model can generate a brief, task-specific reminder.
Deliveries do not automatically add the timestamp, timezone or ID; those remain
available through `/reminders`. Both are extra model calls.

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
| `REMINDER_SNOOZE_MINUTES` | `10` | How far the snooze button pushes a delivered reminder |
