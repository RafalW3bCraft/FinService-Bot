"""Render provider-approved offer content as safe Telegram HTML."""

from __future__ import annotations

from html import escape

from .models import Language, Offer, ServiceConfig

# Telegram hard limit for sendMessage text payload
MAX_TELEGRAM_MESSAGE_BYTES = 4096


class MessageTooLongError(ValueError):
    """Raised when a rendered message exceeds Telegram's 4096-byte limit."""


DISCLAIMERS = {
    Language.ENGLISH: "Referral link; not financial advice. Review provider terms before applying.",
    Language.HINDI: "रेफरल लिंक; वित्तीय सलाह नहीं। आवेदन से पहले प्रदाता की शर्तें देखें।",
    Language.GUJARATI: "રેફરલ લિંક; નાણાકીય સલાહ નથી. અરજી પહેલાં પ્રદાતાની શરતો તપાસો.",
}

LABELS = {
    Language.ENGLISH: ("Provider", "Offer", "Details", "Apply", "Valid until", "Terms"),
    Language.HINDI: ("प्रदाता", "ऑफ़र", "विवरण", "आवेदन", "मान्य अवधि", "शर्तें"),
    Language.GUJARATI: ("પ્રદાતા", "ઓફર", "વિગતો", "અરજી", "માન્યતા", "શરતો"),
}


def render_offer(offer: Offer, service: ServiceConfig, language: Language) -> str:
    provider_label, offer_label, details_label, apply_label, validity_label, terms_label = (
        LABELS[language]
    )
    title = offer.localized("title", language)
    description = offer.localized("description", language)

    lines = [
        f"{escape(service.icon)} <b>{escape(service.display_name(language))}</b>",
        "",
        f"<b>{provider_label}:</b> <code>{escape(offer.provider)}</code>",
        f"<b>{offer_label}:</b> {escape(title)}",
    ]
    if description:
        lines.append(f"<b>{details_label}:</b> {escape(description)}")
    lines.extend(
        [
            f'<b>{apply_label}:</b> <a href="{escape(offer.referral_link, quote=True)}">'
            f"Open provider page</a>",
            f"<b>{validity_label}:</b> {escape(offer.validity or 'Check provider page')}",
            f"<b>{terms_label}:</b> {escape(offer.terms or 'Provider terms apply')}",
            "",
            f"<i>{escape(DISCLAIMERS[language])}</i>",
        ]
    )
    return "\n".join(lines)


def render_message(offer: Offer, service: ServiceConfig, rotation_index: int = 0) -> str:
    """Render the full Telegram message for an offer.

    Raises:
        MessageTooLongError: if the rendered text exceeds Telegram's 4096-byte
            limit. The publisher catches this and marks the offer as FAILED
            with error_code ``"message_too_long"`` so operators can correct it.
    """
    if service.language_mode == "single":
        text = render_offer(offer, service, service.default_language)
    elif service.language_mode == "rotating":
        languages = tuple(Language)
        text = render_offer(offer, service, languages[rotation_index % len(languages)])
    else:
        sections = [render_offer(offer, service, Language.ENGLISH)]
        if offer.title_hi:
            sections.append(render_offer(offer, service, Language.HINDI))
        if offer.title_gu:
            sections.append(render_offer(offer, service, Language.GUJARATI))
        text = "\n\n──────────\n\n".join(sections)

    if len(text.encode("utf-8")) > MAX_TELEGRAM_MESSAGE_BYTES:
        raise MessageTooLongError(
            f"Rendered message exceeds {MAX_TELEGRAM_MESSAGE_BYTES} bytes "
            f"(got {len(text.encode('utf-8'))}). Shorten the offer descriptions."
        )
    return text

