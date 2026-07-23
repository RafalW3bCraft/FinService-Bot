"""Persistent administrator conversation states."""

from enum import StrEnum


class AdminState(StrEnum):
    # CSV row mode: admin pastes a single raw CSV row
    AWAITING_OFFER_ROW = "awaiting_offer_row"

    # Guided wizard — sequential text-input steps
    WIZARD_PROVIDER  = "wizard_provider"    # step 2: waiting for provider name
    WIZARD_TITLE_EN  = "wizard_title_en"    # step 3: waiting for English title
    WIZARD_TITLE_HI  = "wizard_title_hi"    # optional: waiting for Hindi title
    WIZARD_TITLE_GU  = "wizard_title_gu"    # optional: waiting for Gujarati title
    WIZARD_LINK      = "wizard_link"        # step 4: waiting for referral URL
    WIZARD_CONFIRM   = "wizard_confirm"     # preview shown, awaiting inline button
