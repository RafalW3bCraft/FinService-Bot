"""Load and validate the immutable financial-service catalog."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .models import SERVICE_KEYS, Language, ServiceConfig
from .services.channels import CHANNEL_ID_RE


class CatalogError(ValueError):
    """Raised when the service catalog cannot be loaded safely."""


class ServiceCatalog:
    def __init__(self, services: Mapping[str, ServiceConfig]) -> None:
        self._services = dict(services)

    @classmethod
    def load(cls, path: str | Path) -> ServiceCatalog:
        catalog_path = Path(path)
        if not catalog_path.is_file():
            raise CatalogError(f"Service catalog does not exist: {catalog_path}")

        try:
            payload = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise CatalogError(f"Unable to read service catalog: {catalog_path}") from exc

        if not isinstance(payload, Mapping) or not isinstance(payload.get("services"), Mapping):
            raise CatalogError("Service catalog must contain a services mapping")

        raw_services = payload["services"]
        unknown = sorted(set(raw_services) - set(SERVICE_KEYS))
        if unknown:
            raise CatalogError(f"Unknown service key: {', '.join(unknown)}")

        missing = [key for key in SERVICE_KEYS if key not in raw_services]
        if missing:
            raise CatalogError(f"Missing service keys: {', '.join(missing)}")

        services = {
            key: _parse_service(key, raw_services[key])
            for key in SERVICE_KEYS
        }
        return cls(services)

    def get(self, key: str) -> ServiceConfig:
        try:
            return self._services[key]
        except KeyError as exc:
            raise CatalogError(f"Unknown service key: {key}") from exc

    def keys(self) -> tuple[str, ...]:
        return tuple(key for key in SERVICE_KEYS if key in self._services)

    def enabled(self) -> tuple[ServiceConfig, ...]:
        return tuple(self._services[key] for key in self.keys() if self._services[key].enabled)


def _parse_service(key: str, raw: Any) -> ServiceConfig:
    if not isinstance(raw, Mapping):
        raise CatalogError(f"Service {key} must be a mapping")

    channel_id = _required_text(raw, "channel_id", key)
    if not CHANNEL_ID_RE.fullmatch(channel_id):
        raise CatalogError(
            f"Service {key} channel_id must be a valid Telegram username "
            f"(@letter + 4–31 alphanumeric/underscore)"
        )

    language_mode = str(raw.get("language_mode", "single"))
    if language_mode not in {"single", "multi", "rotating"}:
        raise CatalogError(f"Service {key} has invalid language_mode: {language_mode}")

    names_raw = raw.get("display_name")
    if not isinstance(names_raw, Mapping) or not str(names_raw.get("en", "")).strip():
        raise CatalogError(f"Service {key} requires display_name.en")
    names = {
        language: str(names_raw[language.value]).strip()
        for language in Language
        if str(names_raw.get(language.value, "")).strip()
    }

    try:
        default_language = Language(str(raw.get("default_language", "en")))
    except ValueError as exc:
        raise CatalogError(f"Service {key} has invalid default_language") from exc

    rate_limit = raw.get("rate_limit_per_hour", 10)
    if isinstance(rate_limit, bool) or not isinstance(rate_limit, int) or rate_limit <= 0:
        raise CatalogError(f"Service {key} rate_limit_per_hour must be a positive integer")

    return ServiceConfig(
        key=key,
        channel_id=channel_id,
        language_mode=language_mode,
        names=names,
        icon=str(raw.get("icon", "📌")).strip() or "📌",
        default_language=default_language,
        enabled=bool(raw.get("enabled", True)),
        rate_limit_per_hour=rate_limit,
        disclaimer_profile=str(raw.get("disclaimer_profile", "financial_referral")),
    )


def _required_text(raw: Mapping[str, Any], name: str, service_key: str) -> str:
    value = str(raw.get(name, "")).strip()
    if not value:
        raise CatalogError(f"Service {service_key} requires {name}")
    return value
