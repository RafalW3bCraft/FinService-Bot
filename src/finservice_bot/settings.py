"""Validated, side-effect-free runtime settings."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


class SettingsError(ValueError):
    """Raised when runtime settings are missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str = field(repr=False)
    admin_ids: frozenset[int]
    service_config_path: Path
    max_csv_bytes: int
    max_csv_rows: int
    session_ttl_minutes: int
    claim_ttl_seconds: int
    publish_batch_size: int
    publish_interval_seconds: int
    allowed_referral_domains: frozenset[str]

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> Settings:
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise SettingsError("TELEGRAM_BOT_TOKEN is required")

        admin_ids = _parse_admin_ids(values.get("ADMIN_IDS", ""))

        return cls(
            telegram_bot_token=token,
            admin_ids=admin_ids,
            service_config_path=_path(
                values, "SERVICE_CONFIG_PATH", Path("config/services.yaml")
            ),
            max_csv_bytes=_bounded_int(
                values, "MAX_CSV_BYTES", 5 * 1024 * 1024, maximum=20 * 1024 * 1024
            ),
            max_csv_rows=_bounded_int(values, "MAX_CSV_ROWS", 1_000, maximum=10_000),
            session_ttl_minutes=_bounded_int(
                values, "SESSION_TTL_MINUTES", 30, maximum=1_440
            ),
            claim_ttl_seconds=_bounded_int(
                values, "CLAIM_TTL_SECONDS", 120, maximum=3_600
            ),
            publish_batch_size=_bounded_int(
                values, "PUBLISH_BATCH_SIZE", 10, maximum=100
            ),
            publish_interval_seconds=_bounded_int(
                values, "PUBLISH_INTERVAL_SECONDS", 60, maximum=3_600
            ),
            allowed_referral_domains=_parse_domains(
                values.get("ALLOWED_REFERRAL_DOMAINS", "")
            ),
        )

    @classmethod
    def load(cls, env_file: Path | None = None) -> Settings:
        """Load an optional env file, then overlay the process environment."""
        values: dict[str, str] = {}
        if env_file is not None:
            if not env_file.is_file():
                raise SettingsError(f"Environment file does not exist: {env_file}")
            values.update(
                {k: v for k, v in dotenv_values(env_file).items() if v is not None}
            )
        values.update(os.environ)
        return cls.from_mapping(values)


def _parse_admin_ids(raw: str) -> frozenset[int]:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        raise SettingsError("ADMIN_IDS must contain at least one integer")
    try:
        identifiers = frozenset(int(part) for part in parts)
    except ValueError as exc:
        raise SettingsError("ADMIN_IDS must be a comma-separated list of integers") from exc
    if any(identifier <= 0 for identifier in identifiers):
        raise SettingsError("ADMIN_IDS must contain positive integers")
    return identifiers


def _bounded_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    maximum: int,
) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value <= 0:
        raise SettingsError(f"{name} must be greater than zero")
    if value > maximum:
        raise SettingsError(f"{name} must not exceed {maximum}")
    return value


def _path(values: Mapping[str, str], name: str, default: Path) -> Path:
    raw = values.get(name)
    if raw is None:
        return default
    normalized = raw.strip()
    if not normalized:
        raise SettingsError(f"{name} must not be blank")
    return Path(normalized)


def _parse_domains(raw: str) -> frozenset[str]:
    domains: set[str] = set()
    for item in raw.split(","):
        domain = item.strip().lower().lstrip(".")
        if not domain:
            continue
        if (
            "." not in domain
            or domain.startswith(".")
            or domain.endswith(".")
            or not re.fullmatch(r"[a-z0-9.-]+", domain)
        ):
            raise SettingsError("ALLOWED_REFERRAL_DOMAINS must contain valid domain names")
        domains.add(domain)
    return frozenset(domains)
