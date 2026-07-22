import pytest

from finservice_bot.config import ServiceCatalog
from finservice_bot.models import Language, Offer
from finservice_bot.rendering import render_message, render_offer


@pytest.fixture
def service():
    return ServiceCatalog.load("config/services.yaml").get("credit_card")


def make_offer(**overrides):
    values = {
        "service_type": "credit_card",
        "provider": "HDFC Bank",
        "title_en": "Cashback",
        "title_hi": "कैशबैक",
        "title_gu": "કેશબેક",
        "description_en": "Provider-approved offer",
        "description_hi": None,
        "description_gu": None,
        "referral_link": "https://example.com/ref",
        "validity": "31 Dec 2026",
        "terms": "Eligibility applies",
    }
    values.update(overrides)
    return Offer(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "<b>Injected</b>"),
        ("title_en", "A & B"),
        ("description_en", "<a href='https://evil.example'>click</a>"),
        ("validity", "<code>forever</code>"),
        ("terms", 'x\" onclick=\"alert(1)'),
    ],
)
def test_rendering_escapes_every_dynamic_text_field(service, field, value):
    rendered = render_offer(make_offer(**{field: value}), service, Language.ENGLISH)

    assert value not in rendered


def test_rendering_escapes_link_attribute(service):
    offer = make_offer(referral_link='https://example.com/ref?campaign=a\"b')

    rendered = render_offer(offer, service, Language.ENGLISH)

    assert 'campaign=a\"b' not in rendered
    assert "&quot;" in rendered


def test_rendering_uses_service_icon(service):
    assert service.icon in render_offer(make_offer(), service, Language.ENGLISH)


def test_rendering_falls_back_to_english(service):
    rendered = render_offer(
        make_offer(title_hi=None, description_hi=None),
        service,
        Language.HINDI,
    )

    assert "Cashback" in rendered
    assert "Provider-approved offer" in rendered


def test_multi_language_mode_includes_available_languages(service):
    rendered = render_message(make_offer(), service)

    assert "Cashback" in rendered
    assert "कैशबैक" in rendered
    assert "કેશબેક" in rendered


def test_disclaimer_does_not_claim_no_personal_data_is_stored(service):
    rendered = render_offer(make_offer(), service, Language.ENGLISH)

    assert "No PII stored" not in rendered
    assert "not financial advice" in rendered.lower()
