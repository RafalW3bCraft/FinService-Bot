"""Claim and publish due offers to Telegram channels."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from finservice_bot.config import ServiceCatalog
from finservice_bot.rendering import render_message
from finservice_bot.storage.schema import OfferRecord, OfferStatus, ServiceRoute


class PublicationRepository(Protocol):
    async def claim_due_offers(
        self, *, now: datetime, limit: int, lock_ttl: timedelta
    ) -> tuple[OfferRecord, ...]: ...

    async def get_route(self, service_key: str) -> ServiceRoute | None: ...

    async def reserve_publication_slot(
        self, service_type: str, *, now: datetime, limit: int
    ) -> bool: ...

    async def finalize_published(
        self,
        offer_id: str,
        *,
        claim_token: str,
        channel_id: str,
        message_id: int,
        now: datetime,
    ) -> bool: ...

    async def finalize_failure(
        self,
        offer_id: str,
        *,
        claim_token: str,
        status: OfferStatus,
        error_code: str,
        now: datetime,
        retry_at: datetime | None = None,
    ) -> bool: ...


class TelegramSender(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class PublishReport:
    claimed: int = 0
    published: int = 0
    requeued: int = 0
    failed: int = 0
    review: int = 0


class Publisher:
    def __init__(
        self,
        *,
        repository: PublicationRepository,
        bot: TelegramSender | None,
        catalog: ServiceCatalog,
        claim_ttl: timedelta,
        batch_size: int,
        interval_seconds: int = 60,
    ) -> None:
        self.repository = repository
        self.bot = bot
        self.catalog = catalog
        self.claim_ttl = claim_ttl
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds

    async def publish_due(self, *, now: datetime) -> PublishReport:
        claims = await self.repository.claim_due_offers(
            now=now,
            limit=self.batch_size,
            lock_ttl=self.claim_ttl,
        )
        report = PublishReport(claimed=len(claims))
        for claim in claims:
            outcome = await self._publish_one(claim, now=now)
            report = replace(report, **{outcome: getattr(report, outcome) + 1})
        return report

    async def _publish_one(self, claim: OfferRecord, *, now: datetime) -> str:
        claim_token = claim.claim_token
        if not claim_token:
            return "review"

        route = await self.repository.get_route(claim.offer.service_type)
        if route is None or not route.enabled or route.verified_at is None:
            await self.repository.finalize_failure(
                claim.offer_id,
                claim_token=claim_token,
                status=OfferStatus.FAILED,
                error_code="route_unverified",
                now=now,
            )
            return "failed"

        has_capacity = await self.repository.reserve_publication_slot(
            claim.offer.service_type,
            now=now,
            limit=route.rate_limit_per_hour,
        )
        if not has_capacity:
            await self.repository.finalize_failure(
                claim.offer_id,
                claim_token=claim_token,
                status=OfferStatus.QUEUED,
                error_code="service_rate_limit",
                now=now,
                retry_at=now + timedelta(hours=1),
            )
            return "requeued"

        service = self.catalog.get(claim.offer.service_type)
        text = render_message(claim.offer, service, rotation_index=claim.attempt_count - 1)
        try:
            message = await self.bot.send_message(
                chat_id=route.channel_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except RetryAfter as exc:
            delay = exc.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            retry_at = now + timedelta(seconds=min(max(seconds, 1), 3_600))
            await self.repository.finalize_failure(
                claim.offer_id,
                claim_token=claim_token,
                status=OfferStatus.QUEUED,
                error_code="telegram_retry_after",
                now=now,
                retry_at=retry_at,
            )
            return "requeued"
        except Forbidden:
            await self.repository.finalize_failure(
                claim.offer_id,
                claim_token=claim_token,
                status=OfferStatus.FAILED,
                error_code="telegram_forbidden",
                now=now,
            )
            return "failed"
        except BadRequest:
            await self.repository.finalize_failure(
                claim.offer_id,
                claim_token=claim_token,
                status=OfferStatus.FAILED,
                error_code="telegram_bad_request",
                now=now,
            )
            return "failed"
        except (TimedOut, NetworkError):
            await self.repository.finalize_failure(
                claim.offer_id,
                claim_token=claim_token,
                status=OfferStatus.REVIEW_REQUIRED,
                error_code="telegram_ambiguous",
                now=now,
            )
            return "review"

        finalized = await self.repository.finalize_published(
            claim.offer_id,
            claim_token=claim_token,
            channel_id=route.channel_id,
            message_id=int(message.message_id),
            now=now,
        )
        return "published" if finalized else "review"
