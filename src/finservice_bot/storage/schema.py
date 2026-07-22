"""Typed domain record schemas for the SQLite repository."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from finservice_bot.models import SERVICE_KEYS, Offer


SCHEMA_VERSION = 1


class OfferStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    REVIEW_REQUIRED = "review_required"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class OfferRecord:
    offer_id: str
    offer: Offer
    status: OfferStatus
    scheduled_at: datetime
    created_by: int
    created_at: datetime
    updated_at: datetime
    fingerprint: str
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    attempt_count: int = 0
    last_error_code: str | None = None
    telegram_channel_id: str | None = None
    telegram_message_id: int | None = None
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.offer_id or not self.fingerprint:
            raise ValueError("offer_id and fingerprint are required")
        if self.offer.service_type not in SERVICE_KEYS:
            raise ValueError(f"Unknown service type: {self.offer.service_type}")
        if self.attempt_count < 0:
            raise ValueError("attempt_count must not be negative")
        for name in ("scheduled_at", "created_at", "updated_at"):
            _require_aware(name, getattr(self, name))
        for name in ("claim_expires_at", "published_at"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(name, value)


@dataclass(frozen=True, slots=True)
class UserRecord:
    telegram_user_id: int
    role: str
    blocked: bool
    created_at: datetime
    last_seen_at: datetime
    display_language: str = "en"
    privacy_deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.role not in {"user", "admin"}:
            raise ValueError("role must be user or admin")
        _require_aware("created_at", self.created_at)
        _require_aware("last_seen_at", self.last_seen_at)
        if self.privacy_deleted_at is not None:
            _require_aware("privacy_deleted_at", self.privacy_deleted_at)


@dataclass(frozen=True, slots=True)
class ServiceRoute:
    service_key: str
    channel_id: str
    enabled: bool
    language_mode: str
    rate_limit_per_hour: int
    updated_at: datetime
    updated_by: int
    verified_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.service_key not in SERVICE_KEYS:
            raise ValueError(f"Unknown service key: {self.service_key}")
        if not self.channel_id.startswith("@") or len(self.channel_id) < 2:
            raise ValueError("channel_id must start with @")
        if self.language_mode not in {"single", "multi", "rotating"}:
            raise ValueError("invalid language_mode")
        if self.rate_limit_per_hour <= 0:
            raise ValueError("rate_limit_per_hour must be positive")
        _require_aware("updated_at", self.updated_at)
        if self.verified_at is not None:
            _require_aware("verified_at", self.verified_at)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    telegram_user_id: int
    state: str
    nonce_hash: str
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.state or not self.nonce_hash:
            raise ValueError("state and nonce_hash are required")
        try:
            encoded = json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("session payload must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > 20_000:
            raise ValueError("session payload exceeds 20000 bytes")
        for name in ("created_at", "updated_at", "expires_at"):
            _require_aware(name, getattr(self, name))


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
