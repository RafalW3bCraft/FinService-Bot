---
name: Channel verification error strings
description: ChannelVerifier.verify() returns human-readable reason strings, not machine codes
---

## Rule
`ChannelVerification.reason` is a **human-readable HTML string** shown directly to admins in the Telegram message. Tests must NOT assert `result.reason == "bot_cannot_post"` — assert on content instead (e.g. `"posting rights" in result.reason`).

**Why:** Machine codes like `"verification_failed"` were surfaced directly to admins with no explanation. Human-readable strings are now returned from `_REASON_*` constants in channels.py.

**How to apply:** When checking channel verification failure in tests, assert on substring presence rather than exact equality.
