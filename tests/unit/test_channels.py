from datetime import UTC, datetime
from types import SimpleNamespace

from telegram.error import TelegramError

from finservice_bot.services.channels import ChannelVerification, ChannelVerifier


NOW = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)


class FakeBot:
    def __init__(self, member):
        self.member = member

    async def get_me(self):
        return SimpleNamespace(id=99)

    async def get_chat_member(self, chat_id, user_id):
        assert chat_id == "@Offers"
        assert user_id == 99
        return self.member


async def test_administrator_with_post_permission_is_verified():
    member = SimpleNamespace(status="administrator", can_post_messages=True)

    result = await ChannelVerifier(FakeBot(member)).verify("@Offers", now=NOW)

    assert result == ChannelVerification(True, "verified", NOW)


async def test_member_without_post_permission_is_rejected():
    member = SimpleNamespace(status="member", can_post_messages=False)

    result = await ChannelVerifier(FakeBot(member)).verify("@Offers", now=NOW)

    assert not result.verified
    # reason is now a human-readable string shown directly to admins
    assert "posting rights" in result.reason
    assert result.verified_at is None


async def test_telegram_error_is_reported_without_exception_details():
    class ErrorBot(FakeBot):
        async def get_chat_member(self, chat_id, user_id):
            raise TelegramError("sensitive transport details")

    result = await ChannelVerifier(ErrorBot(None)).verify("@Offers", now=NOW)

    assert not result.verified
    # reason is now a human-readable string — must not leak the raw exception
    assert "sensitive transport details" not in result.reason
    assert "channel" in result.reason.lower() or "bot" in result.reason.lower()
