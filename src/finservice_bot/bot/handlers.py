"""Telegram command handlers backed by the local SQLite repository."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from finservice_bot.bot.states import AdminState
from finservice_bot.config import ServiceCatalog
from finservice_bot.models import Language, Offer
from finservice_bot.rendering import render_offer
from finservice_bot.services.channels import ChannelVerifier
from finservice_bot.settings import Settings
from finservice_bot.storage.schema import SessionRecord
from finservice_bot.storage.sqlite_repo import DuplicateOfferError
from finservice_bot.validation import ALL_COLUMNS, CSVValidator, is_valid_url


LOGGER = logging.getLogger(__name__)

# ── Static copy ───────────────────────────────────────────────────────────────

_WELCOME = (
    "👋 <b>Welcome to FinService Bot</b>\n\n"
    "I publish curated financial referral offers to dedicated Telegram channels — "
    "credit cards, loans, insurance, investments, and more.\n\n"
    "Use the buttons below or type /help to see all commands."
)

_WELCOME_BLOCKED = "⛔ This account is not authorised to use this bot."

_HELP_PUBLIC = (
    "ℹ️ <b>Help</b>\n\n"
    "<b>Your account</b>\n"
    "/start — Open or restart the bot\n"
    "/language — Choose your preferred language\n"
    "/privacy — How your data is stored and for how long\n"
    "/delete_me — Permanently erase your account\n"
    "/help — Show this message"
)

_HELP_ADMIN_EXTRA = (
    "\n\n<b>Offers</b>\n"
    "/add_offer — Add a new offer (guided wizard)\n"
    "/list_queued — View offers waiting to be published\n"
    "/template — Download the CSV bulk-import template\n"
    "\n<b>Channels</b>\n"
    "/setup_channels <i>key</i> <i>@channel</i> — Link a service to a channel\n"
    "/list_services — Service catalogue with channel status\n"
    "\n<b>Operations</b>\n"
    "/stats — Publication counts by status\n"
    "/audit — Last 20 admin actions\n"
    "/prune — Clean up expired records\n"
    "/block <i>user_id</i> — Restrict a user\n"
    "/unblock <i>user_id</i> — Restore a user\n"
    "/cancel — Stop the current workflow"
)

_PRIVACY = (
    "🔒 <b>Data &amp; Privacy</b>\n\n"
    "<b>What we store</b>\n"
    "• Your Telegram numeric ID\n"
    "• Role (user / admin) and block state\n"
    "• Activity timestamps (first seen, last active)\n"
    "• Short-lived workflow state — deleted automatically when it expires\n\n"
    "<b>What we never collect</b>\n"
    "• Your name, username, or phone number\n"
    "• Message content outside active workflows\n\n"
    "Use /delete_me to erase everything we hold about you."
)

_NOT_ADMIN = "🚫 This command is available to administrators only."

_STATUS_ICON: dict[str, str] = {
    "draft":            "📝",
    "queued":           "🕐",
    "publishing":       "📤",
    "published":        "✅",
    "failed":           "❌",
    "review_required":  "👁",
    "archived":         "📦",
}

_ACTION_LABEL: dict[str, str] = {
    "offer.import":   "Offer imported",
    "route.update":   "Channel linked",
    "privacy.delete": "Account erased",
    "user.block":     "User blocked",
    "user.unblock":   "User unblocked",
    "session.prune":  "Records pruned",
}

_LANG_MODE_LABEL: dict[str, str] = {
    "single":   "English only",
    "multi":    "Multi-language",
    "rotating": "Rotating",
}

_LANG_LABEL: dict[str, str] = {
    "en": "🇬🇧 English",
    "hi": "🇮🇳 हिन्दी",
    "gu": "🇮🇳 ગુજરાતી",
}

_LANG_DISPLAY: dict[str, str] = {
    "en": "English",
    "hi": "हिन्दी (Hindi)",
    "gu": "ગુજરાતી (Gujarati)",
}

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ── Visual helpers ─────────────────────────────────────────────────────────────

def _make_bar(count: int, total: int, width: int = 10) -> str:
    """Return a Unicode block-character progress bar."""
    if total == 0 or count == 0:
        return "░" * width
    filled = max(1, round(count / total * width))
    return "▓" * filled + "░" * (width - filled)


def _relative_time(dt: datetime, now: datetime) -> str:
    """Return a human-friendly relative timestamp (e.g. '5 min ago', 'in 2 h')."""
    delta = dt - now
    seconds = int(delta.total_seconds())
    abs_s = abs(seconds)
    suffix = "ago" if seconds < 0 else "from now"
    if abs_s < 60:
        return f"{abs_s}s {suffix}"
    if abs_s < 3600:
        return f"{abs_s // 60}m {suffix}"
    if abs_s < 86400:
        return f"{abs_s // 3600}h {suffix}"
    return f"{abs_s // 86400}d {suffix}"


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 Language", callback_data="action:language"),
            InlineKeyboardButton("🔒 Privacy",  callback_data="action:privacy"),
        ],
        [InlineKeyboardButton("🗑 Delete my data", callback_data="action:delete_me")],
    ])


def _confirm_keyboard(yes_data: str, yes_label: str = "✅ Yes, proceed") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(yes_label, callback_data=yes_data),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_action"),
    ]])


def _service_keyboard(catalog: ServiceCatalog) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key in catalog.keys():
        svc = catalog.get(key)
        label = f"{svc.icon} {svc.display_name(Language.ENGLISH)}"
        row.append(InlineKeyboardButton(label, callback_data=f"wizard_svc:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="wizard_cancel")])
    return InlineKeyboardMarkup(buttons)


def _language_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    for code, label in _LANG_LABEL.items():
        tick = " ✓" if code == current else ""
        rows.append([InlineKeyboardButton(f"{label}{tick}", callback_data=f"lang:{code}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    return InlineKeyboardMarkup(rows)


def _wizard_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Queue this offer", callback_data="wizard_confirm")],
        [
            InlineKeyboardButton("🔁 Start over", callback_data="wizard_restart"),
            InlineKeyboardButton("❌ Cancel",     callback_data="wizard_cancel"),
        ],
    ])


def _skip_keyboard(callback_data: str, label: str = "⏭ Skip this step") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=callback_data),
        InlineKeyboardButton("❌ Cancel", callback_data="wizard_cancel"),
    ]])


# ── Handler class ─────────────────────────────────────────────────────────────

class BotHandlers:
    def __init__(self, settings: Settings, repository: Any, catalog: ServiceCatalog) -> None:
        self.settings = settings
        self.repository = repository
        self.catalog = catalog
        self.validator = CSVValidator(
            max_rows=settings.max_csv_rows,
            max_bytes=settings.max_csv_bytes,
            allowed_domains=set(settings.allowed_referral_domains),
        )

    # ── Public commands ───────────────────────────────────────────────────────

    async def start(self, update: Any, context: Any) -> None:
        del context
        user_id, message = self._identity(update)
        now = datetime.now(UTC)
        user = await self.repository.touch_user(
            user_id,
            is_admin=user_id in self.settings.admin_ids,
            now=now,
        )
        if user.blocked:
            await message.reply_text(_WELCOME_BLOCKED)
            return

        if self._is_admin(user_id):
            # Check if any channels are set up; offer first-run guidance
            routes = await self.repository.list_routes()
            verified = sum(1 for r in routes if r.verified_at)
            tip = (
                "\n\n💡 <b>Admin tip:</b> No channels are verified yet. "
                "Run /setup_channels to link your first service."
                if verified == 0 else ""
            )
            await message.reply_text(
                f"{_WELCOME}{tip}",
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                _WELCOME,
                parse_mode="HTML",
                reply_markup=_welcome_keyboard(),
            )

    async def help(self, update: Any, context: Any) -> None:
        del context
        user_id, message = self._identity(update)
        user = await self.repository.touch_user(
            user_id,
            is_admin=user_id in self.settings.admin_ids,
            now=datetime.now(UTC),
        )
        if user.blocked:
            await message.reply_text(_WELCOME_BLOCKED)
            return

        lang_name = _LANG_DISPLAY.get(user.display_language or "en", "English")
        lang_line = f"\n🌐 Your language: <b>{lang_name}</b> · change with /language"

        text = _HELP_PUBLIC + lang_line
        if user_id in self.settings.admin_ids:
            text += _HELP_ADMIN_EXTRA
        await message.reply_text(text, parse_mode="HTML")

    async def privacy(self, update: Any, context: Any) -> None:
        del context
        user_id, message = self._identity(update)
        user = await self.repository.touch_user(
            user_id,
            is_admin=user_id in self.settings.admin_ids,
            now=datetime.now(UTC),
        )
        if user.blocked:
            await message.reply_text(_WELCOME_BLOCKED)
            return
        await message.reply_text(_PRIVACY, parse_mode="HTML")

    async def language(self, update: Any, context: Any) -> None:
        del context
        user_id, message = self._identity(update)
        now = datetime.now(UTC)
        user = await self.repository.touch_user(
            user_id,
            is_admin=user_id in self.settings.admin_ids,
            now=now,
        )
        if user.blocked:
            await message.reply_text(_WELCOME_BLOCKED)
            return
        current = user.display_language or "en"
        current_name = _LANG_DISPLAY.get(current, "English")
        await message.reply_text(
            f"🌐 <b>Choose your language</b>\n\n"
            f"Current: <b>{current_name}</b>\n\n"
            "Select a language — offer messages will be shown in your preferred "
            "language wherever a translation is available.",
            parse_mode="HTML",
            reply_markup=_language_keyboard(current),
        )

    async def delete_me(self, update: Any, context: Any) -> None:
        del context
        _, message = self._identity(update)
        await message.reply_text(
            "⚠️ <b>Delete your account?</b>\n\n"
            "This will permanently erase your user profile, role, language preference, "
            "and any active workflow state. It cannot be undone.\n\n"
            "<i>Note: this is your right regardless of account status.</i>",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard("confirm_delete", "🗑 Yes, delete my data"),
        )

    # ── Admin commands ────────────────────────────────────────────────────────

    async def add_offer(self, update: Any, context: Any) -> None:
        del context
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        _, message = authorized
        await message.reply_chat_action("typing")
        await message.reply_text(
            "🧭 <b>New Offer Wizard</b>\n\n"
            "<b>Step 1 of 4:</b> Select a service category\n"
            "<i>Hindi &amp; Gujarati translations can be added optionally after step 3.</i>",
            parse_mode="HTML",
            reply_markup=_service_keyboard(self.catalog),
        )

    async def text_input(self, update: Any, context: Any) -> None:
        del context
        user_id, message = self._identity(update)
        now = datetime.now(UTC)
        session = await self.repository.get_session(user_id, now=now)
        if session is None:
            return
        if await self._require_admin(update) is None:
            return
        state = session.state
        if state == AdminState.AWAITING_OFFER_ROW:
            await self._handle_csv_row(message, session, user_id, now)
        elif state == AdminState.WIZARD_PROVIDER:
            await self._wizard_handle_provider(message, session, user_id, now)
        elif state == AdminState.WIZARD_TITLE_EN:
            await self._wizard_handle_title_en(message, session, user_id, now)
        elif state == AdminState.WIZARD_TITLE_HI:
            await self._wizard_handle_title_hi(message, session, user_id, now)
        elif state == AdminState.WIZARD_TITLE_GU:
            await self._wizard_handle_title_gu(message, session, user_id, now)
        elif state == AdminState.WIZARD_LINK:
            await self._wizard_handle_link(message, session, user_id, now)
        # WIZARD_CONFIRM is handled by callback query only

    async def upload_csv(self, update: Any, context: Any) -> None:
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        user_id, message = authorized
        document = message.document
        size = int(document.file_size or 0)
        if size <= 0 or size > self.settings.max_csv_bytes:
            max_kb = self.settings.max_csv_bytes // 1024
            await message.reply_text(f"⚠️ CSV must be between 1 B and {max_kb} KB.")
            return
        if not str(document.file_name or "").lower().endswith(".csv"):
            await message.reply_text(
                "⚠️ Please upload a <code>.csv</code> file.", parse_mode="HTML"
            )
            return
        await message.reply_chat_action("typing")
        telegram_file = await context.bot.get_file(document.file_id)
        payload = bytes(await telegram_file.download_as_bytearray())
        try:
            content = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            await message.reply_text("⚠️ File must use UTF-8 encoding.")
            return
        result = self.validator.validate_text(content)
        if not result.valid:
            total_errors = len(result.errors)
            shown = result.errors[:5]
            errors = "\n".join(
                f"• Row {e.row} <code>{e.field}</code>: {e.message}"
                for e in shown
            )
            footer = (
                f"\n<i>…and {total_errors - len(shown)} more error(s)</i>"
                if total_errors > len(shown) else ""
            )
            await message.reply_text(
                f"⚠️ <b>Validation failed</b> — {total_errors} error(s)\n\n"
                f"{errors}{footer}\n\n"
                "Fix the issues and re-upload, or use /template to get a fresh template.",
                parse_mode="HTML",
            )
            return
        now = datetime.now(UTC)
        queued, duplicates = await self._queue_offers(result.offers, user_id=user_id, now=now)
        parts = [f"✅ <b>{queued} offer(s) queued for publication</b>"]
        if duplicates:
            parts.append(f"⏭ {duplicates} duplicate(s) skipped")
        if result.warnings:
            parts.append(f"⚠️ {len(result.warnings)} warning(s) — check for truncated fields")
        parts.append("\nUse /stats to see queue status.")
        await message.reply_text("\n".join(parts), parse_mode="HTML")

    async def template(self, update: Any, context: Any) -> None:
        del context
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        _, message = authorized
        data = io.BytesIO(self.validator.generate_template().encode("utf-8"))
        await message.reply_document(
            document=data,
            filename="finservice-offers.csv",
            caption=(
                "📄 <b>CSV Import Template</b>\n\n"
                "Fill in each row and upload back here to queue your offers.\n"
                "Required columns: <code>service_type</code>, <code>provider</code>, "
                "<code>title_en</code>, <code>referral_link</code>"
            ),
            parse_mode="HTML",
        )

    async def setup_channels(self, update: Any, context: Any) -> None:
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        user_id, message = authorized
        if len(context.args) != 2:
            await message.reply_text(
                "📡 <b>Setup channels</b>\n\n"
                "Usage: <code>/setup_channels &lt;service_key&gt; &lt;@channel&gt;</code>\n\n"
                "Example:\n"
                "<code>/setup_channels credit_card @MyCreditCardChannel</code>\n\n"
                "Use /list_services to see all valid service keys.",
                parse_mode="HTML",
            )
            return
        service_key, channel_id = context.args
        if service_key not in self.catalog.keys():
            await message.reply_text(
                f"⚠️ Unknown service key: <code>{service_key}</code>\n\n"
                "Use /list_services to see valid keys.",
                parse_mode="HTML",
            )
            return
        await message.reply_chat_action("typing")
        now = datetime.now(UTC)
        verification = await ChannelVerifier(context.bot).verify(channel_id, now=now)
        if not verification.verified or verification.verified_at is None:
            await message.reply_text(
                f"⚠️ <b>Channel verification failed</b>\n\n{verification.reason}",
                parse_mode="HTML",
            )
            return
        await self.repository.set_route(
            service_key,
            channel_id=channel_id,
            enabled=True,
            verified_at=verification.verified_at,
            updated_by=user_id,
            now=now,
        )
        await self.repository.record_audit_event(
            actor_user_id=user_id,
            action="route.update",
            target_type="service_route",
            target_id=service_key,
            result="success",
            safe_details={"channel": channel_id},
            now=now,
        )
        svc = self.catalog.get(service_key)
        await message.reply_text(
            f"✅ <b>Channel activated</b>\n\n"
            f"{svc.icon} {svc.display_name(Language.ENGLISH)}\n"
            f"↳ {channel_id}\n\n"
            "Offers queued for this service will now be published automatically.",
            parse_mode="HTML",
        )

    async def list_services(self, update: Any, context: Any) -> None:
        del context
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        _, message = authorized
        await message.reply_chat_action("typing")
        routes = {r.service_key: r for r in await self.repository.list_routes()}
        counts = await self.repository.offer_status_counts()
        queued_total = counts.get("queued", 0)

        lines = ["🔌 <b>Service Routes</b>\n"]
        for key in self.catalog.keys():
            svc = self.catalog.get(key)
            route = routes.get(key)
            if route and route.enabled and route.verified_at:
                status_icon = "✅"
                channel_line = f"{route.channel_id}"
            elif route and route.channel_id:
                status_icon = "⚠️"
                channel_line = f"{route.channel_id} (not verified)"
            else:
                status_icon = "❌"
                channel_line = "not configured"
            lang = _LANG_MODE_LABEL.get(svc.language_mode, svc.language_mode)
            lines.append(
                f"{status_icon} {svc.icon} <b>{svc.display_name(Language.ENGLISH)}</b>\n"
                f"   📡 {channel_line}\n"
                f"   🌐 {lang} · ⏱ {svc.rate_limit_per_hour}/h"
            )

        if queued_total:
            lines.append(f"\n🕐 <b>{queued_total} offer(s)</b> waiting in queue across all services.")
        else:
            lines.append("\n✅ Queue is empty.")
        await message.reply_text("\n".join(lines), parse_mode="HTML")

    async def stats(self, update: Any, context: Any) -> None:
        del context
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        _, message = authorized
        await message.reply_chat_action("typing")
        counts = await self.repository.offer_status_counts()
        if not counts:
            await message.reply_text(
                "📊 <b>Offer Statistics</b>\n\nNo offers on record yet.\n\n"
                "Use /add_offer or upload a CSV to get started.",
                parse_mode="HTML",
            )
            return
        total = sum(counts.values())
        ordered = [
            ("queued",          True),
            ("publishing",      False),
            ("published",       True),
            ("failed",          True),
            ("review_required", False),
            ("archived",        False),
            ("draft",           False),
        ]
        lines = [f"📊 <b>Offer Statistics</b>  —  {total} total\n"]
        for status, always_show in ordered:
            count = counts.get(status, 0)
            if count == 0 and not always_show:
                continue
            icon  = _STATUS_ICON.get(status, "•")
            label = status.replace("_", " ").title()
            bar   = _make_bar(count, total)
            pct   = f"{count / total * 100:.0f}%" if total else "—"
            lines.append(
                f"{icon} <b>{label:<18}</b> {bar} <code>{count:>4}</code>  <i>{pct}</i>"
            )
        await message.reply_text("\n".join(lines), parse_mode="HTML")

    async def audit(self, update: Any, context: Any) -> None:
        del context
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        _, message = authorized
        await message.reply_chat_action("typing")
        events = await self.repository.list_audit_events(limit=20)
        if not events:
            await message.reply_text(
                "📋 <b>Audit Log</b>\n\nNo admin actions recorded yet.",
                parse_mode="HTML",
            )
            return
        lines = ["📋 <b>Audit Log</b> — last 20 events\n"]
        for event in events:
            label       = _ACTION_LABEL.get(event["action"], event["action"])
            ts          = event["created_at"].strftime("%d %b, %H:%M")
            result_icon = "✅" if event["result"] == "success" else "❌"
            detail      = ""
            raw         = event.get("safe_details")
            if raw:
                try:
                    d = raw if isinstance(raw, dict) else json.loads(raw)
                    if "queued" in d:
                        q, dupes = d["queued"], d.get("duplicates", 0)
                        detail = f" <i>{q} queued" + (f", {dupes} skipped" if dupes else "") + "</i>"
                    elif "channel" in d:
                        detail = f" <i>→ {d['channel']}</i>"
                    elif "deleted" in d:
                        detail = f" <i>{d['deleted']} removed</i>"
                except Exception:
                    pass
            lines.append(f"{result_icon} <b>{label}</b>{detail}  <code>{ts}</code>")
        await message.reply_text("\n".join(lines), parse_mode="HTML")

    async def list_queued(self, update: Any, context: Any) -> None:
        del context
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        _, message = authorized
        await message.reply_chat_action("typing")
        offers = await self.repository.list_queued_offers(limit=15)
        now = datetime.now(UTC)
        if not offers:
            await message.reply_text(
                "🕐 <b>Queued Offers</b>\n\nThe queue is empty — all clear!",
                parse_mode="HTML",
            )
            return
        counts = await self.repository.offer_status_counts()
        total_queued = counts.get("queued", 0)
        shown = len(offers)
        lines = [f"🕐 <b>Queued Offers</b>  —  {total_queued} total\n"]
        for i, rec in enumerate(offers, 1):
            svc   = self.catalog.get(rec.offer.service_type)
            when  = _relative_time(rec.scheduled_at, now)
            title = rec.offer.title_en[:60] + ("…" if len(rec.offer.title_en) > 60 else "")
            lines.append(
                f"<b>{i}.</b> {svc.icon} {svc.display_name(Language.ENGLISH)}\n"
                f"   <i>{rec.offer.provider}</i> — {title}\n"
                f"   ⏰ {when}"
            )
        if total_queued > shown:
            lines.append(f"\n<i>…and {total_queued - shown} more. Use /stats for totals.</i>")
        await message.reply_text("\n".join(lines), parse_mode="HTML")

    async def prune(self, update: Any, context: Any) -> None:
        del context
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        _, message = authorized
        await message.reply_text(
            "🧹 <b>Prune expired records?</b>\n\n"
            "This will delete expired sessions and old posting history (older than 365 days). "
            "Active offers are not affected.",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard("confirm_prune", "🧹 Yes, prune now"),
        )

    async def block(self, update: Any, context: Any) -> None:
        await self._set_blocked(update, context, blocked=True)

    async def unblock(self, update: Any, context: Any) -> None:
        await self._set_blocked(update, context, blocked=False)

    async def cancel(self, update: Any, context: Any) -> None:
        """Cancel any active session. Works for all users, not just admins."""
        del context
        user_id, message = self._identity(update)
        await self.repository.delete_session(user_id)
        if user_id in self.settings.admin_ids:
            await message.reply_text(
                "✅ Workflow cancelled.\n\nUse /add_offer to start a new offer, "
                "or /help to see all commands."
            )
        else:
            await message.reply_text("✅ Cancelled.")

    # ── Callback query dispatcher ─────────────────────────────────────────────

    async def handle_callback_query(self, update: Any, context: Any) -> None:
        """Route inline keyboard callbacks via a dispatch table.

        Exact matches are looked up first; prefix matches are iterated in order.
        Adding a new callback requires only a new ``_cb_*`` method and an entry
        in one of the two structures below.
        """
        del context
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        user_id = int(query.from_user.id)
        data: str = query.data or ""
        now = datetime.now(UTC)

        # Exact-match dispatch
        exact_handlers: dict[str, Any] = {
            "cancel_action":           self._cb_cancel_action,
            "confirm_delete":          self._cb_confirm_delete,
            "confirm_prune":           self._cb_confirm_prune,
            "wizard_confirm":          self._cb_wizard_confirm,
            "wizard_restart":          self._cb_wizard_restart,
            "wizard_cancel":           self._cb_wizard_cancel,
            "wizard_skip_translations": self._cb_wizard_skip_translations,
            "wizard_skip_gu":          self._cb_wizard_skip_gu,
        }
        if data in exact_handlers:
            await exact_handlers[data](query, user_id, data, now)
            return

        # Prefix-match dispatch (order matters for overlapping prefixes)
        prefix_handlers: tuple[tuple[str, Any], ...] = (
            ("confirm_block:",   self._cb_confirm_block),
            ("confirm_unblock:", self._cb_confirm_unblock),
            ("wizard_svc:",      self._cb_wizard_svc),
            ("lang:",            self._cb_lang),
            ("action:",          self._cb_action),
        )
        for prefix, handler in prefix_handlers:
            if data.startswith(prefix):
                await handler(query, user_id, data, now)
                return

    # ── Callback implementations ──────────────────────────────────────────────

    async def _cb_cancel_action(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        del user_id, data, now
        await query.edit_message_text("✅ Cancelled.")

    async def _cb_confirm_delete(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        del data
        await self.repository.touch_user(
            user_id,
            is_admin=user_id in self.settings.admin_ids,
            now=now,
        )
        await self.repository.record_audit_event(
            actor_user_id=user_id,
            action="privacy.delete",
            target_type="user",
            target_id=str(user_id),
            result="success",
            safe_details={},
            now=now,
        )
        await self.repository.delete_user_data(user_id, now=now)
        await query.edit_message_text(
            "🗑 <b>Account erased.</b>\n\n"
            "All data we held about you has been permanently deleted. "
            "You can use /start to create a fresh account at any time.",
            parse_mode="HTML",
        )

    async def _cb_confirm_prune(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        del data
        if not self._is_admin(user_id):
            await query.edit_message_text("🚫 Not authorised.")
            return
        deleted = await self.repository.prune_expired(now=now)
        await query.edit_message_text(
            f"✅ <b>Prune complete</b>\n\n{deleted} expired record(s) removed.",
            parse_mode="HTML",
        )

    async def _cb_confirm_block(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        if not self._is_admin(user_id):
            await query.edit_message_text("🚫 Not authorised.")
            return
        try:
            target = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await query.edit_message_text("⚠️ Invalid user ID.")
            return
        changed = await self.repository.set_user_blocked(target, blocked=True, now=now)
        if changed:
            await query.edit_message_text(
                f"⛔ User <code>{target}</code> has been <b>blocked</b>.\n"
                "They will no longer be able to use the bot.",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                f"⚠️ User <code>{target}</code> was not found in the database.",
                parse_mode="HTML",
            )

    async def _cb_confirm_unblock(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        if not self._is_admin(user_id):
            await query.edit_message_text("🚫 Not authorised.")
            return
        try:
            target = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await query.edit_message_text("⚠️ Invalid user ID.")
            return
        changed = await self.repository.set_user_blocked(target, blocked=False, now=now)
        if changed:
            await query.edit_message_text(
                f"✅ User <code>{target}</code> has been <b>unblocked</b>.\n"
                "They can now use the bot again.",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                f"⚠️ User <code>{target}</code> was not found in the database.",
                parse_mode="HTML",
            )

    async def _cb_wizard_svc(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        if not self._is_admin(user_id):
            await query.edit_message_text("🚫 Not authorised.")
            return
        key = data[len("wizard_svc:"):]
        if key not in self.catalog.keys():
            await query.edit_message_text("⚠️ Unknown service key.")
            return
        svc = self.catalog.get(key)
        nonce = secrets.token_urlsafe(24)
        await self.repository.save_session(SessionRecord(
            telegram_user_id=user_id,
            state=AdminState.WIZARD_PROVIDER.value,
            nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
            payload={"service_type": key, "_nonce": nonce},
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(minutes=self.settings.session_ttl_minutes),
        ))
        await query.edit_message_text(
            f"🧭 <b>New Offer Wizard</b>\n\n"
            f"✅ Category: {svc.icon} <b>{svc.display_name(Language.ENGLISH)}</b>\n\n"
            "<b>Step 2 of 4:</b> Enter the <b>provider name</b>\n"
            "<i>e.g. HDFC Bank, ICICI, SBI, Bajaj Finance</i>",
            parse_mode="HTML",
        )

    async def _cb_wizard_confirm(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        del data
        if not self._is_admin(user_id):
            await query.edit_message_text("🚫 Not authorised.")
            return
        session = await self.repository.get_session(user_id, now=now)
        if session is None or session.state != AdminState.WIZARD_CONFIRM.value:
            await query.edit_message_text(
                "⚠️ Session expired. Use /add_offer to start again."
            )
            return
        p = session.payload
        offer = Offer(
            service_type=p["service_type"],
            provider=p["provider"],
            title_en=p["title_en"],
            referral_link=p["referral_link"],
            title_hi=p.get("title_hi"),
            title_gu=p.get("title_gu"),
        )
        try:
            await self.repository.create_offer(
                offer, created_by=user_id, scheduled_at=now, now=now
            )
            queued, duplicates = 1, 0
        except DuplicateOfferError:
            queued, duplicates = 0, 1
        await self.repository.delete_session(user_id)
        await self.repository.record_audit_event(
            actor_user_id=user_id,
            action="offer.import",
            target_type="offer_batch",
            target_id="wizard",
            result="success",
            safe_details={"queued": queued, "duplicates": duplicates},
            now=now,
        )
        if queued:
            svc = self.catalog.get(p["service_type"])
            langs_added = []
            if p.get("title_hi"):
                langs_added.append("🇮🇳 Hindi")
            if p.get("title_gu"):
                langs_added.append("🇮🇳 Gujarati")
            lang_note = (
                f"\nTranslations: {', '.join(langs_added)}" if langs_added else ""
            )
            await query.edit_message_text(
                f"✅ <b>Offer queued successfully!</b>\n\n"
                f"{svc.icon} <b>{svc.display_name(Language.ENGLISH)}</b>\n"
                f"Provider: {p['provider']}"
                f"{lang_note}\n\n"
                "Use /list_queued to see all pending offers.",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                "⏭ <b>Duplicate skipped</b>\n\n"
                "An identical offer already exists in the queue.",
                parse_mode="HTML",
            )

    async def _cb_wizard_restart(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        del data
        if not self._is_admin(user_id):
            await query.edit_message_text("🚫 Not authorised.")
            return
        await self.repository.delete_session(user_id)
        await query.edit_message_text(
            "🧭 <b>New Offer Wizard</b>\n\n"
            "<b>Step 1 of 4:</b> Select a service category:",
            parse_mode="HTML",
            reply_markup=_service_keyboard(self.catalog),
        )

    async def _cb_wizard_cancel(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        del now
        await self.repository.delete_session(user_id)
        await query.edit_message_text(
            "✅ Offer creation cancelled.\n\nUse /add_offer to start a new one.",
        )

    async def _cb_wizard_skip_translations(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        """Skip Hindi & Gujarati — jump straight to the referral link step."""
        del data
        if not self._is_admin(user_id):
            await query.edit_message_text("🚫 Not authorised.")
            return
        session = await self.repository.get_session(user_id, now=now)
        if session is None:
            await query.edit_message_text(
                "⚠️ Session expired. Use /add_offer to start again."
            )
            return
        await self._wizard_advance(
            session, user_id, now,
            new_state=AdminState.WIZARD_LINK,
            extra_payload={},
        )
        await query.edit_message_text(
            f"🧭 <b>New Offer Wizard</b>\n\n"
            f"✅ Category, provider, and English title saved\n\n"
            "<b>Step 4 of 4:</b> Enter the <b>referral link</b>\n"
            "Must start with <code>https://</code>",
            parse_mode="HTML",
        )

    async def _cb_wizard_skip_gu(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        """Skip Gujarati — jump to the referral link step."""
        del data
        if not self._is_admin(user_id):
            await query.edit_message_text("🚫 Not authorised.")
            return
        session = await self.repository.get_session(user_id, now=now)
        if session is None:
            await query.edit_message_text(
                "⚠️ Session expired. Use /add_offer to start again."
            )
            return
        await self._wizard_advance(
            session, user_id, now,
            new_state=AdminState.WIZARD_LINK,
            extra_payload={},
        )
        await query.edit_message_text(
            f"🧭 <b>New Offer Wizard</b>\n\n"
            f"✅ Category, provider, and titles saved\n\n"
            "<b>Step 4 of 4:</b> Enter the <b>referral link</b>\n"
            "Must start with <code>https://</code>",
            parse_mode="HTML",
        )

    async def _cb_lang(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        code = data[len("lang:"):]
        if code not in ("en", "hi", "gu"):
            await query.edit_message_text("⚠️ Unknown language code.")
            return
        await self.repository.touch_user(
            user_id,
            is_admin=user_id in self.settings.admin_ids,
            now=now,
        )
        await self.repository.set_display_language(user_id, language=code, now=now)
        name = _LANG_DISPLAY.get(code, code)
        await query.edit_message_text(
            f"✅ <b>Language updated</b>\n\nNow set to: <b>{name}</b>\n\n"
            "Offer messages will appear in this language wherever a translation is available.",
            parse_mode="HTML",
        )

    async def _cb_action(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        """Handle quick-action buttons from the welcome keyboard."""
        action = data[len("action:"):]
        if action == "privacy":
            user = await self.repository.get_user(user_id)
            if user and user.blocked:
                await query.edit_message_text(_WELCOME_BLOCKED)
                return
            await query.edit_message_text(_PRIVACY, parse_mode="HTML")

        elif action == "language":
            user = await self.repository.touch_user(
                user_id,
                is_admin=self._is_admin(user_id),
                now=now,
            )
            if user.blocked:
                await query.edit_message_text(_WELCOME_BLOCKED)
                return
            current = user.display_language or "en"
            current_name = _LANG_DISPLAY.get(current, "English")
            await query.edit_message_text(
                f"🌐 <b>Choose your language</b>\n\nCurrent: <b>{current_name}</b>",
                parse_mode="HTML",
                reply_markup=_language_keyboard(current),
            )

        elif action == "delete_me":
            await query.edit_message_text(
                "⚠️ <b>Delete your account?</b>\n\n"
                "This will permanently erase your user profile, role, language preference, "
                "and any active workflow state. It cannot be undone.",
                parse_mode="HTML",
                reply_markup=_confirm_keyboard("confirm_delete", "🗑 Yes, delete my data"),
            )

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _handle_csv_row(
        self, message: Any, session: Any, user_id: int, now: datetime
    ) -> None:
        content = ",".join(ALL_COLUMNS) + "\n" + (message.text or "") + "\n"
        result = self.validator.validate_text(content)
        if not result.valid:
            errors = "\n".join(
                f"• <code>{e.field}</code>: {e.message}" for e in result.errors[:5]
            )
            await message.reply_text(
                f"⚠️ <b>Validation failed</b>\n\n{errors}\n\n"
                "Fix the values and try again, or /cancel to stop.",
                parse_mode="HTML",
            )
            return
        queued, duplicates = await self._queue_offers(result.offers, user_id=user_id, now=now)
        await self.repository.delete_session(user_id)
        parts = [f"✅ <b>{queued} offer(s) queued</b>"]
        if duplicates:
            parts.append(f"⏭ {duplicates} duplicate(s) skipped")
        await message.reply_text("\n".join(parts), parse_mode="HTML")

    async def _wizard_handle_provider(
        self, message: Any, session: Any, user_id: int, now: datetime
    ) -> None:
        provider = (message.text or "").strip()
        if not provider or len(provider) > 120:
            await message.reply_text(
                "⚠️ Provider name must be 1–120 characters. Try again:"
            )
            return
        await self._wizard_advance(
            session, user_id, now,
            new_state=AdminState.WIZARD_TITLE_EN,
            extra_payload={"provider": provider},
        )
        await message.reply_text(
            f"🧭 <b>New Offer Wizard</b>\n\n"
            f"✅ Provider: <b>{provider}</b>\n\n"
            "<b>Step 3 of 4:</b> Enter the <b>offer title</b> in English\n"
            "<i>Keep it clear and concise (max 200 characters)</i>",
            parse_mode="HTML",
        )

    async def _wizard_handle_title_en(
        self, message: Any, session: Any, user_id: int, now: datetime
    ) -> None:
        title = (message.text or "").strip()
        if not title or len(title) > 200:
            await message.reply_text(
                "⚠️ Title must be 1–200 characters. Try again:"
            )
            return
        await self._wizard_advance(
            session, user_id, now,
            new_state=AdminState.WIZARD_TITLE_HI,
            extra_payload={"title_en": title},
        )
        await message.reply_text(
            f"🧭 <b>New Offer Wizard</b>\n\n"
            f"✅ English title: <i>{title}</i>\n\n"
            "🌐 <b>Optional: Hindi translation</b>\n"
            "Type the Hindi title, or skip to go straight to the referral link.\n"
            "<i>Adding translations helps reach more users.</i>",
            parse_mode="HTML",
            reply_markup=_skip_keyboard("wizard_skip_translations", "⏭ Skip translations →"),
        )

    async def _wizard_handle_title_hi(
        self, message: Any, session: Any, user_id: int, now: datetime
    ) -> None:
        title_hi = (message.text or "").strip()
        if not title_hi or len(title_hi) > 200:
            await message.reply_text(
                "⚠️ Hindi title must be 1–200 characters. "
                "Try again, or tap <b>Skip</b> below.",
                parse_mode="HTML",
                reply_markup=_skip_keyboard("wizard_skip_translations", "⏭ Skip translations →"),
            )
            return
        await self._wizard_advance(
            session, user_id, now,
            new_state=AdminState.WIZARD_TITLE_GU,
            extra_payload={"title_hi": title_hi},
        )
        await message.reply_text(
            f"🧭 <b>New Offer Wizard</b>\n\n"
            f"✅ Hindi title: <i>{title_hi}</i>\n\n"
            "🌐 <b>Optional: Gujarati translation</b>\n"
            "Type the Gujarati title, or skip to the referral link.",
            parse_mode="HTML",
            reply_markup=_skip_keyboard("wizard_skip_gu", "⏭ Skip Gujarati →"),
        )

    async def _wizard_handle_title_gu(
        self, message: Any, session: Any, user_id: int, now: datetime
    ) -> None:
        title_gu = (message.text or "").strip()
        if not title_gu or len(title_gu) > 200:
            await message.reply_text(
                "⚠️ Gujarati title must be 1–200 characters. "
                "Try again, or tap <b>Skip</b> below.",
                parse_mode="HTML",
                reply_markup=_skip_keyboard("wizard_skip_gu", "⏭ Skip Gujarati →"),
            )
            return
        await self._wizard_advance(
            session, user_id, now,
            new_state=AdminState.WIZARD_LINK,
            extra_payload={"title_gu": title_gu},
        )
        await message.reply_text(
            f"🧭 <b>New Offer Wizard</b>\n\n"
            f"✅ Gujarati title: <i>{title_gu}</i>\n\n"
            "<b>Step 4 of 4:</b> Enter the <b>referral link</b>\n"
            "Must start with <code>https://</code>",
            parse_mode="HTML",
        )

    async def _wizard_handle_link(
        self, message: Any, session: Any, user_id: int, now: datetime
    ) -> None:
        link = (message.text or "").strip()
        if not is_valid_url(link):
            await message.reply_text(
                "⚠️ That doesn't look like a valid public HTTPS URL.\n\n"
                "Example: <code>https://bank.example/apply?ref=123</code>\n\nTry again:",
                parse_mode="HTML",
            )
            return
        allowed = set(self.settings.allowed_referral_domains)
        if allowed:
            host = urlsplit(link).hostname or ""
            if not any(host == d or host.endswith(f".{d}") for d in allowed):
                domains = ", ".join(f"<code>{d}</code>" for d in sorted(allowed))
                await message.reply_text(
                    f"⚠️ Domain not on the allowlist.\n\nAllowed: {domains}",
                    parse_mode="HTML",
                )
                return
        await self._wizard_advance(
            session, user_id, now,
            new_state=AdminState.WIZARD_CONFIRM,
            extra_payload={"referral_link": link},
        )
        p = {**session.payload, "referral_link": link}
        svc = self.catalog.get(p["service_type"])
        offer = Offer(
            service_type=p["service_type"],
            provider=p["provider"],
            title_en=p["title_en"],
            referral_link=link,
            title_hi=p.get("title_hi"),
            title_gu=p.get("title_gu"),
        )

        # Show multi-language preview if translations were added
        langs_to_preview = [Language.ENGLISH]
        if p.get("title_hi"):
            langs_to_preview.append(Language.HINDI)
        if p.get("title_gu"):
            langs_to_preview.append(Language.GUJARATI)

        if len(langs_to_preview) > 1:
            preview = "\n\n──────────\n\n".join(
                render_offer(offer, svc, lang) for lang in langs_to_preview
            )
        else:
            preview = render_offer(offer, svc, Language.ENGLISH)

        await message.reply_text(
            f"👁 <b>Preview — does this look right?</b>\n\n{preview}",
            parse_mode="HTML",
            reply_markup=_wizard_preview_keyboard(),
            link_preview_options=_NO_PREVIEW,
        )

    async def _wizard_advance(
        self,
        session: Any,
        user_id: int,
        now: datetime,
        *,
        new_state: AdminState,
        extra_payload: dict[str, Any],
    ) -> None:
        nonce = session.payload.get("_nonce", secrets.token_urlsafe(24))
        await self.repository.save_session(SessionRecord(
            telegram_user_id=user_id,
            state=new_state.value,
            nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
            payload={**session.payload, **extra_payload},
            created_at=session.created_at,
            updated_at=now,
            expires_at=now + timedelta(minutes=self.settings.session_ttl_minutes),
        ))

    async def _set_blocked(self, update: Any, context: Any, *, blocked: bool) -> None:
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        _, message = authorized
        if len(context.args) != 1:
            verb = "block" if blocked else "unblock"
            await message.reply_text(
                f"Usage: <code>/{verb} &lt;telegram_user_id&gt;</code>\n\n"
                "Tip: find a user's numeric ID in the /audit log.",
                parse_mode="HTML",
            )
            return
        try:
            target = int(context.args[0])
        except ValueError:
            await message.reply_text("⚠️ Telegram user ID must be a number.")
            return
        if target in self.settings.admin_ids:
            await message.reply_text("⚠️ Configured administrators cannot be blocked.")
            return
        verb = "block" if blocked else "unblock"
        action_label = "Block" if blocked else "Unblock"
        callback_data = f"confirm_block:{target}" if blocked else f"confirm_unblock:{target}"
        await message.reply_text(
            f"⚠️ <b>{action_label} user <code>{target}</code>?</b>\n\n"
            + ("They will no longer be able to use the bot."
               if blocked else "They will regain access to the bot."),
            parse_mode="HTML",
            reply_markup=_confirm_keyboard(callback_data, f"✅ Yes, {verb}"),
        )

    async def _queue_offers(
        self, offers: Any, *, user_id: int, now: datetime
    ) -> tuple[int, int]:
        queued = 0
        duplicates = 0
        for offer in offers:
            try:
                await self.repository.create_offer(
                    offer,
                    created_by=user_id,
                    scheduled_at=now,
                    now=now,
                )
            except DuplicateOfferError:
                duplicates += 1
            else:
                queued += 1
        await self.repository.record_audit_event(
            actor_user_id=user_id,
            action="offer.import",
            target_type="offer_batch",
            target_id="telegram",
            result="success",
            safe_details={"queued": queued, "duplicates": duplicates},
            now=now,
        )
        return queued, duplicates

    async def _require_admin(self, update: Any) -> tuple[int, Any] | None:
        user_id, message = self._identity(update)
        now = datetime.now(UTC)
        user = await self.repository.touch_user(
            user_id,
            is_admin=user_id in self.settings.admin_ids,
            now=now,
        )
        if user_id not in self.settings.admin_ids or user.blocked:
            await message.reply_text(_NOT_ADMIN)
            return None
        return user_id, message

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids

    @staticmethod
    def _identity(update: Any) -> tuple[int, Any]:
        if update.effective_user is None or update.effective_message is None:
            raise ValueError("Telegram update missing user or message")
        return int(update.effective_user.id), update.effective_message
