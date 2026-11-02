"""Telegram-side administration for Aura.

The owner (and any extra admins) can inspect and change runtime settings from
Telegram instead of editing `.env` on the server. This module owns everything
user-facing: the `/admin` command, the inline-keyboard panel and its callbacks.

Design rules
------------
* Access is decided in one place (`AdminAccess.allows`). Every entry point and
  every callback checks it; callbacks are re-checked because they are separate
  updates.
* The panel works in private chats only. Settings can contain provider keys and
  hostnames, and a group is the wrong place to print them.
* Secrets are shown masked. There is deliberately no command that echoes a key.
* Values are validated by `settings_store`; this module only renders results.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import providers
from settings_store import GROUPS, INDEX, SettingError, SettingsStore, specs_in_group

logger = logging.getLogger("aura-admin")

DENIED = "Access denied. This panel is limited to the bot owner and configured admins."
PRIVATE_ONLY = "For safety, settings can only be changed in a private chat with me."
USAGE = (
    "Usage:\n"
    "/admin — open the panel\n"
    "/admin set <key> <value>\n"
    "/admin unset <key>\n"
    "/admin show <key>\n"
    "/admin test — check the configured model provider\n"
    "/admin audit — recent setting changes"
)


@dataclass(frozen=True)
class AdminAccess:
    """Who may administer this deployment."""

    founder_id: int = 0
    admin_ids: tuple[int, ...] = ()

    @classmethod
    def from_env(cls, founder_id: int = 0) -> "AdminAccess":
        raw = os.getenv("ADMIN_USER_IDS", "")
        ids: list[int] = []
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                logger.warning("Ignoring a non-numeric ADMIN_USER_IDS entry: %r", part)
                continue
            if value and value not in ids:
                ids.append(value)
        return cls(founder_id=int(founder_id or 0), admin_ids=tuple(ids))

    def allows(self, user_id: Optional[int]) -> bool:
        if not user_id:
            return False
        return user_id == self.founder_id or user_id in self.admin_ids

    def describe(self) -> str:
        if not self.founder_id and not self.admin_ids:
            return "No admin is configured. Set FOUNDER_USER_ID or ADMIN_USER_IDS in .env."
        parts = []
        if self.founder_id:
            parts.append(f"founder {self.founder_id}")
        if self.admin_ids:
            parts.append("admins " + ", ".join(str(i) for i in self.admin_ids))
        return " · ".join(parts)


def _value_label(store: SettingsStore, key: str) -> str:
    spec = INDEX[key]
    value = store.describe(key)
    if spec.kind == "bool":
        return "✅ on" if value == "true" else "⛔️ off"
    if spec.kind == "secret":
        return value
    return value if len(value) <= 40 else value[:37] + "…"


def overview_text(store: SettingsStore, access: AdminAccess) -> str:
    status = providers.provider_status(store)
    if status["configured"]:
        provider_line = f"{status['provider']} · {status['model']}"
        if status["base_url"]:
            provider_line += f"\n{status['base_url']}"
    else:
        provider_line = f"⚠️ {status['error'] or 'not configured'}"
    return (
        "⚙️ Aura admin\n\n"
        f"Provider: {provider_line}\n"
        f"Encryption: {store.encryption_mode()}\n"
        f"Access: {access.describe()}\n\n"
        "Choose a section. Booleans toggle inline; numbers, choices, URLs and keys "
        "use /admin set <key> <value>."
    )


def overview_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🧠 Provider", callback_data="admin:g:provider")],
        [InlineKeyboardButton("🤖 Agent budget", callback_data="admin:g:agent")],
        [InlineKeyboardButton("🧩 Features", callback_data="admin:g:features")],
        [InlineKeyboardButton("💬 Behaviour", callback_data="admin:g:behaviour")],
        [
            InlineKeyboardButton("🩺 Test provider", callback_data="admin:test"),
            InlineKeyboardButton("🧾 Audit", callback_data="admin:audit"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def group_text(store: SettingsStore, group: str) -> str:
    lines = [f"⚙️ {group.title()}\n"]
    for spec in specs_in_group(group):
        value = _value_label(store, spec.key)
        source = store.source(spec.key)
        suffix = "" if source == "default" else f"  ({source})"
        lines.append(f"• {spec.key} = {value}{suffix}")
        if spec.help:
            lines.append(f"    {spec.help}")
    lines.append("\nTap a boolean to toggle it. Others: /admin set <key> <value>")
    return "\n".join(lines)


def group_keyboard(store: SettingsStore, group: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for spec in specs_in_group(group):
        if spec.kind == "bool":
            current = "✅" if store.get_bool(spec.key) else "⛔️"
            rows.append([InlineKeyboardButton(
                f"{current} {spec.label or spec.key}", callback_data=f"admin:t:{spec.key}",
            )])
        elif spec.kind == "choice":
            rows.append([InlineKeyboardButton(
                f"🔁 {spec.label or spec.key}: {store.get(spec.key)}", callback_data=f"admin:c:{spec.key}",
            )])
        elif spec.kind == "secret":
            rows.append([InlineKeyboardButton(
                f"🔑 {spec.label or spec.key}: {_value_label(store, spec.key)}", callback_data=f"admin:i:{spec.key}",
            )])
        else:
            rows.append([InlineKeyboardButton(
                f"✏️ {spec.label or spec.key}: {_value_label(store, spec.key)}", callback_data=f"admin:i:{spec.key}",
            )])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin:menu")])
    return InlineKeyboardMarkup(rows)


def setting_hint(store: SettingsStore, key: str) -> str:
    spec = INDEX[key]
    source = store.source(key)
    lines = [
        f"*{spec.key}*",
        f"Current: {_value_label(store, key)}  ({source})",
    ]
    if spec.help:
        lines.append(spec.help)
    if spec.kind == "secret":
        lines.append("Set a new value:")
        lines.append(f"/admin set {spec.key} YOUR_KEY")
        lines.append("The current value is never displayed, only a masked hint.")
    elif spec.kind == "choice":
        lines.append("Allowed values: " + ", ".join(spec.choices))
        lines.append(f"/admin set {spec.key} {spec.choices[0]}")
    elif spec.kind == "int":
        bounds = []
        if spec.minimum is not None:
            bounds.append(f"min {spec.minimum}")
        if spec.maximum is not None:
            bounds.append(f"max {spec.maximum}")
        lines.append(f"Whole number ({', '.join(bounds)}):")
        lines.append(f"/admin set {spec.key} {spec.default or '0'}")
    else:
        lines.append(f"/admin set {spec.key} <value>")
    if store.is_overridden(key):
        lines.append(f"Reset to the .env/default value: /admin unset {spec.key}")
    if spec.restart_hint:
        lines.append("Takes effect after a restart.")
    # Underscores would be swallowed by Markdown; this text is sent as plain text.
    return "\n".join(lines).replace("*", "")


def audit_text(store: SettingsStore, limit: int = 10) -> str:
    rows = store.recent_audit(limit)
    if not rows:
        return "🧾 No setting changes recorded yet."
    lines = ["🧾 Recent setting changes\n"]
    for row in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["at"]))
        actor = row["actor"] if row["actor"] is not None else "env/cli"
        lines.append(f"• {when} · {row['action']} · {row['key']} · by {actor}")
    return "\n".join(lines)


def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and getattr(chat, "type", "") == "private")


def _user_id(update: Update) -> Optional[int]:
    user = update.effective_user
    return getattr(user, "id", None) if user else None


async def _reply(update: Update, text: str, keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
    if not update.message:
        return
    try:
        await update.message.reply_text(text, reply_markup=keyboard)
    except TelegramError as exc:  # pragma: no cover - Telegram side
        logger.warning("Could not send an admin panel message: %s", exc)


async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    store: SettingsStore,
    access: AdminAccess,
) -> None:
    """`/admin` plus `set`, `unset`, `show`, `test`, `audit` subcommands."""
    if not update.effective_user or not update.message:
        return
    user_id = _user_id(update)
    if not access.allows(user_id):
        logger.warning("Rejected admin command | user=%s", user_id)
        await _reply(update, DENIED)
        return
    if not _is_private(update):
        await _reply(update, PRIVATE_ONLY)
        return

    args = list(context.args or [])
    action = args[0].lower() if args else ""

    if not action:
        await _reply(update, overview_text(store, access), overview_keyboard())
        return

    if action == "set":
        if len(args) < 3:
            await _reply(update, "Usage: /admin set <key> <value>\n\n" + USAGE)
            return
        key = args[1]
        value = " ".join(args[2:]).strip()
        try:
            store.set(key, value, actor=user_id)
        except SettingError as exc:
            await _reply(update, f"❌ {key}: {exc}")
            return
        except Exception as exc:  # pragma: no cover - storage failure
            logger.exception("Setting write failed")
            await _reply(update, f"❌ Could not save {key}: {exc}")
            return
        spec = INDEX[key]
        shown = store.describe(key) if spec.kind != "secret" else "saved (hidden)"
        lines = [f"✅ {key} = {shown}"]
        if spec.restart_hint:
            lines.append("Restart Aura to apply this setting.")
        await _reply(update, "\n".join(lines))
        return

    if action == "unset":
        if len(args) < 2:
            await _reply(update, "Usage: /admin unset <key>")
            return
        key = args[1]
        try:
            removed = store.unset(key, actor=user_id)
        except SettingError as exc:
            await _reply(update, f"❌ {exc}")
            return
        if removed:
            spec = INDEX[key]
            if spec.secret:
                shown = "env/default (masked)"
            else:
                shown = store.describe(key)
            await _reply(update, f"↩️ {key} now comes from {store.source(key)}: {shown}")
        else:
            await _reply(update, f"ℹ️ {key} had no override; it already uses {store.source(key)}.")
        return

    if action == "show":
        if len(args) < 2:
            await _reply(update, "Usage: /admin show <key>")
            return
        key = args[1]
        if key not in INDEX:
            await _reply(update, f"❌ Unknown setting: {key}")
            return
        await _reply(update, setting_hint(store, key))
        return

    if action == "test":
        await _reply(update, "🩺 Checking the configured provider…")
        try:
            config = providers.resolve_config(store)
            models = await asyncio.to_thread(providers.list_models, config)
            sample = ", ".join(m["id"] for m in models[:5]) or "none returned"
            await _reply(
                update,
                f"✅ {config.name} reachable\nModel: {config.model}\n"
                f"Models visible: {len(models)} (e.g. {sample})",
            )
        except providers.ProviderError as exc:
            await _reply(update, f"❌ Provider check failed\n{exc}")
        except Exception as exc:  # pragma: no cover - network dependent
            await _reply(update, f"❌ Provider check failed\n{exc}")
        return

    if action == "audit":
        await _reply(update, audit_text(store))
        return

    if action == "help":
        await _reply(update, USAGE)
        return

    await _reply(update, f"Unknown subcommand: {action}\n\n{USAGE}")


async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    store: SettingsStore,
    access: AdminAccess,
) -> None:
    """Handle inline-keyboard taps."""
    query = update.callback_query
    if query is None:
        return
    user_id = _user_id(update)
    if not access.allows(user_id):
        await query.answer("Access denied.", show_alert=True)
        logger.warning("Rejected admin callback | user=%s data=%s", user_id, query.data)
        return

    data = query.data or ""
    try:
        if data == "admin:menu":
            await query.answer()
            await query.edit_message_text(overview_text(store, access), reply_markup=overview_keyboard())
            return

        if data.startswith("admin:g:"):
            group = data.split(":", 2)[2]
            if group not in GROUPS:
                await query.answer("Unknown section.", show_alert=True)
                return
            await query.answer()
            await query.edit_message_text(group_text(store, group), reply_markup=group_keyboard(store, group))
            return

        if data.startswith("admin:t:"):
            key = data.split(":", 2)[2]
            if key not in INDEX or INDEX[key].kind != "bool":
                await query.answer("Not a toggle.", show_alert=True)
                return
            new_value = "false" if store.get_bool(key) else "true"
            store.set(key, new_value, actor=user_id)
            spec = INDEX[key]
            await query.answer(f"{spec.label or key}: {'on' if new_value == 'true' else 'off'}")
            group = spec.group
            await query.edit_message_text(group_text(store, group), reply_markup=group_keyboard(store, group))
            return

        if data.startswith("admin:c:"):
            key = data.split(":", 2)[2]
            spec = INDEX.get(key)
            if spec is None or spec.kind != "choice":
                await query.answer("Not a choice.", show_alert=True)
                return
            current = store.get(key)
            try:
                index = spec.choices.index(current)
            except ValueError:
                index = -1
            new_value = spec.choices[(index + 1) % len(spec.choices)]
            store.set(key, new_value, actor=user_id)
            await query.answer(f"{spec.label or key}: {new_value}")
            await query.edit_message_text(group_text(store, spec.group), reply_markup=group_keyboard(store, spec.group))
            return

        if data.startswith("admin:i:"):
            key = data.split(":", 2)[2]
            if key not in INDEX:
                await query.answer("Unknown setting.", show_alert=True)
                return
            await query.answer()
            await query.edit_message_text(
                setting_hint(store, key),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"admin:g:{INDEX[key].group}")]]),
            )
            return

        if data == "admin:audit":
            await query.answer()
            await query.edit_message_text(
                audit_text(store),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:menu")]]),
            )
            return

        if data == "admin:test":
            await query.answer("Checking…")
            try:
                config = providers.resolve_config(store)
                models = await asyncio.to_thread(providers.list_models, config)
                text = f"✅ {config.name} · {config.model}\nModels visible: {len(models)}"
            except Exception as exc:
                text = f"❌ Provider check failed\n{exc}"
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:menu")]]),
            )
            return

        await query.answer("Unknown action.", show_alert=True)
    except SettingError as exc:
        await query.answer(f"❌ {exc}", show_alert=True)
    except TelegramError as exc:  # pragma: no cover - Telegram side
        logger.warning("Admin callback could not update the message: %s", exc)
