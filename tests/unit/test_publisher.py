from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from finservice_bot.config import ServiceCatalog
from finservice_bot.models import Offer
from finservice_bot.services.publisher import PublishReport, Publisher
from finservice_bot.storage.schema import OfferRecord, OfferStatus, ServiceRoute


NOW = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)


def record(*, attempt_count: int = 1) -> OfferRecord:
    return OfferRecord(
        offer_id="offer-1",
        offer=Offer(
            service_type="credit_card",
            provider="Example Bank",
            title_en="Welcome <bonus>",
            referral_link="https://bank.example/apply",
        ),
        status=OfferStatus.PUBLISHING,
        scheduled_at=NOW,
        created_by=42,
        created_at=NOW,
        updated_at=NOW,
        fingerprint="abc",
        claim_token="claim",
        claim_expires_at=NOW + timedelta(minutes=2),
        attempt_count=attempt_count,
    )


def route(*, enabled: bool = True, verified: bool = True) -> ServiceRoute:
    return ServiceRoute(
        service_key="credit_card",
        channel_id="@VerifiedOffers",
        enabled=enabled,
        language_mode="multi",
        rate_limit_per_hour=10,
        updated_at=NOW,
        updated_by=42,
        verified_at=NOW if verified else None,
    )


class FakeRepository:
    def __init__(self, claimed: tuple[OfferRecord, ...] = (record(),)) -> None:
        self.claimed = claimed
        self.route = route()
        self.published: list[dict[str, object]] = []
        self.failures: list[dict[str, object]] = []
        self.recent_publications = 0

    async def claim_due_offers(self, **kwargs):
        return self.claimed

    async def get_route(self, service_key: str):
        assert service_key == "credit_card"
        return self.route

    async def finalize_published(self, offer_id: str, **kwargs):
        self.published.append({"offer_id": offer_id, **kwargs})
        return True

    async def finalize_failure(self, offer_id: str, **kwargs):
        self.failures.append({"offer_id": offer_id, **kwargs})
        return True

    async def reserve_publication_slot(self, service_type: str, *, now, limit):
        assert service_type == "credit_card"
        assert limit == 10
        return self.recent_publications < limit


class FakeBot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(message_id=101)


def publisher(repository: FakeRepository, bot: FakeBot) -> Publisher:
    return Publisher(
        repository=repository,
        bot=bot,
        catalog=ServiceCatalog.load("config/services.yaml"),
        claim_ttl=timedelta(minutes=2),
        batch_size=10,
    )


async def test_publisher_sends_safe_html_and_finalizes_claim():
    repository = FakeRepository()
    bot = FakeBot()

    report = await publisher(repository, bot).publish_due(now=NOW)

    assert report == PublishReport(claimed=1, published=1, requeued=0, failed=0, review=0)
    assert bot.calls[0]["chat_id"] == "@VerifiedOffers"
    assert "&lt;bonus&gt;" in str(bot.calls[0]["text"])
    assert repository.published[0]["claim_token"] == "claim"
    assert repository.published[0]["message_id"] == 101


async def test_unverified_route_fails_without_calling_telegram():
    repository = FakeRepository()
    repository.route = route(verified=False)
    bot = FakeBot()

    report = await publisher(repository, bot).publish_due(now=NOW)

    assert report.failed == 1
    assert bot.calls == []
    assert repository.failures[0]["status"] is OfferStatus.FAILED
    assert repository.failures[0]["error_code"] == "route_unverified"


async def test_retry_after_requeues_with_server_delay():
    repository = FakeRepository()
    bot = FakeBot(RetryAfter(timedelta(seconds=30)))

    report = await publisher(repository, bot).publish_due(now=NOW)

    assert report.requeued == 1
    failure = repository.failures[0]
    assert failure["status"] is OfferStatus.QUEUED
    assert failure["retry_at"] == NOW + timedelta(seconds=30)
    assert failure["error_code"] == "telegram_retry_after"


async def test_timeout_is_review_required_to_prevent_duplicate_delivery():
    repository = FakeRepository()
    bot = FakeBot(TimedOut("uncertain delivery"))

    report = await publisher(repository, bot).publish_due(now=NOW)

    assert report.review == 1
    assert repository.failures[0]["status"] is OfferStatus.REVIEW_REQUIRED
    assert repository.failures[0]["error_code"] == "telegram_ambiguous"


async def test_permanent_telegram_error_is_failed_without_leaking_message():
    repository = FakeRepository()
    bot = FakeBot(Forbidden("bot token and channel details must not be stored"))

    report = await publisher(repository, bot).publish_due(now=NOW)

    assert report.failed == 1
    assert repository.failures[0]["status"] is OfferStatus.FAILED
    assert repository.failures[0]["error_code"] == "telegram_forbidden"


async def test_bad_request_is_permanent_even_though_it_is_a_network_error_subclass():
    repository = FakeRepository()
    bot = FakeBot(BadRequest("invalid channel content"))

    report = await publisher(repository, bot).publish_due(now=NOW)

    assert report.failed == 1
    assert repository.failures[0]["status"] is OfferStatus.FAILED
    assert repository.failures[0]["error_code"] == "telegram_bad_request"


async def test_service_hourly_rate_limit_requeues_without_sending():
    repository = FakeRepository()
    repository.recent_publications = 10
    bot = FakeBot()

    report = await publisher(repository, bot).publish_due(now=NOW)

    assert report.requeued == 1
    assert bot.calls == []
    assert repository.failures[0]["status"] is OfferStatus.QUEUED
    assert repository.failures[0]["error_code"] == "service_rate_limit"
    assert repository.failures[0]["retry_at"] == NOW + timedelta(hours=1)
