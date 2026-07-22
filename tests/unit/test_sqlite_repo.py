from datetime import UTC, datetime, timedelta
import pytest
import aiosqlite

from finservice_bot.config import ServiceCatalog
from finservice_bot.models import Offer
from finservice_bot.storage.sqlite_repo import SqliteRepository, DuplicateOfferError
from finservice_bot.storage.schema import OfferStatus, SessionRecord


@pytest.fixture
def catalog():
    return ServiceCatalog.load("config/services.yaml")


@pytest.mark.asyncio
async def test_sqlite_repository_lifecycle(tmp_path, catalog):
    db_file = tmp_path / "test.db"
    admin_ids = frozenset({42, 100})
    
    # 1. Initialize
    repo = SqliteRepository(db_file, catalog, admin_ids)
    await repo.connect()
    
    # Check schema version
    ver = await repo.schema_version()
    assert ver == 1
    
    # Check routes were bootstrapped
    routes = await repo.list_routes()
    assert len(routes) > 0
    assert any(r.service_key == "credit_card" for r in routes)
    
    # Check admin users touched
    user42 = await repo.get_user(42)
    assert user42 is not None
    assert user42.role == "admin"
    
    # 2. Touch and Get User
    now = datetime.now(UTC)
    regular_user = await repo.touch_user(123, is_admin=False, now=now)
    assert regular_user.role == "user"
    assert regular_user.blocked is False
    
    # Block user
    await repo.set_user_blocked(123, blocked=True, now=now)
    updated_user = await repo.get_user(123)
    assert updated_user.blocked is True
    
    # 3. Sessions
    session = SessionRecord(
        telegram_user_id=123,
        state="awaiting_input",
        nonce_hash="hash123",
        payload={"step": 1},
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    await repo.save_session(session)
    
    fetched_session = await repo.get_session(123, now=now)
    assert fetched_session is not None
    assert fetched_session.state == "awaiting_input"
    assert fetched_session.payload == {"step": 1}
    
    # Delete session
    await repo.delete_session(123)
    assert await repo.get_session(123, now=now) is None
    
    # 4. Offers
    offer = Offer(
        service_type="credit_card",
        provider="TestBank",
        title_en="Cool Card",
        referral_link="https://bank.example/apply",
    )
    
    created_offer = await repo.create_offer(offer, created_by=42, scheduled_at=now, now=now)
    assert created_offer.offer_id is not None
    assert created_offer.status == OfferStatus.QUEUED
    
    # Test Duplicate Prevention
    with pytest.raises(DuplicateOfferError):
        await repo.create_offer(offer, created_by=42, scheduled_at=now, now=now)
        
    # Claim Offer
    claimed = await repo.claim_due_offers(now=now + timedelta(seconds=1), limit=1, lock_ttl=timedelta(minutes=5))
    assert len(claimed) == 1
    assert claimed[0].offer_id == created_offer.offer_id
    assert claimed[0].status == OfferStatus.PUBLISHING
    assert claimed[0].claim_token is not None
    
    # Finalize Success
    success = await repo.finalize_published(
        claimed[0].offer_id,
        claim_token=claimed[0].claim_token,
        channel_id="@testchannel",
        message_id=999,
        now=now,
    )
    assert success is True
    
    finalized_offer = await repo.get_offer(created_offer.offer_id)
    assert finalized_offer.status == OfferStatus.PUBLISHED
    assert finalized_offer.telegram_channel_id == "@testchannel"
    assert finalized_offer.telegram_message_id == 999
    
    # 5. Route Operations
    route = await repo.get_route("credit_card")
    assert route is not None
    
    updated_route = await repo.set_route(
        "credit_card",
        channel_id="@newchannel",
        enabled=False,
        verified_at=now,
        updated_by=42,
        now=now,
    )
    assert updated_route.channel_id == "@newchannel"
    assert updated_route.enabled is False
    
    # 6. Rate Limit Slot Reservation
    res1 = await repo.reserve_publication_slot("credit_card", now=now, limit=1)
    assert res1 is True
    res2 = await repo.reserve_publication_slot("credit_card", now=now, limit=1)
    assert res2 is False # Limit hit
    
    # 7. Audit Logging
    audit_id = await repo.record_audit_event(
        actor_user_id=42,
        action="test.action",
        target_type="system",
        target_id=None,
        result="success",
        safe_details={"meta": "data"},
        now=now,
    )
    assert audit_id is not None
    
    audit_events = await repo.list_audit_events(limit=5)
    assert len(audit_events) >= 1
    assert audit_events[0]["action"] == "test.action"
    
    # Close
    await repo.close()
