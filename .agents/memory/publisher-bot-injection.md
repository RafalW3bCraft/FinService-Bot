---
name: Publisher bot injection pattern
description: Publisher no longer stores bot as a mutable attribute; bot is injected at publish_due() call time
---

## Rule
`Publisher.__init__` does **not** accept a `bot` argument. Pass `bot=context.bot` (or equivalent) as a keyword argument to `publish_due(bot=..., now=...)`.

**Why:** A mutable `self.bot = None` set at construction and injected at job runtime raises `AttributeError` if `publish_due()` is ever called before the first job fires (e.g. in tests or future webhook mode). Passing bot at call time is explicit and safe.

**How to apply:** Anywhere a Publisher is constructed (cli.py, tests) — remove `bot=` from constructor. Anywhere `publish_due` is called — add `bot=<bot_instance>`. Test fixture `publisher()` in test_publisher.py takes only `repository` now.
