import os
import sys
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from telegram import Bot
from telegram.error import (
    TelegramError, RetryAfter, TimedOut, NetworkError,
    BadRequest, Forbidden,
)

from db_layer import db_manager
from config_schema import config_manager
from templates import template_engine, OfferData

logger = logging.getLogger(__name__)


def _get_token() -> str:
    token = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Scheduler startup aborted: missing TELEGRAM_BOT_TOKEN / BOT_TOKEN")
        sys.exit(1)
    return token


class PostingScheduler:
    """Drains the offer queue. Used in-process by `main.py` and as a
    standalone CLI worker (`python scheduler.py --continuous`)."""

    # Posting-history retention window and prune cadence. Both are class
    # attributes so they can be tweaked without touching method bodies.
    HISTORY_KEEP_DAYS = 90
    PRUNE_INTERVAL_SECONDS = 24 * 3600

    def __init__(self, bot: Optional[Bot] = None):
        # Accept an injected Bot so the in-process scheduler can share the
        # same HTTP client as the Application; fall back to creating one
        # for standalone CLI use.
        self.bot = bot if bot is not None else Bot(_get_token())
        self.posted_count = 0
        self.failed_count = 0
        # Set to "now" so the first prune happens ~24h after startup, not
        # immediately on the first cycle.
        self._last_prune_ts: float = time.monotonic()

    def _maybe_prune_history(self) -> None:
        """Trim ``posting_history`` at most once every PRUNE_INTERVAL_SECONDS.
        Errors are swallowed and logged — pruning must never break a posting
        cycle."""
        now = time.monotonic()
        if now - self._last_prune_ts < self.PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune_ts = now
        try:
            removed = db_manager.prune_posting_history(keep_days=self.HISTORY_KEEP_DAYS)
            if removed:
                logger.info("Pruned %d posting_history row(s) older than %d days",
                            removed, self.HISTORY_KEEP_DAYS)
        except Exception:
            logger.exception("posting_history prune failed")

    @staticmethod
    def _row_to_offer(row: Tuple, icon: str = "📌") -> OfferData:
        return OfferData(
            service_type=row[1],
            provider=row[2],
            title_en=row[3],
            title_hi=row[4],
            title_gu=row[5],
            description_en=row[6],
            description_hi=row[7],
            description_gu=row[8],
            referral_link=row[9],
            validity=row[10],
            terms=row[11],
            icon=icon,
        )

    async def _send_with_retry(self, channel_id: str, message: str,
                               max_attempts: int = 3) -> None:
        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                await self.bot.send_message(
                    chat_id=channel_id,
                    text=message,
                    parse_mode="HTML",
                )
                return
            except RetryAfter as e:
                last_exc = e
                logger.warning("Telegram rate limit; sleeping %ss before retry", e.retry_after)
                await asyncio.sleep(float(e.retry_after) + 1.0)
            except (TimedOut, NetworkError) as e:
                last_exc = e
                logger.warning("Transient network error on attempt %d/%d: %s",
                               attempt + 1, max_attempts, e)
                await asyncio.sleep(2 ** attempt)
            except (BadRequest, Forbidden):
                raise
        if last_exc is not None:
            raise last_exc

    async def post_by_service(self, service_key: str) -> bool:
        row = db_manager.next_queued_by_service(service_key)
        if not row:
            return False

        offer_id = row[0]
        channel_id = row[12]

        service_config = config_manager.get_service_config(service_key)
        # Pass the service icon through so scheduled posts render with the
        # right emoji (was hard-coded to the OfferData default before).
        offer = self._row_to_offer(row, icon=service_config.icon)
        message = template_engine.render(offer, service_config)

        try:
            await self._send_with_retry(channel_id, message)
            db_manager.mark_posted(offer_id, success=True)
            self.posted_count += 1
            logger.info("Posted offer %s to %s (%s)", offer_id, channel_id, service_key)
            return True
        except TelegramError as e:
            db_manager.mark_posted(offer_id, success=False, error_message=str(e))
            self.failed_count += 1
            logger.warning("Failed to post offer %s to %s (%s): %s",
                           offer_id, channel_id, service_key, e)
            return True
        except Exception:
            db_manager.mark_posted(offer_id, success=False, error_message="internal error")
            self.failed_count += 1
            logger.exception("Unexpected error posting offer %s to %s", offer_id, channel_id)
            return True

    async def post_round_robin(self) -> int:
        services = config_manager.list_enabled_keys()
        posted_before = self.posted_count
        failed_before = self.failed_count
        attempted = 0

        # Cheap, idempotent: short-circuits unless the prune interval elapsed.
        self._maybe_prune_history()

        for service in services:
            if await self.post_by_service(service):
                attempted += 1

        ok = self.posted_count - posted_before
        fail = self.failed_count - failed_before
        empty = len(services) - attempted
        if ok or fail:
            logger.info("Cycle complete: services=%d posted=%d failed=%d empty=%d",
                        len(services), ok, fail, empty)
        else:
            logger.debug("Cycle complete: queue empty across %d services", len(services))
        return attempted

    async def run_continuous(self, interval_hours: float):
        interval_seconds = interval_hours * 3600
        logger.info("Standalone scheduler started, interval=%sh", interval_hours)
        while True:
            try:
                await self.post_round_robin()
            except Exception:
                logger.exception("Scheduler cycle failed")
            await asyncio.sleep(interval_seconds)


def main():
    import argparse
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description="FinReferrals Posting Scheduler")
    parser.add_argument("--continuous", action="store_true",
                        help="Run continuously at intervals")
    parser.add_argument("--interval", type=float,
                        default=float(os.environ.get("SCHED_INTERVAL_HOURS", "1.6")),
                        help="Interval in hours for continuous mode (env: SCHED_INTERVAL_HOURS)")
    args = parser.parse_args()

    interval = max(0.1, args.interval)
    if interval != args.interval:
        logger.warning("--interval=%s below minimum 0.1; clamping.", args.interval)

    scheduler = PostingScheduler()
    try:
        if args.continuous:
            asyncio.run(scheduler.run_continuous(interval))
        else:
            posted = asyncio.run(scheduler.post_round_robin())
            logger.info("One-shot scheduler run complete: posted=%d", posted)
    except Exception:
        logger.exception("Scheduler main failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
