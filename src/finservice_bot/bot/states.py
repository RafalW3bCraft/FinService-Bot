"""Persistent administrator conversation states."""

from enum import StrEnum


class AdminState(StrEnum):
    # Legacy: admin pastes or uploads a raw CSV row
    AWAITING_OFFER_ROW = "awaiting_offer_row"

    # Guided wizard — four sequential text-input steps
    WIZARD_PROVIDER  = "wizard_provider"   # step 2: waiting for provider name
    WIZARD_TITLE_EN  = "wizard_title_en"   # step 3: waiting for English title
    WIZARD_LINK      = "wizard_link"       # step 4: waiting for referral URL
    WIZARD_CONFIRM   = "wizard_confirm"    # preview shown, awaiting inline button
