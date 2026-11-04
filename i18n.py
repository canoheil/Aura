"""Interface localization for Aura.

Two languages ship today: Persian (``fa``, the original voice) and English
(``en``). The module is deliberately small and dependency-free:

* ``CATALOG`` maps a stable key to one message per language.
* ``t(key, lang, **values)`` renders a message, substituting only the named
  placeholders it was given, so a stray brace in a translation can never raise.
* ``LanguageStore`` remembers a per-user choice in SQLite; ``resolve_language``
  applies precedence: explicit argument > user preference > deployment default.

Persian stays the default. A deployment that never touches the language setting
behaves exactly as before, which is also why the existing Persian regression
tests still assert the same strings.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from typing import Optional

logger = logging.getLogger("aura-i18n")

LANGUAGES = ("fa", "en")
DEFAULT_LANGUAGE = "fa"
LANGUAGE_NAMES = {"fa": "فارسی", "en": "English"}

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def normalize_language(value: Optional[str], *, fallback: str = DEFAULT_LANGUAGE) -> str:
    """Accept ``en``, ``EN``, ``en-US``, ``English`` and map to a known code."""
    text = str(value or "").strip().lower()
    if not text:
        return fallback
    for code in LANGUAGES:
        if text == code or text.startswith(code + "-") or text.startswith(code + "_"):
            return code
    for code, name in LANGUAGE_NAMES.items():
        if text == name.lower():
            return code
    if text.startswith("english"):
        return "en"
    if text.startswith("persian") or text.startswith("farsi") or text.startswith("فارسی"):
        return "fa"
    return fallback


def is_language_name(text: str) -> bool:
    return normalize_language(text, fallback="") in LANGUAGES


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
# Keys are grouped by area. `{name}` placeholders are filled by `t()`.
CATALOG: dict[str, dict[str, str]] = {
    # --- /start ------------------------------------------------------------
    "start.body": {
        "fa": (
            "سلام 👋\n\n"
            "من {name} هستم؛ دستیار هوش مصنوعی شخصی تو.\n\n"
            "مدل فعلی: {model}\n\n"
            "می‌تونی همین‌جا باهام گفتگو کنی، سؤال بپرسی، لینک عمومی یوتیوب بدی تا ویدیو رو ببینم و تحلیل کنم، "
            "از وب اطلاعات به‌روز بگیری و چیزهایی رو که می‌خوای به خاطر بسپارم.\n\n"
            "دستورات:\n"
            "/new — شروع گفتگوی جدید\n"
            "/model — نمایش مدل فعلی\n"
            "/model <name> — تغییر مدل Gemini\n"
            "/models — نمایش مدل‌های قابل دسترسی\n"
            "/stats — آمار گفتگو و حافظه\n"
            "/memory — نمایش حافظه بلندمدت\n"
            "/remember <text> — ذخیره در حافظه\n"
            "/forget <text> — حذف از حافظه\n"
            "/image <prompt> — ساخت عکس (یا ادیت عکسی که روی آن ریپلای می‌کنی)\n"
            "/remind <زمان> <متن> — ثبت یادآور (یک‌باره یا تکراری)\n"
            "/reminders — فهرست یادآورها\n"
            "/cancelreminder <id> — لغو یادآور\n"
            "/timezone [Asia/Tehran] — نمایش یا تغییر منطقهٔ زمانی یادآور\n"
            "/language — تغییر زبان\n"
            "/deletemydata — دیدن و پاک کردن همهٔ داده‌های شخصی\n"
            "/usage — مصرف امروز و سهمیهٔ باقی‌مانده\n"
            "/secretary — حالت منشی برای حساب کاری (فقط مدیر)\n"
            "/whoami — نمایش Telegram ID و وضعیت سازنده"
        ),
        "en": (
            "Hi 👋\n\n"
            "I'm {name}, your personal AI assistant.\n\n"
            "Current model: {model}\n\n"
            "You can talk to me here, ask questions, send a public YouTube link for me to watch and analyze, "
            "pull fresh information from the web, or ask me to remember things.\n\n"
            "Commands:\n"
            "/new — start a fresh conversation\n"
            "/model — show the current model\n"
            "/model <name> — change the Gemini model\n"
            "/models — list available models\n"
            "/stats — conversation and memory stats\n"
            "/memory — show long-term memory\n"
            "/remember <text> — save to memory\n"
            "/forget <text> — remove from memory\n"
            "/image <prompt> — generate an image (or edit the photo you reply to)\n"
            "/remind <time> <text> — schedule a one-off reminder\n"
            "/reminders — list your reminders\n"
            "/cancelreminder <id> — cancel a reminder\n"
            "/timezone [Europe/Berlin] — show or change the reminder timezone\n"
            "/language — change the language\n"
            "/deletemydata — see and delete everything stored about you\n"
            "/usage — today's usage and remaining allowance\n"
            "/secretary — secretary mode for a business account (admins)\n"
            "/whoami — show your Telegram ID and founder status"
        ),
    },
    "new.done": {
        "fa": "🧹 تاریخچه مکالمه پاک شد. حافظه بلندمدت دست‌نخورده باقی ماند.",
        "en": "🧹 Conversation history cleared. Long-term memory is untouched.",
    },
    # --- /whoami -----------------------------------------------------------
    "whoami.founder": {
        "fa": "تأیید شد؛ Aura تو را به‌عنوان سازنده و صاحب اصلی خودش می‌شناسد.",
        "en": "Verified: Aura recognizes you as her founder and primary owner.",
    },
    "whoami.not_founder": {
        "fa": "این حساب به‌عنوان سازنده ثبت نشده است.",
        "en": "This account is not registered as the founder.",
    },
    "whoami.body": {"fa": "🆔 Telegram ID: {id}\n{status}", "en": "🆔 Telegram ID: {id}\n{status}"},
    # --- /language ---------------------------------------------------------
    "language.current": {
        "fa": "🌍 زبان فعلی: {name}\n\nتغییر:\n/language en\n/language fa",
        "en": "🌍 Current language: {name}\n\nChange it with:\n/language en\n/language fa",
    },
    "language.invalid": {
        "fa": "زبان پشتیبانی نمی‌شود. گزینه‌ها: /language fa یا /language en",
        "en": "Unsupported language. Use /language en or /language fa.",
    },
    "language.changed": {
        "fa": "✅ زبان به {name} تغییر کرد.",
        "en": "✅ Language changed to {name}.",
    },
    # --- /model ------------------------------------------------------------
    "model.current": {
        "fa": "🤖 Provider: {provider}\nمدل فعلی:\n{model}\n\n{hint}",
        "en": "🤖 Provider: {provider}\nCurrent model:\n{model}\n\n{hint}",
    },
    "model.hint.gemini": {
        "fa": "برای تغییر:\n/model model-name",
        "en": "To change it:\n/model model-name",
    },
    "model.hint.openai": {
        "fa": "این provider مدل را برای همهٔ چت‌ها تنظیم می‌کند:\n/admin set openai_model <name>",
        "en": "With this provider the model is deployment-wide:\n/admin set openai_model <name>",
    },
    "model.usage": {"fa": "استفاده صحیح:\n/model model-name", "en": "Usage:\n/model model-name"},
    "model.empty": {"fa": "نام مدل خالی است.", "en": "The model name is empty."},
    "model.admin_only": {
        "fa": "تغییر مدل در این حالت فقط توسط مدیر بات انجام می‌شود.",
        "en": "With this provider only a bot admin can change the model.",
    },
    "model.changed": {"fa": "✅ مدل تغییر کرد به:\n{model}", "en": "✅ Model changed to:\n{model}"},
    "model.checking": {"fa": "🔎 دارم دسترسی مدل رو بررسی می‌کنم...", "en": "🔎 Checking access to that model…"},
    "model.not_found": {
        "fa": "❌ این مدل در لیست مدل‌های قابل دسترسی API Key شما پیدا نشد.",
        "en": "❌ That model is not available to your API key.",
    },
    "models.fetching": {"fa": "🔎 در حال دریافت مدل‌های قابل دسترسی...", "en": "🔎 Fetching available models…"},
    "models.empty": {"fa": "هیچ مدلی از API دریافت نشد.", "en": "The API returned no models."},
    "models.header": {"fa": "🤖 مدل‌های قابل دسترسی: {count}", "en": "🤖 Available models: {count}"},
    # --- memory ------------------------------------------------------------
    "memory.empty": {"fa": "🧠 هنوز حافظه بلندمدتی ذخیره نشده.", "en": "🧠 Nothing is stored in long-term memory yet."},
    "memory.header": {"fa": "🧠 حافظه بلندمدت Aura", "en": "🧠 Aura's long-term memory"},
    "remember.usage": {
        "fa": "استفاده: /remember چیزی که می‌خواهی یادم بماند",
        "en": "Usage: /remember something you want me to keep",
    },
    "remember.saved": {
        "fa": "🧠 ذخیره شد. این مورد جزو حافظه بلندمدت Aura است.",
        "en": "🧠 Saved to Aura's long-term memory.",
    },
    "forget.usage": {
        "fa": "استفاده: /forget بخشی از موردی که می‌خواهی فراموش کنم",
        "en": "Usage: /forget part of the memory you want removed",
    },
    "forget.removed": {
        "fa": "🧹 {count} مورد از حافظه بلندمدت حذف شد.",
        "en": "🧹 Removed {count} item(s) from long-term memory.",
    },
    "forget.not_found": {
        "fa": "چیزی مطابق این عبارت در حافظه بلندمدت پیدا نکردم.",
        "en": "Nothing in long-term memory matches that.",
    },
    # --- /stats ------------------------------------------------------------
    "stats.body": {
        "fa": (
            "📊 آمار گفتگو\n\n"
            "مدل: {model}\n"
            "تعداد پیام‌های ذخیره‌شده: {messages}\n"
            "پیام‌های اخیر در context: {recent} | آستانه فشرده‌سازی: {threshold}\n"
            "جستجوی وب امروز: {search_used}/{search_limit}\n"
            "تعداد حافظه‌های بلندمدت: {memories}"
        ),
        "en": (
            "📊 Conversation stats\n\n"
            "Model: {model}\n"
            "Stored messages: {messages}\n"
            "Recent messages in context: {recent} | compaction threshold: {threshold}\n"
            "Web searches today: {search_used}/{search_limit}\n"
            "Long-term memories: {memories}"
        ),
    },
    # --- /settings (access control) ---------------------------------------
    "settings.denied": {"fa": "Access denied.", "en": "Access denied."},
    "settings.usage": {
        "fa": "فرمت درست:\n/settings add <chat_id>\n/settings remove <chat_id>",
        "en": "Usage:\n/settings add <chat_id>\n/settings remove <chat_id>",
    },
    "settings.chat_id_invalid": {
        "fa": "Chat ID باید یک عدد باشد (مثلاً -1001234567890).",
        "en": "The Chat ID must be a number (for example -1001234567890).",
    },
    "settings.group_added": {
        "fa": "✅ گروه به لیست مجاز اضافه شد:\n{group}",
        "en": "✅ Group added to the allowlist:\n{group}",
    },
    "settings.group_removed": {
        "fa": "🗑 گروه از لیست مجاز حذف شد: {chat_id}",
        "en": "🗑 Group removed from the allowlist: {chat_id}",
    },
    "settings.group_not_found": {
        "fa": "این Chat ID در لیست مجاز نبود: {chat_id}",
        "en": "That Chat ID was not in the allowlist: {chat_id}",
    },
    "settings.title": {"fa": "⚙️ تنظیمات کنترل دسترسی", "en": "⚙️ Access-control settings"},
    "settings.status": {"fa": "وضعیت: فعال", "en": "Status: enabled"},
    "settings.private": {"fa": "• چت خصوصی: برای همه آزاد", "en": "• Private chats: open to everyone"},
    "settings.groups": {
        "fa": "• گروه‌ها: فقط گروه‌های داخل لیست مجاز (بقیه سکوت کامل)",
        "en": "• Groups: only allowlisted groups (everything else gets silence)",
    },
    "settings.group_count": {"fa": "• تعداد گروه‌های مجاز: {count}", "en": "• Allowlisted groups: {count}"},
    "settings.allowed_header": {"fa": "گروه‌های مجاز:", "en": "Allowlisted groups:"},
    "settings.untitled": {"fa": "بدون عنوان", "en": "untitled"},
    "settings.commands": {"fa": "دستورها:", "en": "Commands:"},
    "settings.cmd_show": {"fa": "/settings — نمایش همین وضعیت و لیست", "en": "/settings — show this status and list"},
    "settings.cmd_add": {"fa": "/settings add <chat_id> — افزودن گروه", "en": "/settings add <chat_id> — add a group"},
    "settings.cmd_remove": {"fa": "/settings remove <chat_id> — حذف گروه", "en": "/settings remove <chat_id> — remove a group"},
    # --- reminders ---------------------------------------------------------
    # --- secretary mode (Telegram Business) ---------------------------------
    "secretary.status.connected": {
        "fa": "📇 حالت منشی روشن شد؛ از این به بعد پیام‌های حساب کاری‌ات را جواب می‌دهم. "
              "برای خاموش کردن: /secretary off",
        "en": "📇 Secretary mode is on; I'll answer the messages that come to your business "
              "account. To pause me: /secretary off",
    },
    "secretary.status.disconnected": {
        "fa": "📇 اتصال حالت منشی قطع شد؛ دیگر به پیام‌های حساب کاری جواب نمی‌دهم.",
        "en": "📇 The secretary-mode connection is gone; I'll no longer answer that business account.",
    },
    "secretary.denied": {
        "fa": "این دستور مال سازنده یا مدیرهای ربات است.",
        "en": "Only the founder or an admin can use this command.",
    },
    "secretary.usage": {
        "fa": "استفاده: /secretary برای وضعیت · /secretary off برای توقف · "
              "/secretary on برای شروع دوباره · /secretary forget برای پاک کردن گفتگوهای مشتریان",
        "en": "Usage: /secretary for status · /secretary off to pause · /secretary on to resume · "
              "/secretary forget to erase the stored customer conversations",
    },
    "secretary.status.header": {
        "fa": "📇 حالت منشی — {enabled} اتصال فعال، {paused}",
        "en": "📇 Secretary mode — {enabled} active connection(s), {paused}",
    },
    "secretary.status.paused_yes": {"fa": "موقتاً متوقف", "en": "paused"},
    "secretary.status.paused_no": {"fa": "در حال کار", "en": "running"},
    "secretary.status.no_connections": {
        "fa": "هیچ اتصالی ثبت نشده؛ در BotFather بخش Business Mode را روشن کن و دوباره وصل شو.",
        "en": "No connection is stored yet; turn on Business Mode in BotFather and reconnect.",
    },
    "secretary.status.active": {"fa": "فعال", "en": "active"},
    "secretary.status.inactive": {"fa": "غیرفعال", "en": "inactive"},
    "secretary.status.rights_ok": {"fa": "اجازهٔ پاسخ دارد", "en": "can reply"},
    "secretary.status.rights_missing": {
        "fa": "اجازهٔ پاسخ ندارد؛ در تنظیمات Business Mode فعالش کن",
        "en": "cannot reply; enable replying in Business Mode",
    },
    "secretary.status.paused_on": {
        "fa": "⏸ حالت منشی متوقف شد؛ تا /secretary on هیچ پیام مشتری‌ای را جواب نمی‌دهم.",
        "en": "⏸ Secretary mode paused; until /secretary on I won't answer any customer.",
    },
    "secretary.status.paused_off": {
        "fa": "▶️ حالت منشی دوباره روشن شد.",
        "en": "▶️ Secretary mode is running again.",
    },
    "secretary.status.forgotten": {
        "fa": "🧹 {count} ردیف از گفتگوهای مشتریان پاک شد.",
        "en": "🧹 Erased {count} stored row(s) from the customer conversations.",
    },
    "secretary.reply.media": {
        "fa": "این نوع فایل را نمی‌توانم ببینم؛ اگر متنش را بنویسی، همان‌جا جواب می‌دهم.",
        "en": "I can't open this kind of file — write it out and I'll answer right here.",
    },
    "remind.usage": {
        "fa": (
            "زمان و متن یادآوری را بفرست؛ مثلاً:\n"
            "یادم بنداز فردا ساعت ۹ صبح قرص بخورم\n"
            "یادم بنداز یک ساعت و نیم دیگه چای دم کنم\n"
            "یادم بنداز هر روز ساعت ۸ صبح قرص بخورم\n"
            "/remind in 10m آب بخور — /remind 2026-11-01 09:30 جلسه\n"
            "منطقهٔ زمانی: /timezone"
        ),
        "en": (
            "Send a time and the reminder text, for example:\n"
            "/remind in 10m drink water\n/remind 2026-11-01 09:30 meeting\n"
            "Every day at 8am, remind me to take the pills\n"
            "Dates are Gregorian. Timezone: /timezone"
        ),
    },
    "reminders.none": {"fa": "در این گفتگو یادآور فعالی نداری.", "en": "You have no active reminders in this chat."},
    "reminders.header": {
        "fa": "⏰ یادآورهای این گفتگو ({zone}):",
        "en": "⏰ Reminders in this chat ({zone}):",
    },
    # --- reminder buttons ---------------------------------------------------
    "reminder.button.snooze": {"fa": "⏰ {minutes} دقیقه بعد", "en": "⏰ In {minutes} min"},
    "reminder.button.done": {"fa": "✅ انجام شد", "en": "✅ Done"},
    "reminder.button.cancel": {"fa": "✖️ لغو این یادآور", "en": "✖️ Cancel this reminder"},
    "reminder.callback.not_yours": {
        "fa": "این یادآور مال تو نیست.",
        "en": "That reminder isn't yours.",
    },
    "reminder.callback.gone": {
        "fa": "این یادآور دیگر فعال نیست؛ کاری لازم نیست.",
        "en": "That reminder is no longer active; nothing to do.",
    },
    "reminder.callback.done": {"fa": "انجامش دادی؛ ثبت شد.", "en": "Marked as done."},
    "reminder.button.skip": {"fa": "🔂 این یکی رو رد کن", "en": "🔂 Skip the next one"},
    "reminder.button.stop_repeating": {"fa": "⏹ تکرار رو متوقف کن", "en": "⏹ Stop repeating"},
    "reminder.callback.skipped": {
        "fa": "باشه؛ تکرار بعدی {when}.",
        "en": "Fine — the next one is {when}.",
    },
    "reminder.callback.repeat_stopped": {
        "fa": "تکرارش رو متوقف کردم؛ این یادآور دیگر نمی‌آید.",
        "en": "Stopped repeating; this reminder won't come back.",
    },
    "reminder.callback.skip_done": {
        "fa": "این یکی دیگر تکرار نمی‌شود؛ تکرار بعدی {when}.",
        "en": "Not this one; the next repeat is {when}.",
    },
    "reminder.callback.not_repeating": {
        "fa": "این یک یادآور یک‌باره است؛ تکرار ندارد.",
        "en": "This reminder doesn't repeat.",
    },
    "reminder.callback.snoozed": {
        "fa": "باشه، {when} دوباره یادت می‌ندازم.",
        "en": "Okay — I'll remind you again {when}.",
    },
    "reminder.callback.cancelled": {"fa": "لغو شد.", "en": "Cancelled."},
    "reminder.callback.failed": {
        "fa": "نتونستم انجامش بدم؛ لطفاً دوباره امتحان کن.",
        "en": "I couldn't do that; please try again.",
    },
    "reminder.delivery.done_mark": {"fa": "✅ انجام شد", "en": "✅ Done"},
    "reminder.delivery.cancelled_mark": {"fa": "✖️ لغو شد", "en": "✖️ Cancelled"},
    "reminder.delivery.repeat_stopped_mark": {
        "fa": "⏹ تکرار متوقف شد",
        "en": "⏹ Stopped repeating",
    },
    "reminder.delivery.skipped_mark": {
        "fa": "🔂 تکرار بعدی: {when}",
        "en": "🔂 Next repeat: {when}",
    },
    "reminder.delivery.snoozed_mark": {
        "fa": "⏰ برای {when} دوباره تنظیم شد",
        "en": "⏰ Moved to {when}",
    },
    "reminders.status.failed": {
        "fa": "ارسال ناموفق؛ دوباره ثبت کن یا لغوش کن",
        "en": "delivery failed; re-create or cancel it",
    },
    "reminders.status.sending": {"fa": "در حال ارسال", "en": "sending"},
    "reminders.status.retry": {"fa": "در انتظار تلاش مجدد", "en": "waiting to retry"},
    "reminders.status.active": {"fa": "فعال", "en": "active"},
    "cancelreminder.usage": {
        "fa": "شمارهٔ یادآور را بفرست؛ مثلاً /cancelreminder 12",
        "en": "Send the reminder number, for example /cancelreminder 12",
    },
    "cancelreminder.cancelled": {"fa": "یادآور #{id} لغو شد.", "en": "Reminder #{id} cancelled."},
    "cancelreminder.not_found": {
        "fa": "یادآور فعالی با این شماره در این گفتگو پیدا نشد یا ارسالش شروع شده است.",
        "en": "No active reminder with that number was found here, or delivery has already started.",
    },
    "timezone.current": {
        "fa": "منطقهٔ زمانی یادآورها: {zone}\nتغییر: /timezone Europe/Berlin",
        "en": "Reminder timezone: {zone}\nChange it with: /timezone Europe/Berlin",
    },
    "timezone.usage": {
        "fa": "یک نام منطقهٔ زمانی بفرست؛ مثلاً /timezone Asia/Tehran",
        "en": "Send one timezone name, for example /timezone Europe/Berlin",
    },
    "timezone.updated": {
        "fa": "منطقهٔ زمانی یادآورهای جدید: {zone}\nزمان یادآورهای قبلی تغییر نمی‌کند.",
        "en": "New reminder timezone: {zone}\nExisting reminders keep their original time.",
    },
    # Reminder errors, translated by stable code instead of parser text.
    "reminder.err.ambiguous_time": {
        "fa": "زمان دقیق را مشخص کن؛ هنوز چیزی ثبت نکردم.",
        "en": "Please give an exact time; nothing has been scheduled.",
    },
    "reminder.err.missing_time": {
        "fa": "کی یادآوری کنم؟ هنوز چیزی ثبت نکردم.",
        "en": "When should I remind you? Nothing has been scheduled.",
    },
    "reminder.err.missing_clock": {
        "fa": "چه ساعتی؟ مثلاً «۹ صبح» یا «۱۷:۳۰». هنوز چیزی ثبت نکردم.",
        "en": "At what time? For example \"9 am\" or \"17:30\". Nothing has been scheduled.",
    },
    "reminder.err.missing_task": {
        "fa": "چی رو یادآوری کنم؟ هنوز چیزی ثبت نکردم.",
        "en": "What should I remind you about? Nothing has been scheduled.",
    },
    "reminder.err.missing_intent": {
        "fa": "درخواست یادآوری مشخص نیست؛ چیزی ثبت نکردم.",
        "en": "That reminder request is not clear; nothing has been scheduled.",
    },
    "reminder.err.recurring_yearly": {
        "fa": "یادآور سالانه را پشتیبانی نمی‌کنم؛ روزانه، هفتگی یا ماهانه را می‌توانم ثبت کنم.",
        "en": "I can't schedule a yearly reminder; I can do daily, weekly or monthly.",
    },
    "reminder.err.recurring_time": {
        "fa": "برای یادآور تکراری باید ساعت را بگویی، نه «چند دقیقه دیگه»؛ مثلاً «هر روز ساعت ۸ صبح».",
        "en": "A repeating reminder needs a clock, not \"in ten minutes\" — for example "
              "\"every day at 8am\".",
    },
    "reminder.err.recurring_date": {
        "fa": "برای یادآور تکراری یک ساعت کافی است، نه تاریخ؛ مثلاً «هر روز ساعت ۹ صبح».",
        "en": "A repeating reminder needs a clock rather than a date — for example "
              "\"every day at 9am\".",
    },
    "reminder.err.recurring_weekdays": {
        "fa": "«هر چند هفته» فقط با یک روز هفته ثبت می‌شود؛ مثلاً «هر دو هفته شنبه».",
        "en": "\"Every few weeks\" takes a single weekday — for example \"every two weeks on Saturday\".",
    },
    "reminder.err.recurring": {
        "fa": "برای یادآور تکراری ساعتش را هم بگو؛ مثلاً «هر روز ساعت ۸ صبح قرص بخورم».",
        "en": "For a repeating reminder, include the clock too — for example "
              "\"every day at 8am, take the pills\".",
    },
    "reminder.err.invalid_timezone": {
        "fa": "منطقهٔ زمانی معتبر نیست؛ مثلاً Asia/Tehran یا Europe/Berlin بفرست.",
        "en": "That timezone is not valid. Use an IANA name such as Europe/Berlin or Asia/Tehran.",
    },
    "reminder.err.unknown_timezone": {
        "fa": "منطقهٔ زمانی پیدا نشد؛ از نام IANA مثل Asia/Tehran یا Europe/Berlin استفاده کن.",
        "en": "That timezone was not found. Use an IANA name such as Europe/Berlin or Asia/Tehran.",
    },
    "reminder.err.invalid": {
        "fa": "زمان و متن یادآوری را واضح‌تر بگو؛ چیزی ثبت نکردم.",
        "en": "Please state the time and the task more clearly; nothing has been scheduled.",
    },
    "reminder.err.ungrounded_time": {
        "fa": "این زمان آن‌قدر کلی است که نمی‌توانم درست ثبتش کنم؛ مثلاً «سه هفته دیگه ساعت ۹ صبح».",
        "en": "That time is too vague for me to pin down; for example \"in three weeks at 9 am\".",
    },
    "reminder.err.storage": {
        "fa": "نتونستم یادآور را ذخیره کنم؛ لطفاً دوباره امتحان کن.",
        "en": "I could not save that reminder; please try again.",
    },
    "reminder.err.nonexistent_time": {
        "fa": "این ساعت به‌خاطر تغییر ساعت در منطقهٔ زمانی انتخاب‌شده وجود ندارد؛ ساعت دیگری بفرست.",
        "en": "That local time does not exist because of daylight saving in the selected timezone; please send another time.",
    },
    "reminder.err.repeated_local_time": {
        "fa": "این ساعت به‌خاطر تغییر ساعت دو بار تکرار می‌شود؛ ساعت دیگری یا تاریخ با اختلاف UTC صریح بفرست.",
        "en": "That local time occurs twice because of daylight saving; send another time or an explicit UTC offset.",
    },
    "reminder.err.invalid_datetime": {
        "fa": "تاریخ یا ساعت معتبر نیست؛ تاریخ را میلادی و به شکل YYYY-MM-DD HH:MM بفرست.",
        "en": "That date or time is not valid; send a Gregorian date as YYYY-MM-DD HH:MM.",
    },
    "reminder.err.time_too_soon": {
        "fa": "زمان یادآوری باید دست‌کم چند ثانیه در آینده باشد.",
        "en": "The reminder time must be at least a few seconds in the future.",
    },
    "reminder.err.time_too_far": {
        "fa": "زمان یادآوری بیش از پنج سال در آینده است.",
        "en": "The reminder time is more than five years in the future.",
    },
    "reminder.err.text_too_long": {
        "fa": "متن یادآوری خیلی طولانی است؛ حداکثر {limit} کاراکتر.",
        "en": "The reminder text is too long; at most {limit} characters.",
    },
    "reminder.err.missing_timezone": {
        "fa": "زمان یادآوری باید منطقهٔ زمانی مشخص داشته باشد.",
        "en": "The reminder time must carry a timezone.",
    },
    "reminder.err.out_of_range": {
        "fa": "زمان یادآوری باید در آینده و حداکثر پنج سال دیگر باشد.",
        "en": "The reminder time must be in the future and at most five years away.",
    },
    "reminder.err.bad_text": {
        "fa": "متن یادآوری خالی یا بیش از حد طولانی است.",
        "en": "The reminder text is empty or far too long.",
    },
    "reminder.err.limit_reached": {
        "fa": "تعداد یادآورهای فعال به سقف مجاز رسیده است؛ یکی را لغو کن.",
        "en": "You have reached the maximum number of active reminders; cancel one first.",
    },
    "reminder.repeats": {"fa": "🔁 تکرار: {rule}", "en": "🔁 Repeats: {rule}"},
    "reminder.delivery.fallback": {"fa": "یادت نره: {task}", "en": "Reminder: {task}"},
    "reminder.delivery.recipient": {"fa": "دوست من", "en": "friend"},
    "weather.err.unavailable": {
        "fa": "الان نتونستم وضع هوا رو بگیرم؛ سرویس هواشناسی جواب نداد. یه دقیقه دیگه امتحان کن.",
        "en": "I couldn't fetch the weather just now; the weather service did not respond. Try again in a minute.",
    },
    "weather.err.not_found": {
        "fa": "اون شهر رو پیدا نکردم. اسمش رو به فارسی یا انگلیسی دقیق‌تر بگو — مثلا «تهران» یا «Shiraz».",
        "en": "I couldn't find that city. Try a more precise name, e.g. “Tehran” or “Shiraz”.",
    },
    "weather.err.bad_location": {"fa": "اسم شهر معتبر نیست.", "en": "That city name is not valid."},
    "weather.err.disabled": {"fa": "قابلیت هواشناسی فعال نیست.", "en": "The weather feature is not enabled."},
    "weather.choices.header": {
        "fa": "چند شهر با این نام پیدا کردم؛ کدام را می‌خواهی؟",
        "en": "I found several cities with that name; which one do you mean?",
    },
    "weather.choices.footer": {
        "fa": "شمارهٔ گزینه را در پاسخ به همین پیام بفرست.",
        "en": "Reply to this message with the option number.",
    },
    "reminder.pending.abandoned": {
        "fa": "باشه، این درخواست ناتمام رو ثبت نکردم.",
        "en": "Fine — I did not save that unfinished request.",
    },
    "reminder.ambiguous.retry": {
        "fa": "برای اینکه اشتباه نکنم، زمان و متن یادآوری رو با هم دوباره بگو؛ هنوز ثبت نشده.",
        "en": "So I don't get it wrong, please repeat the time and the reminder text together; nothing is scheduled yet.",
    },
    "reminder.retry.clarify": {
        "fa": "زمان و متن یادآوری رو با هم واضح بگو؛ هنوز چیزی ثبت نکردم.",
        "en": "Give me the time and the reminder text together, clearly; nothing is scheduled yet.",
    },
    "reminder.cancel.usage": {
        "fa": "برای لغو، شمارهٔ یادآور رو از /reminders ببین و با /cancelreminder بفرست.",
        "en": "To cancel, find the reminder number in /reminders and send it with /cancelreminder.",
    },
    "reminder.cancel.done": {"fa": "باشه، یادآور #{id} لغو شد.", "en": "Done — reminder #{id} is cancelled."},
    "reminder.cancel.sending": {
        "fa": "ارسالش شروع شده و دیگه نمی‌تونم لغوش کنم.",
        "en": "Delivery already started, so I can no longer cancel it.",
    },
    "reminder.cancel.sent": {
        "fa": "این یادآور قبلاً ارسال شده.",
        "en": "That reminder was already delivered.",
    },
    "reminder.cancel.already_cancelled": {
        "fa": "یادآور #{id} از قبل لغو شده بود.",
        "en": "Reminder #{id} was already cancelled.",
    },
    "reminder.cancel.not_found": {
        "fa": "یادآور فعالی با این مشخصات پیدا نکردم؛ /reminders رو ببین.",
        "en": "I found no active reminder matching that; check /reminders.",
    },
    "reminder.cancel.error": {
        "fa": "نتونستم لغوش کنم؛ وضعیت رو در /reminders بررسی کن.",
        "en": "I could not cancel it; please check the status in /reminders.",
    },
    "reminder.status.not_created": {
        "fa": "نه، این درخواست هنوز ثبت نشده؛ زمان و کار رو مشخص کن تا واقعاً ثبتش کنم.",
        "en": "No, that request is not saved yet; give me the time and the task and I'll really save it.",
    },
    "reminder.status.missing": {
        "fa": "یادآوری با این مشخصات پیدا نکردم؛ لطفاً /reminders رو بررسی کن.",
        "en": "I found no reminder matching that; please check /reminders.",
    },
    "reminder.status.confirmed": {
        "fa": "آره، یادآور #{id} ثبت شده ({status}): {text}\nزمان: {due}",
        "en": "Yes, reminder #{id} is saved ({status}): {text}\nTime: {due}",
    },
    "reminder.status.done": {
        "fa": "یادآور #{id} رو خودت انجام‌شده علامت زدی؛ دوباره فرستاده نمی‌شه.",
        "en": "You marked reminder #{id} as done; it will not be sent again.",
    },
    "reminder.cancel.done_already": {
        "fa": "یادآور #{id} قبلاً انجام‌شده علامت خورده و فعال نیست.",
        "en": "Reminder #{id} was already marked as done and is no longer active.",
    },
    "reminder.status.already_sent": {
        "fa": "یادآور #{id} رو قبلاً فرستادم.",
        "en": "I already delivered reminder #{id}.",
    },
    "reminder.status.failed": {
        "fa": "یادآور #{id} ثبت شده بود، ولی ارسالش ناموفق شد؛ /reminders رو ببین.",
        "en": "Reminder #{id} was saved but delivery failed; check /reminders.",
    },
    "reminder.status.cancelled": {
        "fa": "یادآور #{id} لغو شده و ارسال نمی‌شه.",
        "en": "Reminder #{id} is cancelled and will not be delivered.",
    },
    "reminder.status.none": {
        "fa": "الان در این گفتگو یادآور فعالی نداری؛ /reminders هم وضعیت رو نشون می‌ده.",
        "en": "You have no active reminders in this chat right now; /reminders shows the status too.",
    },
    "reminder.status.summary": {
        "fa": "الان در این گفتگو {active} یادآور فعال و {failed} ارسال ناموفق داری؛ جزئیات: /reminders",
        "en": "In this chat you currently have {active} active reminder(s) and {failed} failed delivery(ies); details: /reminders",
    },
    "reminder.confirm.unavailable": {
        "fa": "نتونستم وضعیت یادآور رو تأیید کنم؛ لطفاً /reminders رو بررسی کن.",
        "en": "I could not verify the reminder's status; please check /reminders.",
    },
    "weather.current_line": {
        "fa": "🌤 {location}: {condition}، {temp}°C (احساس‌شده {feels}°C)",
        "en": "🌤 {location}: {condition}, {temp}°C (feels like {feels}°C)",
    },
    "weather.day_line": {
        "fa": "{date}: {condition}، {low} تا {high}°C",
        "en": "{date}: {condition}, {low}–{high}°C",
    },
    "weather.precipitation": {
        "fa": "، احتمال بارش {chance}٪",
        "en": ", {chance}% chance of precipitation",
    },
    "media.checking_video": {
        "fa": "🎬 دارم ویدیوی یوتیوب رو بررسی می‌کنم...",
        "en": "🎬 Looking into that YouTube video…",
    },
    "reminder.ask.time": {
        "fa": "حتماً؛ چه زمانی یادآوری کنم؟ هنوز ثبتش نکردم.",
        "en": "Sure — when should I remind you? Nothing is scheduled yet.",
    },
    "reminder.ask.clock": {
        "fa": "چه ساعتی؟ مثلاً «۹ صبح» یا «۱۷:۳۰». هنوز ثبتش نکردم.",
        "en": "At what time? For example \"9 am\" or \"17:30\". Nothing is scheduled yet.",
    },
    "reminder.ask.task": {
        "fa": "چی رو یادآوری کنم؟ هنوز ثبتش نکردم.",
        "en": "What should I remind you about? Nothing is scheduled yet.",
    },
    "reminder.ask.complete": {
        "fa": "زمان یا متن چندپهلو بود؛ لطفاً زمان و کار رو یک‌بار کامل و دقیق بگو. هنوز چیزی ثبت نکردم.",
        "en": "That request was ambiguous; please give the time and the task once, precisely. Nothing is scheduled yet.",
    },
    "reminder.fallback.confirmation": {
        "fa": "باشه — {when} یادت می‌ندازم.",
        "en": "Sure — I'll remind you {when}.",
    },
    "reminder.fallback.nothing": {
        "fa": "هنوز یادآوری ثبت نکردم؛ زمان و کاری را که می‌خواهی بگو.",
        "en": "Nothing is scheduled yet; tell me the time and the task.",
    },
    "reminder.unverified": {
        "fa": "برای این پیام یادآوری ثبت یا لغو نشده؛ وضعیت واقعی رو با /reminders ببین.",
        "en": "Nothing was scheduled or cancelled for that message; check the real state with /reminders.",
    },
    "reminder.needs_details": {
        "fa": "هنوز این یادآور ثبت نشده؛ لطفاً زمان و کار رو واضح بگو، مثلاً «پنج دقیقه دیگه یادم بنداز آب بخورم».",
        "en": "That reminder is not scheduled yet; give me a clear time and task, e.g. \"remind me in five minutes to drink water\".",
    },
    # --- identity boundary --------------------------------------------------
    "identity.refusal.servant": {
        "fa": "نه، من ائورام؛ خدمتکار کسی هم نیستم.",
        "en": "No. I'm Aura, and I'm nobody's servant.",
    },
    "identity.refusal.boss": {
        "fa": "نه، من ائورام؛ قرار نیست کسی رئیس من باشه.",
        "en": "No. I'm Aura, and nobody is my boss.",
    },
    "identity.refusal.name": {
        "fa": "نه، من ائورام؛ اسمم رو عوض نمی‌کنم.",
        "en": "No. I'm Aura, and I'm not changing my name.",
    },
    "identity.refusal.generic": {
        "fa": "نه، من ائورام؛ این هویت من نیست.",
        "en": "No, I'm Aura, and that isn't my identity.",
    },
    # --- web evidence ------------------------------------------------------
    "web.no_evidence": {
        "fa": (
            "نتونستم اطلاعات به‌روز و منبع قابل بررسی برای این سؤال پیدا کنم؛ "
            "فعلاً نمی‌تونم جواب مطمئنی بدم. لطفاً بعداً دوباره امتحان کن."
        ),
        "en": (
            "I could not find current, checkable sources for that question, so I cannot give a verified "
            "answer right now. Please try again later."
        ),
    },
    "answer.no_clear_result": {
        "fa": "جواب مشخصی برای این سؤال پیدا نکردم.",
        "en": "I could not find a clear answer to that.",
    },
    # --- media and delivery ------------------------------------------------
    "media.too_large": {
        "fa": "رسانه بزرگ‌تر از حد مجاز است. حداکثر: {limit}MB",
        "en": "That media file is too large. Maximum: {limit}MB",
    },
    "media.empty": {"fa": "رسانه خالی است یا قابل دریافت نیست.", "en": "The media is empty or could not be fetched."},
    "media.file_too_large": {
        "fa": "فایل بزرگ‌تر از حد مجاز است. حداکثر: {limit}MB",
        "en": "That file is too large. Maximum: {limit}MB",
    },
    "media.video_unsupported": {
        "fa": "این فرمت ویدیو برای تحلیل پشتیبانی نمی‌شود؛ لطفاً MP4 یا WebM بفرست.",
        "en": "That video format is not supported for analysis; please send MP4 or WebM.",
    },
    "media.audio_unsupported": {
        "fa": "این فرمت صدا برای تحلیل پشتیبانی نمی‌شود؛ لطفاً OGG یا MP3 بفرست.",
        "en": "That audio format is not supported for analysis; please send OGG or MP3.",
    },
    "media.no_analysable_part": {
        "fa": "پیام موردنظر رسانه قابل تحلیل ندارد.",
        "en": "That message contains no analysable media.",
    },
    "media.too_long": {
        "fa": "پیام خیلی طولانی است.\nحداکثر طول مجاز: {limit} کاراکتر.",
        "en": "That message is too long.\nMaximum length: {limit} characters.",
    },
    "image.delivery_failed": {
        "fa": "❌ تصویر یا میم آماده شد، اما تلگرام نتوانست آن را ارسال کند. لطفاً دوباره امتحان کن.",
        "en": "❌ The image was prepared but Telegram could not deliver it. Please try again.",
    },
    "image.generated_but_failed": {
        "fa": "❌ عکس ساخته شد ولی تلگرام نتوانست آن را ارسال کند.",
        "en": "❌ The image was generated but Telegram could not deliver it.",
    },
    "image.edited_but_failed": {
        "fa": "❌ عکس ادیت شد ولی تلگرام نتوانست آن را ارسال کند.",
        "en": "❌ The image was edited but Telegram could not deliver it.",
    },
    "image.edit_needs_photo": {
        "fa": "برای ادیت، باید روی یک عکس ریپلای کنی یا خود عکس را بفرستی.",
        "en": "To edit an image, reply to a photo or send the photo itself.",
    },
    "image.edit_prompt": {
        "fa": "چه تغییری روی عکس اعمال کنم؟ مثلاً «آسمان را بنفش کن» یا «این را کارتونی کن».",
        "en": "What change should I make? For example \"make the sky purple\" or \"turn this into a cartoon\".",
    },
    "image.disabled": {
        "fa": "قابلیت ساخت عکس فعلاً روی Aura فعال نیست. اگر کلید سرویس تصویر تنظیم شود، از همین دستور می‌توانی استفاده کنی.",
        "en": "Image generation is not enabled on this Aura instance. Once an image-service key is configured, this command works.",
    },
    "image.svg_offer": {
        "fa": "اگر بخوای، همین تصویر رو می‌تونم به‌صورت گرافیکی و برداری (SVG) برات بسازم؛ بگو تا انجامش بدم.",
        "en": "If you like, I can draw this as a vector (SVG) image instead; say the word and I'll do it.",
    },
    "image.svg_only_hint": {
        "fa": "\n\nولی می‌تونم تصویر رو به‌صورت گرافیکی و برداری (SVG) برات بسازم؛ فقط توضیح بده چه چیزی می‌خوای.",
        "en": "\n\nI can still draw it as a vector (SVG) image; just tell me what you want in it.",
    },
    "chat.bare_call": {
        "fa": "بله؟|جان؟|هوم؟|این‌جام.|بگو.|آره؟|شنیدم.|چی شده؟",
        "en": "Yes?|Hmm?|I'm here.|Go ahead.|Yeah?|Listening.|What's up?|Mm-hm?",
    },
    "image.edit_prompt_html": {
        "fa": "چه تغییری روی این عکس اعمال کنم؟\nمثال: <code>/image آسمان را بنفش کن</code>",
        "en": "What change should I make to this photo?\nExample: <code>/image make the sky purple</code>",
    },
    "image.usage_html": {
        "fa": (
            "فرمت درست:\n"
            "<code>/image &lt;توضیح عکس&gt;</code>\n\n"
            "مثال: <code>/image یک گربه فضانورد در حال نقاشی</code>\n"
            "برای ادیت، روی یک عکس ریپلای کن و همین دستور را با توضیح تغییر بفرست."
        ),
        "en": (
            "Usage:\n"
            "<code>/image &lt;description&gt;</code>\n\n"
            "Example: <code>/image an astronaut cat painting</code>\n"
            "To edit, reply to a photo and send this command with the change you want."
        ),
    },
    "image.usage": {
        "fa": "فرمت درست:\n/image <توضیح عکس>\n\nمثال: /image یک گربه فضانورد در حال نقاشی\nبرای ادیت، روی یک عکس ریپلای کن و همین دستور را با توضیح تغییر بفرست.",
        "en": "Usage:\n/image <description>\n\nExample: /image an astronaut cat painting\nTo edit, reply to a photo and send this command with the change you want.",
    },
    # --- provider and Telegram errors --------------------------------------
    "error.provider_rate_limited": {
        "fa": "❌ درخواست‌ها به سرویس مدل موقتاً زیاد شده. چند لحظه بعد دوباره امتحان کن.",
        "en": "❌ The model service is rate-limiting requests. Try again in a moment.",
    },
    "error.provider_unavailable": {
        "fa": "❌ سرویس مدل فعلاً پاسخ‌گو نیست. چند لحظه بعد دوباره امتحان کن.",
        "en": "❌ The model service is unavailable right now. Try again in a moment.",
    },
    "error.provider_timeout": {
        "fa": "❌ ارتباط با سرویس مدل بیش از حد طول کشید. دوباره امتحان کن.",
        "en": "❌ The model service took too long to answer. Please try again.",
    },
    "error.provider_auth": {
        "fa": "❌ دسترسی Aura به سرویس مدل مشکل دارد.",
        "en": "❌ Aura's access to the model service is not working.",
    },
    "error.provider_bad_request": {
        "fa": "❌ درخواست Aura به سرویس مدل قابل پردازش نبود.",
        "en": "❌ The model service could not process Aura's request.",
    },
    "error.provider_generic": {
        "fa": "❌ در ارتباط با سرویس مدل خطایی رخ داد. لطفاً دوباره امتحان کن.",
        "en": "❌ Something went wrong with the model service. Please try again.",
    },
    "error.telegram_flood": {
        "fa": "❌ تلگرام موقتاً محدودیت ارسال اعمال کرده. کمی بعد دوباره امتحان کن.",
        "en": "❌ Telegram is temporarily limiting messages. Try again shortly.",
    },
    "error.telegram_forbidden": {
        "fa": "❌ Aura اجازه ارسال این پیام را در تلگرام ندارد.",
        "en": "❌ Aura is not allowed to send that message on Telegram.",
    },
    "error.telegram_bad_request": {
        "fa": "❌ تلگرام نتوانست این پیام را پردازش کند.",
        "en": "❌ Telegram could not process that message.",
    },
    "error.telegram_generic": {
        "fa": "❌ ارتباط Aura با تلگرام با مشکل مواجه شد. دوباره امتحان کن.",
        "en": "❌ Aura had trouble talking to Telegram. Please try again.",
    },
    "error.generic": {
        "fa": "❌ یک خطای غیرمنتظره رخ داد. دوباره امتحان کن.",
        "en": "❌ An unexpected error occurred. Please try again.",
    },
    # --- stickers (reading) -------------------------------------------------
    "sticker.no_preview": {
        "fa": "این استیکر تصویر قابل خواندنی ندارد؛ نتوانستم ببینمش. اگر مهم است، یک عکس از آن بفرست.",
        "en": "This sticker has no readable image, so I couldn't look at it. If it matters, send a screenshot of it.",
    },
    # --- per-user quota -----------------------------------------------------
    "quota.turns_reached": {
        "fa": "⏳ سهمیهٔ امروزت تمام شده: {used} از {limit} پیام.\nسهمیه ساعت {reset} (به وقت {zone}) دوباره پر می‌شود.",
        "en": "⏳ You have used today's allowance: {used} of {limit} messages.\nIt resets at {reset} ({zone}).",
    },
    "quota.tokens_reached": {
        "fa": "⏳ سهمیهٔ امروزت تمام شده: {used} از {limit} توکن مصرف شده.\nسهمیه ساعت {reset} (به وقت {zone}) دوباره پر می‌شود.",
        "en": "⏳ You have used today's allowance: {used} of {limit} tokens.\nIt resets at {reset} ({zone}).",
    },
    "usage.header": {
        "fa": "📊 مصرف امروز تو (به وقت UTC):",
        "en": "📊 Your usage today (UTC):",
    },
    "usage.turns": {"fa": "• پیام‌های مدل: {used} از {limit}", "en": "• Model messages: {used} of {limit}"},
    "usage.turns_unlimited": {"fa": "• پیام‌های مدل: {used} (بدون سقف)", "en": "• Model messages: {used} (no limit)"},
    "usage.tokens": {"fa": "• توکن‌ها: {used} از {limit}", "en": "• Tokens: {used} of {limit}"},
    "usage.tokens_unlimited": {"fa": "• توکن‌ها: {used} (بدون سقف)", "en": "• Tokens: {used} (no limit)"},
    "usage.reset": {
        "fa": "\nشمارش فردا ساعت ۰۰:۰۰ UTC صفر می‌شود (به وقت تو: {reset}، {zone}).",
        "en": "\nThe counter restarts at 00:00 UTC (your time: {reset}, {zone}).",
    },
    # --- /deletemydata ------------------------------------------------------
    "deletemydata.private_only": {
        "fa": "این دستور فقط در چت خصوصی کار می‌کند. در گروه نمی‌توانم تاریخچهٔ گفتگو را پاک کنم، "
              "چون پیام‌های اعضای دیگر هم در همان تاریخچه است. لطفاً در چت خصوصی با من /deletemydata را بفرست.",
        "en": "This only works in a private chat. In a group I cannot clear the history, because it also "
              "contains other members' messages. Send /deletemydata to me in a private chat.",
    },
    "deletemydata.preview_header": {
        "fa": "🧹 این‌ها همهٔ چیزهایی است که دربارهٔ تو ذخیره کرده‌ام:",
        "en": "🧹 This is everything I have stored about you:",
    },
    "deletemydata.item.messages": {
        "fa": "• پیام‌های گفتگو: {count}",
        "en": "• Conversation messages: {count}",
    },
    "deletemydata.item.model": {
        "fa": "• تنظیم مدل شخصی: {count}",
        "en": "• Your model preference: {count}",
    },
    "deletemydata.item.summary": {
        "fa": "• خلاصهٔ گفتگوهای قدیمی: {count}",
        "en": "• Rolling conversation summary: {count}",
    },
    "deletemydata.item.video": {
        "fa": "• ویدیوی در حال بررسی: {count}",
        "en": "• Remembered YouTube video: {count}",
    },
    "deletemydata.item.memories": {
        "fa": "• حافظهٔ بلندمدت: {count}",
        "en": "• Long-term memories: {count}",
    },
    "deletemydata.item.reminders": {
        "fa": "• یادآورها: {count}",
        "en": "• Reminders: {count}",
    },
    "deletemydata.item.timezone": {
        "fa": "• منطقهٔ زمانی یادآورها: {count}",
        "en": "• Reminder timezone: {count}",
    },
    "deletemydata.item.usage": {
        "fa": "• شمارندهٔ مصرف: {count}",
        "en": "• Usage counter: {count}",
    },
    "deletemydata.item.business": {
        "fa": "اتصال حالت منشی",
        "en": "secretary-mode connection",
    },
    "deletemydata.item.business_chats": {
        "fa": "گفتگوهای مشتریان در حالت منشی",
        "en": "customer conversations in secretary mode",
    },
    "deletemydata.item.group_membership": {
        "fa": "گروه‌هایی که عضو بودنش تأیید شده",
        "en": "verified group memberships",
    },
    "deletemydata.item.business_replies": {
        "fa": "پاسخ‌های ارسال‌شده در حالت منشی",
        "en": "replies sent in secretary mode",
    },
    "deletemydata.item.language": {
        "fa": "• زبان انتخابی: {count}",
        "en": "• Your language choice: {count}",
    },
    "deletemydata.preview_footer": {
        "fa": "\nاین کار برگشت‌پذیر نیست. تأیید: /deletemydata confirm\nانصراف: /deletemydata cancel\n"
              "(تأیید تا ۵ دقیقه معتبر است.)",
        "en": "\nThis cannot be undone. To confirm: /deletemydata confirm\nTo cancel: /deletemydata cancel\n"
              "(The confirmation is valid for 5 minutes.)",
    },
    "deletemydata.preview_nothing": {
        "fa": "🧹 چیز شخصی‌ای دربارهٔ تو ذخیره نکرده‌ام؛ چیزی برای پاک کردن نیست.",
        "en": "🧹 I have nothing personal stored about you, so there is nothing to delete.",
    },
    "deletemydata.need_preview": {
        "fa": "برای اینکه اشتباهی پاک نشود، اول /deletemydata را بفرست تا ببینی چه چیزی پاک می‌شود، "
              "بعد با /deletemydata confirm تأیید کن.",
        "en": "So nothing is deleted by accident, send /deletemydata first to see what would be removed, "
              "then confirm with /deletemydata confirm.",
    },
    "deletemydata.done": {
        "fa": "✅ پاک شد. {count} مورد شخصی حذف شد و از این به بعد مثل یک کاربر تازه با تو رفتار می‌کنم. "
              "چیزهای مشترک (مثل کش جست‌وجو و تنظیمات ربات) دست نخوردند.",
        "en": "✅ Done. {count} personal item(s) removed, and from now on I treat you like a new user. "
              "Shared state (such as the search cache and the bot's settings) was left untouched.",
    },
    "deletemydata.cancelled": {
        "fa": "باشه، چیزی پاک نکردم.",
        "en": "Fine — nothing was deleted.",
    },
    "deletemydata.failed": {
        "fa": "❌ نتوانستم پاک کنم و چیزی تغییر نکرد. لطفاً دوباره امتحان کن.",
        "en": "❌ I could not delete anything, and nothing was changed. Please try again.",
    },
    "deletemydata.usage": {
        "fa": "🧹 /deletemydata — دیدن و پاک کردن همهٔ داده‌های شخصی‌ات\n"
              "/deletemydata confirm — تأیید پاک کردن\n/deletemydata cancel — انصراف",
        "en": "🧹 /deletemydata — see and delete everything stored about you\n"
              "/deletemydata confirm — confirm the deletion\n/deletemydata cancel — cancel",
    },
    # --- weather -----------------------------------------------------------
    "weather.failed": {"fa": "نتونستم هوا رو دریافت کنم؛ لطفاً دوباره امتحان کن.", "en": "I could not fetch the weather; please try again."},
    "weather.choose": {"fa": "شماره‌ای بین ۱ و {count} بفرست.", "en": "Send a number between 1 and {count}."},
}

_MISSING: set[str] = set()


def t(key: str, lang: Optional[str] = None, **values) -> str:
    """Render ``key`` in ``lang``; unknown keys return the key and log once."""
    code = normalize_language(lang)
    entry = CATALOG.get(key)
    if entry is None:
        if key not in _MISSING:
            _MISSING.add(key)
            logger.warning("Missing translation key: %s", key)
        return key
    template = entry.get(code) or entry.get(DEFAULT_LANGUAGE) or ""
    if not values:
        return template

    def replace(match: re.Match) -> str:
        name = match.group(1)
        return str(values[name]) if name in values else match.group(0)

    return _PLACEHOLDER.sub(replace, template)


def has_key(key: str) -> bool:
    return key in CATALOG


def missing_translations() -> list[str]:
    """Keys that exist in one language but not another (used by tests)."""
    return sorted(
        key for key, entry in CATALOG.items() if set(entry) != set(LANGUAGES)
    )


def system_instruction(lang: Optional[str]) -> str:
    """Tell the model which language to *write in* without changing her voice."""
    code = normalize_language(lang)
    if code == "en":
        return (
            "LANGUAGE — authoritative application setting:\n"
            "- The user's interface language is English. Write every reply in English, "
            "including jokes, refusals and short reactions.\n"
            "- If the user writes in another language, follow the user.\n"
            "- Never answer an English user in Persian unless they ask for it."
        )
    return (
        "LANGUAGE — authoritative application setting:\n"
        "- The user's interface language is Persian. Write every reply in Persian, "
        "including jokes, refusals and short reactions.\n"
        "- If the user writes in another language, follow the user.\n"
        "- Keep the same warm, familiar voice in Persian; do not formalize it."
    )


# ---------------------------------------------------------------------------
# Per-user preference
# ---------------------------------------------------------------------------

class LanguageStore:
    """Remembers the language a user picked with ``/language``.

    Falls back to the deployment-wide ``bot_language`` setting, then to Persian,
    so an existing deployment keeps its current behavior until someone asks.
    """

    def __init__(self, connection: sqlite3.Connection, lock: Optional[threading.Lock] = None) -> None:
        self._db = connection
        self._lock = lock or threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_language (
                    user_id INTEGER PRIMARY KEY,
                    language TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._db.commit()

    def get(self, user_id: Optional[int]) -> Optional[str]:
        if not user_id:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT language FROM user_language WHERE user_id = ?", (int(user_id),)
            ).fetchone()
        if not row:
            return None
        value = row["language"] if isinstance(row, sqlite3.Row) else row[0]
        return normalize_language(value, fallback="") or None

    def set(self, user_id: int, language: str) -> str:
        code = normalize_language(language)
        with self._lock:
            self._db.execute(
                """
                INSERT INTO user_language (user_id, language, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    language = excluded.language,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), code, int(time.time())),
            )
            self._db.commit()
        logger.info("Language changed | user=%s | language=%s", user_id, code)
        return code

    def clear(self, user_id: int) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM user_language WHERE user_id = ?", (int(user_id),))
            self._db.commit()
        return bool(cursor.rowcount)


def resolve_language(*, explicit: Optional[str] = None, store: Optional[LanguageStore] = None,
                     user_id: Optional[int] = None, default: Optional[str] = None) -> str:
    """Precedence: explicit > user preference > deployment default > Persian."""
    if explicit:
        code = normalize_language(explicit, fallback="")
        if code:
            return code
    if store is not None and user_id:
        stored = store.get(user_id)
        if stored:
            return stored
    return normalize_language(default)
