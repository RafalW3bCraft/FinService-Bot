"""Unit tests for BotHandlers covering the updated UX flows."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from finservice_bot.bot.handlers import BotHandlers
from finservice_bot.bot.states import AdminState
from finservice_bot.config import ServiceCatalog
from finservice_bot.settings import Settings
from finservice_bot.storage.schema import SessionRecord


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.document = None
        self.replies: list[str] = []
        self.reply_markups: list[object] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)
        self.reply_markups.append(kwargs.get("reply_markup"))

    async def reply_document(self, **kwargs):
        self.replies.append(kwargs.get("filename", ""))

    async def reply_chat_action(self, action: str) -> None:
        pass


class FakeRepository:
    def __init__(self) -> None:
        self.users: dict = {}
        self.sessions: dict = {}
        self.offers: list = []
        self.deleted: list = []

    async def touch_user(self, user_id, *, is_admin, now):
        user = SimpleNamespace(blocked=False, role="admin" if is_admin else "user")
        self.users[user_id] = user
        return user

    async def get_user(self, user_id):
        return self.users.get(user_id)

    async def save_session(self, session):
        self.sessions[session.telegram_user_id] = session

    async def get_session(self, user_id, *, now):
        return self.sessions.get(user_id)

    async def delete_session(self, user_id):
        self.sessions.pop(user_id, None)

    async def create_offer(self, offer, **kwargs):
        self.offers.append(offer)
        return SimpleNamespace(offer_id=f"offer-{len(self.offers)}")

    async def delete_user_data(self, user_id, *, now):
        self.deleted.append(user_id)
        self.users.pop(user_id, None)

    async def record_audit_event(self, **kwargs):
        return "audit-1"


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_settings(**overrides) -> Settings:
    base = {
        "TELEGRAM_BOT_TOKEN": "123:TEST",
        "ADMIN_IDS": "42",
        "ALLOWED_REFERRAL_DOMAINS": "bank.example",
    }
    base.update(overrides)
    return Settings.from_mapping(base)


def update(user_id: int, text: str = ""):
    message = FakeMessage(text)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
    ), message


def context(*args: str):
    return SimpleNamespace(args=list(args), bot=SimpleNamespace())


def _awaiting_row_session(user_id: int) -> SessionRecord:
    now = datetime(2026, 7, 22, tzinfo=UTC)
    nonce = secrets.token_urlsafe(24)
    return SessionRecord(
        telegram_user_id=user_id,
        state=AdminState.AWAITING_OFFER_ROW.value,
        nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
        payload={},
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(minutes=30),
    )


# ── Tests: access control ──────────────────────────────────────────────────────

async def test_non_admin_cannot_start_offer_workflow():
    repository = FakeRepository()
    handlers = BotHandlers(make_settings(), repository, ServiceCatalog.load("config/services.yaml"))
    incoming, message = update(7)

    await handlers.add_offer(incoming, context())

    assert "administrator" in message.replies[-1].lower()
    assert repository.sessions == {}


# ── Tests: /add_offer — guided wizard entry point ─────────────────────────────

async def test_add_offer_shows_service_keyboard_to_admin():
    repository = FakeRepository()
    handlers = BotHandlers(make_settings(), repository, ServiceCatalog.load("config/services.yaml"))
    incoming, message = update(42)

    await handlers.add_offer(incoming, context())

    assert "step 1 of 4" in message.replies[-1].lower()
    assert "service" in message.replies[-1].lower()
    # Keyboard with service buttons should be attached
    assert message.reply_markups[-1] is not None


# ── Tests: legacy CSV row path (AWAITING_OFFER_ROW session) ───────────────────

async def test_admin_queues_offer_via_csv_row_session():
    repository = FakeRepository()
    # Pre-seed a session in the legacy AWAITING_OFFER_ROW state
    repository.sessions[42] = _awaiting_row_session(42)

    handlers = BotHandlers(make_settings(), repository, ServiceCatalog.load("config/services.yaml"))
    row_update, row_message = update(
        42,
        "credit_card,Example Bank,Welcome offer,https://bank.example/apply",
    )
    await handlers.text_input(row_update, context())

    assert "queued" in row_message.replies[-1].lower()
    assert len(repository.offers) == 1
    assert repository.sessions == {}


async def test_invalid_domain_rejected_in_csv_row_session():
    repository = FakeRepository()
    repository.sessions[42] = _awaiting_row_session(42)

    handlers = BotHandlers(make_settings(), repository, ServiceCatalog.load("config/services.yaml"))
    row_update, row_message = update(
        42,
        "credit_card,Example Bank,Welcome offer,https://evil.example/apply",
    )
    await handlers.text_input(row_update, context())

    # Session must still exist (not deleted on failure)
    assert 42 in repository.sessions
    # The reply should contain a validation error
    assert row_message.replies


# ── Tests: /delete_me — confirmation flow ────────────────────────────────────

async def test_delete_me_shows_confirmation_keyboard():
    repository = FakeRepository()
    handlers = BotHandlers(make_settings(), repository, ServiceCatalog.load("config/services.yaml"))
    incoming, message = update(7)

    await handlers.delete_me(incoming, context())

    # Should show a confirmation prompt, NOT delete immediately
    assert repository.deleted == []
    assert message.replies
    assert "delete" in message.replies[-1].lower()
    # A keyboard should be attached for the Yes/Cancel buttons
    assert message.reply_markups[-1] is not None


async def test_delete_me_confirmation_keyboard_has_confirm_button():
    from telegram import InlineKeyboardMarkup

    repository = FakeRepository()
    handlers = BotHandlers(make_settings(), repository, ServiceCatalog.load("config/services.yaml"))
    incoming, message = update(7)

    await handlers.delete_me(incoming, context())

    keyboard = message.reply_markups[-1]
    assert isinstance(keyboard, InlineKeyboardMarkup)
    buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    callback_datas = {btn.callback_data for btn in buttons}
    assert "confirm_delete" in callback_datas
    assert "cancel_action" in callback_datas
