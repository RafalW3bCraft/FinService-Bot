"""SQLite-backed repository for polling mode — no cloud credentials required."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from finservice_bot.config import ServiceCatalog
from finservice_bot.models import Offer
from finservice_bot.storage.schema import (
    SCHEMA_VERSION,
    OfferRecord,
    OfferStatus,
    ServiceRoute,
    SessionRecord,
    UserRecord,
)
from finservice_bot.validation import offer_fingerprint


class DuplicateOfferError(ValueError):
    """Raised when an active offer already owns the same fingerprint."""


_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    telegram_user_id   INTEGER PRIMARY KEY,
    role               TEXT    NOT NULL DEFAULT 'user',
    blocked            INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL,
    last_seen_at       TEXT    NOT NULL,
    display_language   TEXT    NOT NULL DEFAULT 'en',
    privacy_deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    telegram_user_id INTEGER PRIMARY KEY,
    state            TEXT    NOT NULL,
    nonce_hash       TEXT    NOT NULL,
    payload          TEXT    NOT NULL DEFAULT '{}',
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    expires_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
    offer_id            TEXT PRIMARY KEY,
    status              TEXT    NOT NULL DEFAULT 'draft',
    scheduled_at        TEXT    NOT NULL,
    created_by          INTEGER NOT NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    fingerprint         TEXT    NOT NULL,
    claim_token         TEXT,
    claim_expires_at    TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    last_error_code     TEXT,
    telegram_channel_id TEXT,
    telegram_message_id INTEGER,
    published_at        TEXT,
    offer_json          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS service_routes (
    service_key         TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL,
    enabled             INTEGER NOT NULL DEFAULT 1,
    language_mode       TEXT NOT NULL,
    rate_limit_per_hour INTEGER NOT NULL,
    updated_at          TEXT NOT NULL,
    updated_by          INTEGER NOT NULL,
    verified_at         TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER NOT NULL,
    action        TEXT    NOT NULL,
    target_type   TEXT,
    target_id     TEXT,
    result        TEXT    NOT NULL,
    safe_details  TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS posting_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id     TEXT NOT NULL,
    service_type TEXT NOT NULL,
    channel_id   TEXT,
    message_id   INTEGER,
    result       TEXT NOT NULL,
    attempt      INTEGER NOT NULL,
    error_code   TEXT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_rate_limits (
    id         TEXT PRIMARY KEY,
    count      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


class SqliteRepository:
    def __init__(
        self,
        db_path: str | Path,
        catalog: ServiceCatalog,
        admin_ids: frozenset[int],
    ) -> None:
        self.db_path = db_path
        self.catalog = catalog
        self.admin_ids = admin_ids
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_DDL)
        await self._db.commit()
        await self._db.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        await self._db.commit()
        await self.bootstrap(self.catalog, self.admin_ids, datetime.now(UTC))

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database is not connected. Call connect() first.")
        return self._db

    async def schema_version(self) -> int | None:
        cursor = await self.db.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        )
        row = await cursor.fetchone()
        return int(row["value"]) if row else None

    async def bootstrap(
        self,
        catalog: ServiceCatalog,
        admin_ids: frozenset[int],
        now: datetime,
    ) -> None:
        for key in catalog.keys():
            svc = catalog.get(key)
            await self.db.execute(
                """INSERT OR IGNORE INTO service_routes
                   (service_key, channel_id, enabled, language_mode, rate_limit_per_hour, updated_at, updated_by)
                   VALUES (?, ?, 1, ?, ?, ?, 0)""",
                (key, svc.channel_id, svc.language_mode, svc.rate_limit_per_hour, _dt(now)),
            )
        for uid in admin_ids:
            await self.db.execute(
                """INSERT INTO users
                   (telegram_user_id, role, created_at, last_seen_at)
                   VALUES (?, 'admin', ?, ?)
                   ON CONFLICT(telegram_user_id) DO UPDATE SET role = 'admin'""",
                (uid, _dt(now), _dt(now)),
            )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def touch_user(
        self,
        telegram_user_id: int,
        *,
        is_admin: bool,
        now: datetime,
    ) -> UserRecord:
        role = "admin" if is_admin else "user"
        await self.db.execute(
            """INSERT INTO users (telegram_user_id, role, created_at, last_seen_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
            (telegram_user_id, role, _dt(now), _dt(now)),
        )
        await self.db.commit()
        return await self.get_user(telegram_user_id)  # type: ignore[return-value]

    async def get_user(self, telegram_user_id: int) -> UserRecord | None:
        cursor = await self.db.execute(
            "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return UserRecord(
            telegram_user_id=row["telegram_user_id"],
            role=row["role"],
            blocked=bool(row["blocked"]),
            created_at=_parse_dt(row["created_at"]),
            last_seen_at=_parse_dt(row["last_seen_at"]),
            display_language=row["display_language"],
            privacy_deleted_at=_parse_dt(row["privacy_deleted_at"])
            if row["privacy_deleted_at"]
            else None,
        )

    async def set_display_language(
        self, telegram_user_id: int, *, language: str, now: datetime
    ) -> bool:
        cursor = await self.db.execute(
            "UPDATE users SET display_language = ?, last_seen_at = ? WHERE telegram_user_id = ?",
            (language, _dt(now), telegram_user_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def set_user_blocked(
        self, telegram_user_id: int, *, blocked: bool, now: datetime
    ) -> bool:
        cursor = await self.db.execute(
            "UPDATE users SET blocked = ?, last_seen_at = ? WHERE telegram_user_id = ?",
            (int(blocked), _dt(now), telegram_user_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def delete_user_data(self, telegram_user_id: int, *, now: datetime) -> None:
        await self.db.execute(
            "DELETE FROM sessions WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        await self.db.execute(
            """UPDATE users SET
               role = 'user',
               blocked = 0,
               display_language = 'en',
               privacy_deleted_at = ?
               WHERE telegram_user_id = ?""",
            (_dt(now), telegram_user_id),
        )
        await self.db.execute(
            """UPDATE offers SET
               status = 'archived',
               offer_json = '{}',
               updated_at = ?
               WHERE created_by = ? AND status IN ('draft', 'queued', 'publishing')""",
            (_dt(now), telegram_user_id),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def save_session(self, session: SessionRecord) -> None:
        await self.db.execute(
            """INSERT INTO sessions
               (telegram_user_id, state, nonce_hash, payload, created_at, updated_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(telegram_user_id) DO UPDATE SET
                   state = excluded.state,
                   nonce_hash = excluded.nonce_hash,
                   payload = excluded.payload,
                   created_at = excluded.created_at,
                   updated_at = excluded.updated_at,
                   expires_at = excluded.expires_at""",
            (
                session.telegram_user_id,
                session.state,
                session.nonce_hash,
                json.dumps(session.payload),
                _dt(session.created_at),
                _dt(session.updated_at),
                _dt(session.expires_at),
            ),
        )
        await self.db.commit()

    async def get_session(self, telegram_user_id: int, *, now: datetime) -> SessionRecord | None:
        cursor = await self.db.execute(
            "SELECT * FROM sessions WHERE telegram_user_id = ? AND expires_at > ?",
            (telegram_user_id, _dt(now)),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return SessionRecord(
            telegram_user_id=row["telegram_user_id"],
            state=row["state"],
            nonce_hash=row["nonce_hash"],
            payload=json.loads(row["payload"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            expires_at=_parse_dt(row["expires_at"]),
        )

    async def delete_session(self, telegram_user_id: int) -> None:
        await self.db.execute(
            "DELETE FROM sessions WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Offers
    # ------------------------------------------------------------------

    async def create_offer(
        self,
        offer: Offer,
        *,
        created_by: int,
        scheduled_at: datetime,
        now: datetime,
    ) -> OfferRecord:
        offer_id = secrets.token_hex(16)
        fp = offer_fingerprint(offer)
        cursor = await self.db.execute(
            "SELECT 1 FROM offers WHERE fingerprint = ? AND status IN (?, ?, ?)",
            (fp, OfferStatus.QUEUED.value, OfferStatus.PUBLISHING.value, OfferStatus.PUBLISHED.value),
        )
        if await cursor.fetchone():
            raise DuplicateOfferError("An active offer with this fingerprint already exists")

        payload = {
            "service_type": offer.service_type,
            "provider": offer.provider,
            "title_en": offer.title_en,
            "referral_link": offer.referral_link,
            "title_hi": offer.title_hi,
            "title_gu": offer.title_gu,
            "description_en": offer.description_en,
            "description_hi": offer.description_hi,
            "description_gu": offer.description_gu,
            "validity": offer.validity,
            "terms": offer.terms,
        }
        await self.db.execute(
            """INSERT INTO offers
               (offer_id, status, scheduled_at, created_by, created_at, updated_at, fingerprint, offer_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                offer_id,
                OfferStatus.QUEUED.value,
                _dt(scheduled_at),
                created_by,
                _dt(now),
                _dt(now),
                fp,
                json.dumps(payload),
            ),
        )
        await self.db.commit()
        return await self.get_offer(offer_id)  # type: ignore[return-value]

    async def get_offer(self, offer_id: str) -> OfferRecord | None:
        cursor = await self.db.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        )
        row = await cursor.fetchone()
        return _row_to_offer(row) if row else None

    async def claim_due_offers(
        self,
        *,
        now: datetime,
        limit: int,
        lock_ttl: timedelta,
    ) -> tuple[OfferRecord, ...]:
        cursor = await self.db.execute(
            """SELECT * FROM offers
               WHERE status = ? AND scheduled_at <= ?
               AND (claim_expires_at IS NULL OR claim_expires_at <= ?)
               ORDER BY scheduled_at ASC
               LIMIT ?""",
            (OfferStatus.QUEUED.value, _dt(now), _dt(now), limit),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            offer_id = row["offer_id"]
            token = secrets.token_hex(16)
            expires = now + lock_ttl
            attempts = row["attempt_count"] + 1
            await self.db.execute(
                """UPDATE offers SET
                   status = ?,
                   claim_token = ?,
                   claim_expires_at = ?,
                   attempt_count = ?,
                   updated_at = ?
                   WHERE offer_id = ?""",
                (OfferStatus.PUBLISHING.value, token, _dt(expires), attempts, _dt(now), offer_id),
            )
            updated = await self.get_offer(offer_id)
            if updated:
                results.append(updated)
        await self.db.commit()
        return tuple(results)

    async def finalize_published(
        self,
        offer_id: str,
        *,
        claim_token: str,
        channel_id: str,
        message_id: int,
        now: datetime,
    ) -> bool:
        cursor = await self.db.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        if row["status"] != OfferStatus.PUBLISHING.value or row["claim_token"] != claim_token:
            return False

        await self.db.execute(
            """UPDATE offers SET
               status = ?,
               telegram_channel_id = ?,
               telegram_message_id = ?,
               published_at = ?,
               updated_at = ?,
               claim_token = NULL,
               claim_expires_at = NULL,
               last_error_code = NULL
               WHERE offer_id = ?""",
            (OfferStatus.PUBLISHED.value, channel_id, message_id, _dt(now), _dt(now), offer_id),
        )
        await self.db.execute(
            """INSERT INTO posting_history
               (offer_id, service_type, channel_id, message_id, result, attempt, error_code, created_at, expires_at)
               VALUES (?, ?, ?, ?, 'published', ?, NULL, ?, ?)""",
            (
                offer_id,
                json.loads(row["offer_json"])["service_type"],
                channel_id,
                message_id,
                row["attempt_count"],
                _dt(now),
                _dt(now + timedelta(days=365)),
            ),
        )
        await self.db.commit()
        return True

    async def finalize_failure(
        self,
        offer_id: str,
        *,
        claim_token: str,
        status: OfferStatus,
        error_code: str,
        now: datetime,
        retry_at: datetime | None = None,
    ) -> bool:
        if status not in {OfferStatus.QUEUED, OfferStatus.FAILED, OfferStatus.REVIEW_REQUIRED}:
            raise ValueError("invalid publication failure status")
        if status is OfferStatus.QUEUED and retry_at is None:
            raise ValueError("retry_at is required when requeueing")

        cursor = await self.db.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        if row["status"] != OfferStatus.PUBLISHING.value or row["claim_token"] != claim_token:
            return False

        scheduled = _dt(retry_at) if retry_at else row["scheduled_at"]
        await self.db.execute(
            """UPDATE offers SET
               status = ?,
               scheduled_at = ?,
               updated_at = ?,
               claim_token = NULL,
               claim_expires_at = NULL,
               last_error_code = ?
               WHERE offer_id = ?""",
            (status.value, scheduled, _dt(now), error_code, offer_id),
        )
        await self.db.execute(
            """INSERT INTO posting_history
               (offer_id, service_type, channel_id, message_id, result, attempt, error_code, created_at, expires_at)
               VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?)""",
            (
                offer_id,
                json.loads(row["offer_json"])["service_type"],
                status.value,
                row["attempt_count"],
                error_code,
                _dt(now),
                _dt(now + timedelta(days=365)),
            ),
        )
        await self.db.commit()
        return True

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    async def list_routes(self) -> tuple[ServiceRoute, ...]:
        cursor = await self.db.execute(
            "SELECT * FROM service_routes ORDER BY service_key"
        )
        rows = await cursor.fetchall()
        return tuple(
            ServiceRoute(
                service_key=r["service_key"],
                channel_id=r["channel_id"],
                enabled=bool(r["enabled"]),
                language_mode=r["language_mode"],
                rate_limit_per_hour=r["rate_limit_per_hour"],
                updated_at=_parse_dt(r["updated_at"]),
                updated_by=r["updated_by"],
                verified_at=_parse_dt(r["verified_at"]) if r["verified_at"] else None,
            )
            for r in rows
        )

    async def get_route(self, service_key: str) -> ServiceRoute | None:
        cursor = await self.db.execute(
            "SELECT * FROM service_routes WHERE service_key = ?", (service_key,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return ServiceRoute(
            service_key=row["service_key"],
            channel_id=row["channel_id"],
            enabled=bool(row["enabled"]),
            language_mode=row["language_mode"],
            rate_limit_per_hour=row["rate_limit_per_hour"],
            updated_at=_parse_dt(row["updated_at"]),
            updated_by=row["updated_by"],
            verified_at=_parse_dt(row["verified_at"]) if row["verified_at"] else None,
        )

    async def set_route(
        self,
        service_key: str,
        *,
        channel_id: str,
        enabled: bool,
        verified_at: datetime | None,
        updated_by: int,
        now: datetime,
    ) -> ServiceRoute:
        current = await self.get_route(service_key)
        if current is None:
            raise KeyError(f"Unknown service route: {service_key}")
        await self.db.execute(
            """INSERT INTO service_routes
               (service_key, channel_id, enabled, language_mode, rate_limit_per_hour, updated_at, updated_by, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(service_key) DO UPDATE SET
                   channel_id = excluded.channel_id,
                   enabled = excluded.enabled,
                   verified_at = excluded.verified_at,
                   updated_by = excluded.updated_by,
                   updated_at = excluded.updated_at""",
            (
                service_key,
                channel_id,
                int(enabled),
                current.language_mode,
                current.rate_limit_per_hour,
                _dt(now),
                updated_by,
                _dt(verified_at) if verified_at else None,
            ),
        )
        await self.db.commit()
        return await self.get_route(service_key)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Rate Limits
    # ------------------------------------------------------------------

    async def reserve_publication_slot(
        self,
        service_type: str,
        *,
        now: datetime,
        limit: int,
    ) -> bool:
        if limit <= 0:
            raise ValueError("publication limit must be positive")
        window = now.astimezone(tz=now.tzinfo).replace(minute=0, second=0, microsecond=0)
        limit_id = f"{service_type}-{window:%Y%m%d%H}"
        cursor = await self.db.execute(
            "SELECT count FROM publication_rate_limits WHERE id = ?", (limit_id,)
        )
        row = await cursor.fetchone()
        count = row["count"] if row else 0
        if count >= limit:
            return False
        await self.db.execute(
            """INSERT INTO publication_rate_limits (id, count, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET count = count + 1, updated_at = ?""",
            (limit_id, count + 1, _dt(now), _dt(now)),
        )
        await self.db.commit()
        return True

    # ------------------------------------------------------------------
    # Audit / Stats
    # ------------------------------------------------------------------

    async def record_audit_event(
        self,
        *,
        actor_user_id: int,
        action: str,
        target_type: str | None,
        target_id: str | None,
        result: str,
        safe_details: dict[str, Any],
        now: datetime,
    ) -> str:
        cursor = await self.db.execute(
            """INSERT INTO audit_events
               (actor_user_id, action, target_type, target_id, result, safe_details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                actor_user_id,
                action,
                target_type,
                target_id,
                result,
                json.dumps(safe_details),
                _dt(now),
            ),
        )
        await self.db.commit()
        return str(cursor.lastrowid)

    async def offer_status_counts(self) -> dict[str, int]:
        cursor = await self.db.execute(
            "SELECT status, COUNT(*) AS n FROM offers GROUP BY status"
        )
        rows = await cursor.fetchall()
        return {r["status"]: r["n"] for r in rows}

    async def list_audit_events(self, *, limit: int = 20) -> tuple[dict[str, Any], ...]:
        cursor = await self.db.execute(
            "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return tuple(
            {
                "id": str(r["id"]),
                "actor_user_id": r["actor_user_id"],
                "action": r["action"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "result": r["result"],
                "safe_details": json.loads(r["safe_details"]),
                "created_at": _parse_dt(r["created_at"]),
            }
            for r in rows
        )

    async def prune_expired(self, *, now: datetime, limit: int = 400) -> int:
        cursor = await self.db.execute(
            "DELETE FROM sessions WHERE expires_at < ?", (_dt(now),)
        )
        deleted = cursor.rowcount
        cursor = await self.db.execute(
            "DELETE FROM posting_history WHERE expires_at < ?", (_dt(now),)
        )
        deleted += cursor.rowcount
        await self.db.commit()
        return deleted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _row_to_offer(row: aiosqlite.Row) -> OfferRecord:
    offer_payload = json.loads(row["offer_json"])
    offer = Offer(
        service_type=offer_payload["service_type"],
        provider=offer_payload["provider"],
        title_en=offer_payload["title_en"],
        referral_link=offer_payload["referral_link"],
        title_hi=offer_payload.get("title_hi"),
        title_gu=offer_payload.get("title_gu"),
        description_en=offer_payload.get("description_en"),
        description_hi=offer_payload.get("description_hi"),
        description_gu=offer_payload.get("description_gu"),
        validity=offer_payload.get("validity"),
        terms=offer_payload.get("terms"),
    )
    return OfferRecord(
        offer_id=row["offer_id"],
        offer=offer,
        status=OfferStatus(row["status"]),
        scheduled_at=_parse_dt(row["scheduled_at"]),
        created_by=row["created_by"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        fingerprint=row["fingerprint"],
        claim_token=row["claim_token"],
        claim_expires_at=_parse_dt(row["claim_expires_at"]) if row["claim_expires_at"] else None,
        attempt_count=row["attempt_count"],
        last_error_code=row["last_error_code"],
        telegram_channel_id=row["telegram_channel_id"],
        telegram_message_id=row["telegram_message_id"],
        published_at=_parse_dt(row["published_at"]) if row["published_at"] else None,
    )
