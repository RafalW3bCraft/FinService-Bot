import pytest

from finservice_bot.models import Offer
from finservice_bot.validation import CSVValidator, is_valid_url, offer_fingerprint


VALID_CSV = (
    "service_type,provider,title_en,referral_link\n"
    "credit_card,HDFC,Cashback,https://offers.example.com/ref/a\n"
)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/ref",
        "https://offers.example.com/path?q=campaign",
        "https://xn--bcher-kva.example/path",
    ],
)
def test_public_https_referral_urls_are_accepted(url):
    assert is_valid_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://example.com/ref",
        "ftp://example.com/ref",
        "https://localhost/ref",
        "https://127.0.0.1/ref",
        "https://10.0.0.1/ref",
        "https://user:password@example.com/ref",
        "https://example.com/ref#other-destination",
        "not a url",
    ],
)
def test_unsafe_referral_urls_are_rejected(url):
    assert not is_valid_url(url)


def test_http_can_be_allowed_only_when_explicitly_requested():
    assert is_valid_url("http://example.com/ref", require_https=False)


def test_offer_fingerprint_is_stable_across_tracking_queries():
    first = Offer(
        service_type="credit_card",
        provider="Example Bank",
        title_en="Welcome",
        referral_link="https://bank.example/apply?campaign=one",
    )
    second = Offer(
        service_type="credit_card",
        provider="example bank",
        title_en="Different copy",
        referral_link="https://bank.example/apply?campaign=two",
    )

    assert offer_fingerprint(first) == offer_fingerprint(second)


def test_valid_csv_is_accepted():
    result = CSVValidator().validate_text(VALID_CSV)

    assert result.valid
    assert len(result.offers) == 1
    assert result.offers[0].provider == "HDFC"


def test_http_csv_link_has_explicit_https_error():
    content = VALID_CSV.replace("https://", "http://")

    result = CSVValidator().validate_text(content)

    assert not result.valid
    assert any("HTTPS" in error.message for error in result.errors)


def test_duplicate_rows_are_skipped_with_warning():
    content = VALID_CSV + "credit_card,HDFC,Cashback,https://offers.example.com/ref/a\n"

    result = CSVValidator().validate_text(content)

    assert result.valid
    assert len(result.offers) == 1
    assert any("Duplicate" in warning for warning in result.warnings)


def test_csv_byte_limit_is_checked_before_parsing():
    result = CSVValidator(max_bytes=20).validate_text(VALID_CSV)

    assert not result.valid
    assert result.offers == ()
    assert any("bytes" in error.message for error in result.errors)


def test_csv_row_limit_is_enforced():
    content = VALID_CSV + "loan_home,Provider,Home loan,https://example.com/ref/b\n"

    result = CSVValidator(max_rows=1).validate_text(content)

    assert not result.valid
    assert any("rows" in error.message for error in result.errors)


def test_generated_template_round_trips_to_two_valid_offers():
    validator = CSVValidator()

    result = validator.validate_text(validator.generate_template())

    assert result.valid
    assert len(result.offers) == 2


def test_allowed_domains_are_enforced():
    validator = CSVValidator(allowed_domains={"provider.example"})

    accepted = validator.validate_text(
        VALID_CSV.replace("offers.example.com", "apply.provider.example")
    )
    rejected = validator.validate_text(VALID_CSV)

    assert accepted.valid
    assert not rejected.valid
    assert any("approved domain" in error.message for error in rejected.errors)
