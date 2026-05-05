from config_schema import config_manager
from templates import OfferData, template_engine, _escape


def test_escape_basic():
    assert _escape("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"
    assert _escape("a & b") == "a &amp; b"
    assert _escape(None) == ""
    assert _escape("") == ""


def test_html_special_chars_in_provider_are_escaped():
    cfg = config_manager.get_service_config("credit_card")
    offer = OfferData(
        service_type="credit_card",
        provider="<script>alert(1)</script>",
        title_en="Cashback",
        referral_link="https://example.com/r",
        icon=cfg.icon,
    )
    rendered = template_engine.render(offer, cfg)
    # Raw <script> must not survive into the message.
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_icon_from_service_config_is_used():
    cfg = config_manager.get_service_config("credit_card")
    offer = OfferData(
        service_type="credit_card",
        provider="HDFC",
        title_en="t",
        referral_link="https://example.com/r",
        icon=cfg.icon,
    )
    rendered = template_engine.render(offer, cfg)
    assert cfg.icon in rendered  # 💳 in this case


def test_render_falls_back_to_english_title():
    cfg = config_manager.get_service_config("credit_card")
    offer = OfferData(
        service_type="credit_card",
        provider="HDFC",
        title_en="EnglishOnlyTitle",
        title_hi=None,
        referral_link="https://example.com/r",
        icon=cfg.icon,
    )
    out = template_engine.render(offer, cfg)
    assert "EnglishOnlyTitle" in out
