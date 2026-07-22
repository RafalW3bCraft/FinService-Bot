"""Verify that the bot can publish to configured Telegram channels."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from telegram.error import TelegramError


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

    async def verify(self, channel_id: str, *, now: datetime) -> ChannelVerification:
        if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", channel_id):
            return ChannelVerification(False, "invalid_channel_id", None)
        try:
            bot_user = await self.bot.get_me()
            member = await self.bot.get_chat_member(channel_id, int(bot_user.id))
        except TelegramError:
            return ChannelVerification(False, "verification_failed", None)

        can_post = member.status in {"administrator", "creator", "owner"} and bool(
            getattr(member, "can_post_messages", member.status in {"creator", "owner"})
        )
        if not can_post:
            return ChannelVerification(False, "bot_cannot_post", None)
        return ChannelVerification(True, "verified", now)
