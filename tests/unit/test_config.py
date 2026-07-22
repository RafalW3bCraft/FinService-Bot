from pathlib import Path

import pytest

from finservice_bot.config import CatalogError, ServiceCatalog
from finservice_bot.models import Language


CATALOG_PATH = Path(__file__).parents[2] / "config" / "services.yaml"


def test_catalog_contains_all_financial_service_categories():
    catalog = ServiceCatalog.load(CATALOG_PATH)

    assert catalog.keys() == (
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


def test_catalog_exposes_localized_names_and_channel_metadata():
    service = ServiceCatalog.load(CATALOG_PATH).get("credit_card")

    assert service.display_name(Language.ENGLISH) == "Credit Cards"
    assert service.display_name(Language.HINDI) == "क्रेडिट कार्ड"
    assert service.display_name(Language.GUJARATI) == "ક્રેડિટ કાર્ડ"
    assert service.channel_id.startswith("@")
    assert service.language_mode == "multi"
    assert service.disclaimer_profile == "financial_referral"
    assert not hasattr(service, "requires_kyc")


def test_catalog_fails_on_unknown_service(tmp_path):
    config = tmp_path / "services.yaml"
    config.write_text(
        "services:\n  unknown:\n    channel_id: '@test'\n"
        "    language_mode: single\n    default_language: en\n"
        "    display_name: {en: Test}\n",
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="unknown"):
        ServiceCatalog.load(config)


def test_catalog_fails_on_missing_file(tmp_path):
    with pytest.raises(CatalogError, match="does not exist"):
        ServiceCatalog.load(tmp_path / "missing.yaml")
