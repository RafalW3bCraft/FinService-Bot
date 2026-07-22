from datetime import UTC, datetime

import pytest

from finservice_bot.models import Offer
from finservice_bot.storage.schema import (
    OfferRecord,
    OfferStatus,
    ServiceRoute,
    SessionRecord,
    UserRecord,
)


NOW = datetime(2026, 7, 18, 6, 30, tzinfo=UTC)


def offer() -> Offer:
    return Offer(
        service_type="credit_card",
        provider="Example Bank",
        title_en="Welcome offer",
        referral_link="https://bank.example/apply",
        terms="Provider terms apply",
    )


def test_offer_record_requires_known_service_type():
    with pytest.raises(ValueError, match="Unknown service type"):
        OfferRecord(
            offer_id="offer-1",
            offer=Offer(
                service_type="unknown_type",
                provider="Bank",
                title_en="Title",
                referral_link="https://bank.example/apply",
            ),
            status=OfferStatus.QUEUED,
            scheduled_at=NOW,
            created_by=42,
            created_at=NOW,
            updated_at=NOW,
            fingerprint="abc123",
        )


def test_offer_record_rejects_negative_attempt_count():
    with pytest.raises(ValueError, match="attempt_count"):
        OfferRecord(
            offer_id="offer-1",
            offer=offer(),
            status=OfferStatus.QUEUED,
            scheduled_at=NOW,
            created_by=42,
            created_at=NOW,
            updated_at=NOW,
            fingerprint="abc123",
            attempt_count=-1,
        )


def test_offer_record_requires_timezone_aware_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        OfferRecord(
            offer_id="offer-1",
            offer=offer(),
            status=OfferStatus.QUEUED,
            scheduled_at=datetime(2026, 7, 18),
            created_by=42,
            created_at=NOW,
            updated_at=NOW,
            fingerprint="abc123",
        )


def test_user_record_rejects_invalid_role():
    with pytest.raises(ValueError, match="role"):
        UserRecord(
            telegram_user_id=42,
            role="superuser",
            blocked=False,
            created_at=NOW,
            last_seen_at=NOW,
        )


def test_user_record_stores_only_minimal_fields():
    record = UserRecord(
        telegram_user_id=42,
        role="admin",
        blocked=False,
        created_at=NOW,
        last_seen_at=NOW,
        display_language="en",
    )

    fields = {f.name for f in record.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert "username" not in fields
    assert "phone_number" not in fields
    assert "name" not in fields


def test_service_route_rejects_invalid_channel_id():
    with pytest.raises(ValueError, match="channel_id"):
        ServiceRoute(
            service_key="credit_card",
            channel_id="no-at-sign",
            enabled=True,
            language_mode="multi",
            rate_limit_per_hour=10,
            updated_at=NOW,
            updated_by=42,
        )


def test_service_route_rejects_invalid_language_mode():
    with pytest.raises(ValueError, match="language_mode"):
        ServiceRoute(
            service_key="credit_card",
            channel_id="@Fin_CC_Offers",
            enabled=True,
            language_mode="unknown",
            rate_limit_per_hour=10,
            updated_at=NOW,
            updated_by=42,
        )


def test_session_record_rejects_oversized_payload():
    with pytest.raises(ValueError, match="payload"):
        SessionRecord(
            telegram_user_id=42,
            state="awaiting_provider",
            nonce_hash="abc123",
            payload={"value": "x" * 20_001},
            created_at=NOW,
            updated_at=NOW,
            expires_at=NOW,
        )


def test_session_record_requires_state_and_nonce():
    with pytest.raises(ValueError, match="state"):
        SessionRecord(
            telegram_user_id=42,
            state="",
            nonce_hash="abc123",
            payload={},
            created_at=NOW,
            updated_at=NOW,
            expires_at=NOW,
        )
