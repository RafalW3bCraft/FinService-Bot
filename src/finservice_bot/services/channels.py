"""Verify that the bot can publish to configured Telegram channels."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from telegram.error import TelegramError

# Strict channel username pattern — matches Telegram's own rules:
# @ + letter + 4-31 alphanumeric/underscore characters = 5-32 chars total.
# This is also used by config.py for catalog validation to keep both in sync.
CHANNEL_ID_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{4,31}")

# Human-readable explanations for each failure mode shown directly to admins
_REASON_INVALID_ID = (
    "Channel ID is invalid. It must start with @ followed by 5–32 alphanumeric characters.\n"
    "Example: <code>@MyChannelName</code>"
)
_REASON_VERIFICATION_FAILED = (
    "Could not reach the channel. Make sure:\n"
    "• The channel username is correct\n"
    "• The bot has been added to the channel\n"
    "• The bot has posting rights (Administrator role)"
)
_REASON_CANNOT_POST = (
    "The bot is a member of the channel but does not have posting rights.\n"
    "Go to channel settings → Administrators → give the bot <b>Post Messages</b> permission."
)


class ChannelBot(Protocol):
    async def get_me(self) -> Any: ...
    async def get_chat_member(self, chat_id: str, user_id: int) -> Any: ...


@dataclass(frozen=True, slots=True)
class ChannelVerification:
    verified: bool
    reason: str
    verified_at: datetime | None


class ChannelVerifier:
    def __init__(self, bot: ChannelBot) -> None:
        self.bot = bot
        # Cache the bot's own user object — it never changes during a run
        self._bot_user: Any | None = None

    async def _get_bot_user(self) -> Any:
        if self._bot_user is None:
            self._bot_user = await self.bot.get_me()
        return self._bot_user

    async def verify(self, channel_id: str, *, now: datetime) -> ChannelVerification:
        if not CHANNEL_ID_RE.fullmatch(channel_id):
            return ChannelVerification(False, _REASON_INVALID_ID, None)
        try:
            bot_user = await self._get_bot_user()
            member = await self.bot.get_chat_member(channel_id, int(bot_user.id))
        except TelegramError:
            return ChannelVerification(False, _REASON_VERIFICATION_FAILED, None)

        # "creator" is PTB's term for channel owners; "administrator" for added admins
        can_post = member.status in {"administrator", "creator"} and bool(
            getattr(member, "can_post_messages", member.status == "creator")
        )
        if not can_post:
            return ChannelVerification(False, _REASON_CANNOT_POST, None)
        return ChannelVerification(True, "verified", now)
