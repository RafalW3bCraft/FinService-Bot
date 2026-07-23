---
name: delete_user_data privacy erasure race condition
description: Offers in publishing status must have claim_token nulled but JSON preserved during privacy deletion
---

## Rule
`delete_user_data` in sqlite_repo.py uses **two separate UPDATE statements**:
1. Archive draft+queued offers AND zero their offer_json (privacy erasure)
2. Archive publishing offers, null claim_token+claim_expires_at, but **leave offer_json intact**

**Why:** Zeroing offer_json for publishing offers then having _row_to_offer parse `'{}'` raises KeyError on `service_type`. Since the publisher's `finalize_published` validates `claim_token` before writing, nulling the token is sufficient to prevent the offer from being finalized — the JSON can stay for integrity. Draft/queued offers are safe to zero because they haven't been claimed.

**How to apply:** Never zero offer_json for rows with status='publishing'. The claim_token null is the safety mechanism, not the JSON zeroing.
