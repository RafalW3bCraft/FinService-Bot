"""Validate referral destinations and bulk offer imports."""

from __future__ import annotations

import csv
import hashlib
import ipaddress
from dataclasses import dataclass
from io import StringIO
from urllib.parse import urlsplit

from .models import SERVICE_KEYS, Offer


REQUIRED_COLUMNS = ("service_type", "provider", "title_en", "referral_link")
OPTIONAL_COLUMNS = (
    "title_hi",
    "title_gu",
    "description_en",
    "description_hi",
    "description_gu",
    "validity",
    "terms",
)
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


@dataclass(frozen=True, slots=True)
class ValidationError:
    row: int
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    offers: tuple[Offer, ...]
    errors: tuple[ValidationError, ...]
    warnings: tuple[str, ...]


def is_valid_url(url: str, *, require_https: bool = True) -> bool:
    if not url or url != url.strip() or any(character.isspace() for character in url):
        return False
    try:
        parsed = urlsplit(url)
        allowed_schemes = {"https"} if require_https else {"https", "http"}
        if parsed.scheme.lower() not in allowed_schemes:
            return False
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            return False
        hostname = parsed.hostname
        if not hostname or hostname.lower() == "localhost" or hostname.lower().endswith(".local"):
            return False
        hostname.encode("idna")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return "." in hostname and not hostname.startswith(".") and not hostname.endswith(".")
        return address.is_global
    except (UnicodeError, ValueError):
        return False


class CSVValidator:
    def __init__(
        self,
        *,
        max_rows: int = 1_000,
        max_bytes: int = 5 * 1024 * 1024,
        allowed_domains: set[str] | None = None,
    ) -> None:
        if max_rows <= 0 or max_bytes <= 0:
            raise ValueError("CSV limits must be greater than zero")
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.allowed_domains = {
            domain.strip().lower().lstrip(".")
            for domain in (allowed_domains or set())
            if domain.strip()
        }

    def validate_text(self, content: str) -> ValidationResult:
        if len(content.encode("utf-8")) > self.max_bytes:
            return self._failed(0, "file", f"CSV exceeds {self.max_bytes} bytes")

        errors: list[ValidationError] = []
        warnings: list[str] = []
        offers: list[Offer] = []
        fingerprints: set[str] = set()

        try:
            reader = csv.DictReader(StringIO(content))
            if not reader.fieldnames:
                return self._failed(1, "headers", "CSV headers are required")
            missing = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
            if missing:
                return self._failed(
                    1,
                    "headers",
                    f"Missing required columns: {', '.join(missing)}",
                )

            for index, row in enumerate(reader, start=2):
                if index - 1 > self.max_rows:
                    return self._failed(index, "file", f"CSV exceeds {self.max_rows} rows")
                offer = self._validate_row(row, index, errors)
                if offer is None:
                    continue
                fingerprint = offer_fingerprint(offer)
                if fingerprint in fingerprints:
                    warnings.append(f"Row {index}: Duplicate offer skipped")
                    continue
                fingerprints.add(fingerprint)
                offers.append(offer)
        except csv.Error as exc:
            return self._failed(0, "csv", f"CSV parsing error: {exc}")

        # valid=True when there are no errors, even if all rows were duplicates
        # (all-duplicate CSV previously returned valid=False with zero errors,
        # giving the caller a contradictory signal).
        return ValidationResult(
            valid=not errors,
            offers=tuple(offers),
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    def _validate_row(
        self,
        row: dict[str, str | None],
        row_number: int,
        errors: list[ValidationError],
    ) -> Offer | None:
        values = {key: (value or "").strip() for key, value in row.items() if key is not None}
        row_errors = [
            ValidationError(row_number, field, "Required value is missing")
            for field in REQUIRED_COLUMNS
            if not values.get(field)
        ]
        if row_errors:
            errors.extend(row_errors)
            return None

        service_type = values["service_type"]
        if service_type not in SERVICE_KEYS:
            errors.append(ValidationError(row_number, "service_type", "Unknown service type"))
            return None

        referral_link = values["referral_link"]
        if not is_valid_url(referral_link):
            errors.append(
                ValidationError(
                    row_number,
                    "referral_link",
                    "Referral link must be a valid public HTTPS URL",
                )
            )
            return None
        if self.allowed_domains and not self._domain_allowed(referral_link):
            errors.append(
                ValidationError(
                    row_number,
                    "referral_link",
                    "Referral link is not on an approved domain",
                )
            )
            return None

        provider = values["provider"]
        title_en = values["title_en"]
        if len(provider) > 120 or len(title_en) > 200:
            errors.append(
                ValidationError(row_number, "content", "Provider or title exceeds length limit")
            )
            return None

        return Offer(
            service_type=service_type,
            provider=provider,
            title_en=title_en,
            referral_link=referral_link,
            title_hi=_optional(values, "title_hi", 200),
            title_gu=_optional(values, "title_gu", 200),
            description_en=_optional(values, "description_en", 500),
            description_hi=_optional(values, "description_hi", 500),
            description_gu=_optional(values, "description_gu", 500),
            validity=_optional(values, "validity", 120),
            terms=_optional(values, "terms", 500),
        )

    def _domain_allowed(self, url: str) -> bool:
        hostname = (urlsplit(url).hostname or "").lower()
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in self.allowed_domains
        )

    def generate_template(self) -> str:
        rows = [
            ALL_COLUMNS,
            (
                "credit_card",
                "Example Bank",
                "Card welcome offer",
                "https://bank.example/apply/card",
                "कार्ड स्वागत ऑफ़र",
                "કાર્ડ સ્વાગત ઑફર",
                "Review eligibility and fees before applying",
                "आवेदन से पहले पात्रता और शुल्क देखें",
                "અરજી કરતા પહેલા પાત્રતા અને ફી તપાસો",
                "31 Dec 2026",
                "Provider terms apply",
            ),
            (
                "loan_home",
                "Example Lender",
                "Home finance information",
                "https://lender.example/apply/home",
                "होम फाइनेंस जानकारी",
                "હોમ ફાઇનાન્સ માહિતી",
                "Compare rates, fees, and repayment terms",
                "दर, शुल्क और भुगतान शर्तों की तुलना करें",
                "દર, ફી અને ચુકવણીની શરતો સરખાવો",
                "31 Dec 2026",
                "Subject to lender eligibility",
            ),
        ]
        output = StringIO()
        csv.writer(output, lineterminator="\n").writerows(rows)
        return output.getvalue()

    @staticmethod
    def _failed(row: int, field: str, message: str) -> ValidationResult:
        return ValidationResult(False, (), (ValidationError(row, field, message),), ())


def _optional(values: dict[str, str], key: str, limit: int) -> str | None:
    """Return the field value, silently truncated to ``limit`` characters.

    Returns ``None`` for empty/missing values so callers can use truthiness
    checks without worrying about empty strings.
    """
    value = values.get(key, "")
    return value[:limit] or None


def offer_fingerprint(offer: Offer) -> str:
    """Return a stable duplicate key without retaining tracking query parameters."""
    parsed = urlsplit(offer.referral_link)
    normalized = parsed._replace(query="", fragment="").geturl().lower()
    material = f"{offer.service_type}|{offer.provider.lower()}|{normalized}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
