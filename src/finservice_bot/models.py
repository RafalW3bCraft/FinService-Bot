"""Typed domain models shared by validation, rendering, and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


SERVICE_KEYS = (
    "credit_card",
    "loan_personal",
    "loan_business",
    "loan_home",
    "bank_account_savings",
    "bank_account_current",
    "credit_builder",
    "insurance_health",
    "insurance_vehicle",
    "insurance_pa",
    "demat_account",
    "investment_mutual_fund",
    "investment_fixed_income",
)


class Language(str, Enum):
    ENGLISH = "en"
    HINDI = "hi"
    GUJARATI = "gu"


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    key: str
    channel_id: str
    language_mode: str
    names: dict[Language, str]
    icon: str
    default_language: Language = Language.ENGLISH
    enabled: bool = True
    rate_limit_per_hour: int = 10
    disclaimer_profile: str = "financial_referral"

    def display_name(self, language: Language) -> str:
        return self.names.get(language) or self.names[Language.ENGLISH]


@dataclass(frozen=True, slots=True)
class Offer:
    service_type: str
    provider: str
    title_en: str
    referral_link: str
    title_hi: str | None = None
    title_gu: str | None = None
    description_en: str | None = None
    description_hi: str | None = None
    description_gu: str | None = None
    validity: str | None = None
    terms: str | None = None

    def localized(self, field: str, language: Language) -> str:
        localized = getattr(self, f"{field}_{language.value}", None)
        return localized or getattr(self, f"{field}_en", None) or ""
