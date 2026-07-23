---
name: ValidationResult.valid semantics
description: valid=True when there are no errors, even if all rows were duplicates (offers tuple empty)
---

## Rule
`ValidationResult.valid` is `not errors` — it signals whether the CSV was **structurally valid and error-free**, not whether it produced any new offers. An all-duplicate upload is valid (no errors, empty offers tuple, some warnings).

**Why:** The old `bool(offers) and not errors` returned `valid=False` with zero errors when every row was a duplicate, giving callers a contradictory signal (no errors but marked invalid).

**How to apply:** Callers that want to know if new offers were queued should check `len(result.offers) > 0` separately from `result.valid`.
