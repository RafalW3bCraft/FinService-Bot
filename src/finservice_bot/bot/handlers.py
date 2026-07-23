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
    "Use /help to see what's available, or /privacy to learn how your data is handled."
)

_WELCOME_BLOCKED = "⛔ This account is not authorised to use this bot."

_HELP_PUBLIC = (
    "ℹ️ <b>Help</b>\n\n"
    "<b>Your account</b>\n"
    "/start — Open or restart the bot\n"
    "/privacy — How your data is stored and for how long\n"
    "/delete_me — Permanently erase your account\n"
    "/help — Show this message"
)

_HELP_ADMIN_EXTRA = (
    "\n\n<b>Offers</b>\n"
    "/add_offer — Add a new offer (guided, step-by-step)\n"
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

# ── Keyboard builders ─────────────────────────────────────────────────────────

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
            InlineKeyboardButton("❌ Cancel", callback_data="wizard_cancel"),
        ],
    ])


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
        user = await self.repository.touch_user(
            user_id,
            is_admin=user_id in self.settings.admin_ids,
            now=datetime.now(UTC),
        )
        if user.blocked:
            await message.reply_text(_WELCOME_BLOCKED)
            return
        await message.reply_text(_WELCOME, parse_mode="HTML")

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
        text = _HELP_PUBLIC
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
        current = getattr(user, "display_language", "en") or "en"
        await message.reply_text(
            "🌐 <b>Choose your language</b>\n\n"
            "Public-facing offer messages will be sent in your selected language "
            "wherever a translation is available.",
            parse_mode="HTML",
            reply_markup=_language_keyboard(current),
        )

    async def delete_me(self, update: Any, context: Any) -> None:
        del context
        _, message = self._identity(update)
        await message.reply_text(
            "⚠️ <b>Delete your account?</b>\n\n"
            "This will permanently erase your user profile, role, and any active "
            "workflow state. It cannot be undone.",
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
        await message.reply_text(
            "🧭 <b>New offer — step 1 of 4</b>\n\nSelect a service category:",
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
            await self._wizard_handle_title(message, session, user_id, now)
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
            errors = "\n".join(
                f"• Row {e.row} <code>{e.field}</code>: {e.message}"
                for e in result.errors[:10]
            )
            await message.reply_text(
                f"⚠️ <b>Validation failed</b>\n\n{errors}",
                parse_mode="HTML",
            )
            return
        now = datetime.now(UTC)
        queued, duplicates = await self._queue_offers(result.offers, user_id=user_id, now=now)
        parts = [f"✅ <b>{queued} offer(s) queued</b>"]
        if duplicates:
            parts.append(f"⏭ {duplicates} duplicate(s) skipped")
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
            caption="📄 Fill in each row and upload back here to queue your offers.",
        )

    async def setup_channels(self, update: Any, context: Any) -> None:
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        user_id, message = authorized
        if len(context.args) != 2:
            await message.reply_text(
                "Usage: <code>/setup_channels &lt;service_key&gt; &lt;@channel&gt;</code>\n\n"
                "Use /list_services to see valid service keys.",
                parse_mode="HTML",
            )
            return
        service_key, channel_id = context.args
        if service_key not in self.catalog.keys():
            await message.reply_text(
                f"⚠️ Unknown service key: <code>{service_key}</code>\n"
                "Use /list_services to see valid keys.",
                parse_mode="HTML",
            )
            return
        await message.reply_chat_action("typing")
        now = datetime.now(UTC)
        verification = await ChannelVerifier(context.bot).verify(channel_id, now=now)
        if not verification.verified or verification.verified_at is None:
            await message.reply_text(
                f"⚠️ <b>Channel verification failed</b>\n\n{verification.reason}\n\n"
                "Make sure the bot is a member of the channel with posting rights.",
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
            f"{svc.icon} {svc.display_name(Language.ENGLISH)} → {channel_id}",
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
        lines = ["🔌 <b>Service Routes</b>\n"]
        for key in self.catalog.keys():
            svc = self.catalog.get(key)
            route = routes.get(key)
            if route and route.enabled and route.verified_at:
                channel_line = f"📡 {route.channel_id} · ✅ Verified"
            elif route and route.channel_id:
                channel_line = f"📡 {route.channel_id} · ⚠️ Not verified"
            else:
                channel_line = "❌ Not configured"
            lang = _LANG_MODE_LABEL.get(svc.language_mode, svc.language_mode)
            lines.append(
                f"{svc.icon} <b>{svc.display_name(Language.ENGLISH)}</b>\n"
                f"   {channel_line} · {lang}"
            )
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
            await message.reply_text("📊 No offers on record yet.")
            return
        total = sum(counts.values())
        ordered = [
            "queued", "publishing", "published",
            "failed", "review_required", "archived", "draft",
        ]
        lines = ["📊 <b>Offer Statistics</b>\n"]
        for status in ordered:
            count = counts.get(status, 0)
            # Always show these three even if zero
            if count == 0 and status not in ("queued", "published", "failed"):
                continue
            icon = _STATUS_ICON.get(status, "•")
            label = status.replace("_", " ").title()
            lines.append(f"{icon}  {label:<22} <code>{count:>4}</code>")
        lines.append(f"\n<b>Total</b>                      <code>{total:>4}</code>")
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
            await message.reply_text("📋 No audit events recorded yet.")
            return
        lines = ["📋 <b>Audit Log</b> — last 20 events\n"]
        for event in events:
            label = _ACTION_LABEL.get(event["action"], event["action"])
            ts = event["created_at"].strftime("%d %b %Y, %H:%M")
            result_icon = "✅" if event["result"] == "success" else "❌"
            detail = ""
            raw = event.get("safe_details")
            if raw:
                try:
                    d = raw if isinstance(raw, dict) else json.loads(raw)
                    if "queued" in d:
                        detail = f" — {d['queued']} queued, {d.get('duplicates', 0)} skipped"
                    elif "channel" in d:
                        detail = f" — {d['channel']}"
                    elif "deleted" in d:
                        detail = f" — {d['deleted']} removed"
                except Exception:
                    pass
            lines.append(f"{result_icon} <b>{label}</b>{detail}")
            lines.append(f"     <i>{ts}</i>\n")
        await message.reply_text("\n".join(lines), parse_mode="HTML")

    async def prune(self, update: Any, context: Any) -> None:
        del context
        authorized = await self._require_admin(update)
        if authorized is None:
            return
        _, message = authorized
        await message.reply_text(
            "🧹 <b>Prune expired records?</b>\n\n"
            "This will delete expired sessions and old posting history. "
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
            await message.reply_text("✅ Workflow cancelled. Use /add_offer to start a new one.")
        else:
            await message.reply_text("✅ Cancelled.")

    # ── Callback query dispatcher ─────────────────────────────────────────────

    async def handle_callback_query(self, update: Any, context: Any) -> None:
        """Route inline keyboard callbacks via a dispatch table.

        Exact matches are looked up in ``_EXACT_HANDLERS``; prefix matches
        are iterated in order. Adding a new callback requires only a new
        ``_cb_*`` method and an entry in one of these two structures.
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
            "cancel_action":  self._cb_cancel_action,
            "confirm_delete": self._cb_confirm_delete,
            "confirm_prune":  self._cb_confirm_prune,
            "wizard_confirm": self._cb_wizard_confirm,
            "wizard_restart": self._cb_wizard_restart,
            "wizard_cancel":  self._cb_wizard_cancel,
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
        await query.edit_message_text("🗑 Your account data has been permanently erased.")

    async def _cb_confirm_prune(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        del data
        if not self._is_admin(user_id):
            await query.edit_message_text("🚫 Not authorised.")
            return
        deleted = await self.repository.prune_expired(now=now)
        await query.edit_message_text(f"✅ Pruned {deleted} expired record(s).")

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
                f"✅ User <code>{target}</code> has been blocked.", parse_mode="HTML"
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
                f"✅ User <code>{target}</code> has been unblocked.", parse_mode="HTML"
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
            f"🧭 <b>New offer — step 2 of 4</b>\n\n"
            f"Category: {svc.icon} <b>{svc.display_name(Language.ENGLISH)}</b>\n\n"
            "Enter the <b>provider name</b>\n"
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
            await query.edit_message_text("⚠️ Session expired. Use /add_offer to start again.")
            return
        p = session.payload
        offer = Offer(
            service_type=p["service_type"],
            provider=p["provider"],
            title_en=p["title_en"],
            referral_link=p["referral_link"],
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
            await query.edit_message_text(
                f"✅ <b>Offer queued successfully</b>\n\n"
                f"{svc.icon} <b>{svc.display_name(Language.ENGLISH)}</b>\n"
                f"Provider: {p['provider']}",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text("⏭ This offer already exists — duplicate skipped.")

    async def _cb_wizard_restart(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        del data
        if not self._is_admin(user_id):
            await query.edit_message_text("🚫 Not authorised.")
            return
        await self.repository.delete_session(user_id)
        await query.edit_message_text(
            "🧭 <b>New offer — step 1 of 4</b>\n\nSelect a service category:",
            parse_mode="HTML",
            reply_markup=_service_keyboard(self.catalog),
        )

    async def _cb_wizard_cancel(
        self, query: Any, user_id: int, data: str, now: datetime
    ) -> None:
        del user_id, data, now
        await query.edit_message_text("✅ Offer creation cancelled.")

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
            f"✅ Language set to <b>{name}</b>.",
            parse_mode="HTML",
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
            f"🧭 <b>Step 3 of 4</b>\n\n"
            f"Provider: <code>{provider}</code>\n\n"
            "Enter the <b>offer title</b> in English:",
            parse_mode="HTML",
        )

    async def _wizard_handle_title(
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
            new_state=AdminState.WIZARD_LINK,
            extra_payload={"title_en": title},
        )
        await message.reply_text(
            f"🧭 <b>Step 4 of 4</b>\n\n"
            f"Title: <code>{title}</code>\n\n"
            "Enter the <b>referral link</b> — must start with <code>https://</code>:",
            parse_mode="HTML",
        )

    async def _wizard_handle_link(
        self, message: Any, session: Any, user_id: int, now: datetime
    ) -> None:
        link = (message.text or "").strip()
        if not is_valid_url(link):
            await message.reply_text(
                "⚠️ That doesn't look like a valid public HTTPS URL.\n"
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
                    f"⚠️ Domain not on the allowlist. Allowed: {domains}",
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
        )
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
            f"⚠️ <b>{action_label} user <code>{target}</code>?</b>",
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
