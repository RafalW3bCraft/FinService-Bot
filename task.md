# FinService-Bot — Full Audit & Remediation Task Plan
> Generated: 2026-04-23 | Language: Python 3.11 | Framework: python-telegram-bot ≥22
> Target: Replit Agent implementation

---

## 🔴 OVERALL RATING: 5.5 / 10

| Dimension | Score | Verdict |
|---|---|---|
| Security | 3/10 | Live credentials leaked; HTML injection; file-server risk |
| Performance | 6/10 | Per-op DB connections; N+1 stats query; no WAL |
| Code Quality | 6/10 | Good structure but missing type hints, session TTL, icon bug |
| Error Handling | 7/10 | Solid retry logic; weak edge-case coverage elsewhere |
| Maintainability | 6/10 | Missing tests, no migration system, obs.py ghost |
| Best Practices | 5/10 | Multiple SOLID violations; direct private-symbol imports |

---

## 🚨 PHASE 0 — IMMEDIATE SECURITY REMEDIATION (Do First)

### Task 0.1 — Revoke & Rotate All Exposed Secrets
**Issue:** `.env` was packaged into the zip. The following live credentials are fully exposed:
```
TELEGRAM_BOT_TOKEN=8595888851:AAEOsaVoz6_9pI9ChHNVkhckVkiKKiPvNOY
ADMIN_IDS=7342964534, 818019562
SESSION_SECRET=D8M/mR0DMa5fyTnlJIdEioYUE9GQOjSryjY+QUjog7BD0L1KA08vpWU0r6DLyuN0fx4X+ioHGuhDEzlzzkHh9Q==
```
**Actions:**
- [ ] Immediately revoke the Telegram Bot Token via `@BotFather` → `/revoke`
- [ ] Generate a new token and store it **only** in Replit Secrets (env vars), never in a file
- [ ] Create `.env.example` with placeholder values for documentation
- [ ] Confirm `.env` is in `.gitignore` (✅ already present) and verify it is NOT tracked in git history via `git log --all -- .env`
- [ ] If `.env` appears in git history: run `git filter-branch` or BFG Repo Cleaner to purge it

---

## 🔴 PHASE 1 — SECURITY VULNERABILITIES

### Task 1.1 — HTML Injection via Unsanitized User Input
**File:** `templates.py`, `admin_commands.py`
**Issue:** User-supplied text (`provider`, `title_en`, descriptions) is inserted directly into `parse_mode=HTML` messages. A provider name containing `<script>`, `</b>`, or `&amp;` will break rendering or could be weaponized.

**Fix — add to `templates.py`:**
```python
import html

def _escape(value: Optional[str]) -> str:
    return html.escape(value) if value else ""
```
Apply `_escape()` to all user-supplied fields in `render_single_language()` before template substitution. Fields that should not be escaped (already trusted HTML): none — all are user-supplied.

**Acceptance criteria:** Sending `<b>HDFC</b>` as provider name should display literally, not render bold.

---

### Task 1.2 — Health Check Server File Exposure Risk
**File:** `main.py` → `HealthCheckHandler(http.server.SimpleHTTPRequestHandler)`
**Issue:** `SimpleHTTPRequestHandler` inherits `do_HEAD()` which serves filesystem metadata. The `do_GET` override is correct but `do_HEAD` is not overridden. An attacker probing `HEAD /requirements.txt` could confirm file existence and path structure.

**Fix:**
```python
class HealthCheckHandler(http.server.BaseHTTPRequestHandler):  # Use BaseHTTP, not SimpleHTTP
    def do_GET(self):
        ...
    def do_HEAD(self):
        self.send_response(405)
        self.end_headers()
    def log_message(self, format, *args):
        pass
```

---

### Task 1.3 — Admin Session Hijack via Callback Spoofing
**File:** `admin_commands.py` → `handle_callback()`
**Issue:** Callback data is validated only at the `if data.startswith(...)` level. Session state (`session["state"]`) is not cross-checked against the callback type. Example: if a user has an open `add_offer` session and somehow receives a `confirm_yes` callback, it will try to process `session["offer"]` which may not exist yet, raising `KeyError`.

**Fix:** Add explicit `session.get("offer")` guard before accessing it:
```python
if data == "confirm_yes" and session.get("state") == "confirm":
    if "offer" not in session:
        await query.edit_message_text("Session expired. Start again.")
        del self.user_sessions[uid]
        return
    ...
```

---

### Task 1.4 — Rate Limiting — No Per-User Command Throttle
**Issue:** Any non-blocked user can spam `/add_offer` or document uploads indefinitely, exhausting memory via session accumulation and DB writes.

**Fix:** Add a simple in-memory rate limiter using a `dict[int, float]` keyed by user_id storing last-command timestamp:
```python
# In AdminCommands.__init__:
self._last_command: Dict[int, float] = {}
RATE_LIMIT_SECONDS = 2.0

def _is_rate_limited(self, user_id: int) -> bool:
    now = time.monotonic()
    last = self._last_command.get(user_id, 0)
    if now - last < self.RATE_LIMIT_SECONDS:
        return True
    self._last_command[user_id] = now
    return False
```
Call `_is_rate_limited()` at the top of `handle_message()` and `handle_document()`.

---

## 🟠 PHASE 2 — BUG FIXES & ERROR HANDLING

### Task 2.1 — `obs.py` Missing from Source (Ghost Module)
**Issue:** `__pycache__/obs.cpython-311.pyc` exists but `obs.py` is absent from the repository. If any future code imports `obs`, it will silently use a stale `.pyc` on some Python builds and fail on a clean checkout.

**Fix options (choose one):**
- [ ] Restore `obs.py` — reconstruct from the `.pyc` bytecode (the docstring reveals it was a `log_event()` helper using `shlex.quote`)
- [ ] Remove the stale `.pyc` and add `obs` import to `.gitignore` cleanup notes

**Reconstructed `obs.py` (from bytecode analysis):**
```python
"""Observability helper. One line per meaningful event, key=value format,
no emojis, no banners. Output goes to stdout via the root logger."""
import logging
import shlex

logger = logging.getLogger(__name__)

def log_event(kind: str, **fields) -> None:
    """Emit a single-line audit record.
    Example:
        log_event("cmd", user=123, command="/add_offer", chat="private")
        -> "cmd user=123 command=/add_offer chat=private"
    """
    parts = [kind]
    for key, value in fields.items():
        parts.append(f"{key}={shlex.quote(str(value))}")
    logger.info(" ".join(parts))
```

---

### Task 2.2 — Scheduler `_row_to_offer` Missing `icon` Field
**File:** `scheduler.py` → `PostingScheduler._row_to_offer()`
**Issue:** `OfferData` is constructed without setting `icon`, so it defaults to `"📌"` regardless of the actual service icon. All scheduled posts render with the wrong emoji.

**Fix:**
```python
@staticmethod
def _row_to_offer(row: tuple, icon: str = "📌") -> OfferData:
    return OfferData(
        service_type=row[1], provider=row[2], ...
        icon=icon,  # ADD THIS
    )
```
In `post_by_service()`, pass `icon=service_config.icon`:
```python
service_config = config_manager.get_service_config(service_key)
offer = self._row_to_offer(row, icon=service_config.icon)
```

---

### Task 2.3 — Session Memory Leak — No TTL / Expiry
**File:** `admin_commands.py` → `AdminCommands.user_sessions`
**Issue:** Sessions are only deleted on completion or `/cancel`. Abandoned sessions (user starts `/add_offer`, walks away) accumulate forever. On a busy bot this is a memory leak and a potential state confusion vector.

**Fix:** Add session TTL enforcement in `handle_message()`:
```python
SESSION_TTL_SECONDS = 600  # 10 minutes

def _purge_expired_sessions(self) -> None:
    now = time.monotonic()
    expired = [uid for uid, s in self.user_sessions.items()
               if now - s.get("created_at", now) > self.SESSION_TTL_SECONDS]
    for uid in expired:
        del self.user_sessions[uid]

# In session creation, always store:
self.user_sessions[uid] = {"flow": ..., "state": ..., "data": {}, "created_at": time.monotonic()}
```
Call `_purge_expired_sessions()` at the start of `handle_message()`.

---

### Task 2.4 — Config YAML Write Race Condition
**File:** `config_schema.py` → `update_channel_id()`, `add_custom_service()`
**Issue:** Both methods do read-then-write on the YAML file without any lock. Two concurrent admin commands could both read the same file, each write their change, and the second write silently overwrites the first.

**Fix:** Use a threading lock:
```python
import threading

class ConfigManager:
    def __init__(self, ...):
        ...
        self._file_lock = threading.Lock()

    def update_channel_id(self, ...):
        with self._file_lock:
            with open(self.config_path, "r") as f:
                data = yaml.safe_load(f) or {}
            ...
            with open(self.config_path, "w") as f:
                yaml.dump(data, f, ...)
```
Apply to both mutating methods.

---

### Task 2.5 — `SCHED_INTERVAL_HOURS` Not Validated
**File:** `main.py`
**Issue:** `float(os.environ.get("SCHED_INTERVAL_HOURS", "1.6"))` — if set to `0` or a negative value, the scheduler runs in a tight infinite loop hammering the DB and Telegram API.

**Fix:**
```python
interval_hours = max(0.1, float(os.environ.get("SCHED_INTERVAL_HOURS", "1.6")))
```
Log a warning if the value was clamped.

---

### Task 2.6 — Health Check Server Uses `os._exit(1)` 
**File:** `main.py` → `health_server()`
**Issue:** `os._exit(1)` bypasses Python's `atexit` handlers, `finally` blocks, and PTB's graceful shutdown. If the health port is in use, the entire bot terminates without cleanup.

**Fix:** Replace with logging + a signal to the main thread, or simply log and continue without the health server:
```python
except OSError as e:
    logger.warning("Health-check server could not bind on port %s: %s. Continuing without it.", port, e)
    return  # Don't kill the process
```

---

### Task 2.7 — `handle_document` Double Size Check is Redundant
**File:** `main.py`
**Issue:** File size is checked twice — once from `doc.file_size` (Telegram metadata, may be None) and once after downloading. The second check is correct; the first is unreliable because Telegram doesn't always provide `file_size`.

**Fix:** Remove the first check and rely solely on the post-download check:
```python
# Remove:
if doc.file_size and doc.file_size > MAX_CSV_BYTES:
    return await update.message.reply_text(...)
# Keep only the post-download check on len(raw)
```

---

## 🟡 PHASE 3 — PERFORMANCE OPTIMISATIONS

### Task 3.1 — Enable SQLite WAL Mode + Optimize Connection Handling
**File:** `db_layer.py`
**Issue:** Default SQLite journal mode (DELETE/ROLLBACK) means any write locks out all readers. With async scheduler + interactive bot commands running concurrently, readers block during write operations.

**Fix — add to `init_db()`:**
```python
cur.execute("PRAGMA journal_mode=WAL")
cur.execute("PRAGMA synchronous=NORMAL")
cur.execute("PRAGMA cache_size=-8000")   # 8 MB page cache
cur.execute("PRAGMA temp_store=MEMORY")
cur.execute("PRAGMA foreign_keys=ON")
```

---

### Task 3.2 — N+1 Query in `get_stats()`
**File:** `db_layer.py` → `get_stats()`
**Issue:** Runs one SQL query per service key. With 13 services this is 13 round-trips per `/stats` call.

**Fix — single query with GROUP BY:**
```python
def get_stats(self) -> Dict[str, Dict[str, int]]:
    stats: Dict[str, Dict[str, int]] = {k: {"queued": 0, "posted": 0, "failed": 0}
                                         for k in config_manager.all_service_keys()}
    with self._conn() as con:
        rows = con.execute(
            "SELECT service_type, status, COUNT(*) FROM offers GROUP BY service_type, status"
        ).fetchall()
    for svc, status, count in rows:
        if svc in stats and status in stats[svc]:
            stats[svc][status] = count
    return stats
```

---

### Task 3.3 — Add Missing Database Indexes
**File:** `db_layer.py` → `init_db()`
**Issue:** `posting_history` has no index on `offer_id`. `users` has no index on `blocked`. As history grows, `mark_posted` and block-checks degrade.

**Fix — add to `init_db()`:**
```python
cur.execute("CREATE INDEX IF NOT EXISTS idx_ph_offer_id ON posting_history(offer_id)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_users_blocked ON users(user_id, blocked)")
```

---

### Task 3.4 — Reuse Bot Instance in Scheduler
**File:** `scheduler.py`
**Issue:** `PostingScheduler.__init__` creates a new `Bot` instance every instantiation. While this is fine for the current code (one instance), it initialises a full HTTP client. Should use the same bot the Application already has.

**Fix (long-term):** Pass the `Bot` instance from `Application` to `PostingScheduler` rather than creating a new one:
```python
class PostingScheduler:
    def __init__(self, bot: Bot):
        self.bot = bot
        ...
```
In `_scheduler_loop` / `_post_init`, pass `application.bot`.

---

### Task 3.5 — Add `posting_history` Retention Policy
**Issue:** `posting_history` grows unbounded. No pruning or archival logic exists. At 1 post/hour across 13 services, this is ~113k rows/year.

**Fix — add maintenance method to `DatabaseManager`:**
```python
def prune_posting_history(self, keep_days: int = 90) -> int:
    cutoff = int(time.time()) - keep_days * 86400
    with self._conn() as con:
        cur = con.execute(
            "DELETE FROM posting_history WHERE posted_at < ?", (cutoff,)
        )
        con.commit()
        return cur.rowcount
```
Call monthly from the scheduler or via a new admin command `/prune`.

---

## 🟢 PHASE 4 — CODE QUALITY & REFACTORING

### Task 4.1 — Add Full Type Hints Across All Modules
**Issue:** Multiple functions have incomplete or missing annotations. Key offenders:

| File | Function | Missing |
|---|---|---|
| `db_layer.py` | `list_offers` | return type is `List[Tuple]` — make it `List[Tuple[int,str,str,str,str,str,int]]` or define a NamedTuple |
| `admin_commands.py` | `_add_offer_step` | return type missing |
| `scheduler.py` | `_row_to_offer` | parameter `row` should be `Tuple` |
| `templates.py` | `render_multi_language` | `languages` param should be `Optional[List[Language]]` |

**Fix:** Define row `NamedTuple`s for DB results to eliminate magic index access:
```python
from typing import NamedTuple

class OfferRow(NamedTuple):
    id: int
    service_type: str
    provider: str
    title_en: str
    title_hi: Optional[str]
    title_gu: Optional[str]
    description_en: Optional[str]
    description_hi: Optional[str]
    description_gu: Optional[str]
    referral_link: str
    validity: Optional[str]
    terms: Optional[str]
    channel_id: str
```
Replace all `row[0]`, `row[1]` etc. with `row.id`, `row.service_type` etc.

---

### Task 4.2 — Remove Direct Import of Private Symbol
**File:** `admin_commands.py` line 6: `from config_schema import config_manager, _is_valid_channel_id`
**Issue:** `_is_valid_channel_id` is a private helper (underscore prefix) being imported into another module — violates encapsulation (SOLID: ISP).

**Fix:** Expose it as a public function `is_valid_channel_id` in `config_schema.py`, or move validation into `ConfigManager` as a static method:
```python
@staticmethod
def is_valid_channel_id(channel_id: str) -> bool:
    ...
```

---

### Task 4.3 — Refactor `_add_offer_step` State Machine
**File:** `admin_commands.py`
**Issue:** `_add_offer_step` is a 50-line cascade of `if state == "..."` blocks — brittle and hard to extend.

**Fix:** Use a data-driven step table:
```python
OFFER_FLOW_STEPS = [
    ("provider",        "Send the **provider** name (e.g. HDFC Bank)."),
    ("title_en",        "Send the **title in English**."),
    ("title_hi",        "Send the **title in Hindi**, or `skip`."),
    ("title_gu",        "Send the **title in Gujarati**, or `skip`."),
    ("description_en",  "Send the **description in English**, or `skip`."),
    ("description_hi",  "Send the **description in Hindi**, or `skip`."),
    ("description_gu",  "Send the **description in Gujarati**, or `skip`."),
    ("referral_link",   "Send the **referral link** (must start with https://)."),
]
SKIP_ALLOWED = {"title_hi", "title_gu", "description_en", "description_hi", "description_gu"}

async def _add_offer_step(self, update, session, text):
    state = session["state"]
    steps = {name: prompt for name, prompt in OFFER_FLOW_STEPS}

    if state not in steps:
        return

    # Validate link step
    if state == "referral_link" and not is_valid_url(text):
        return await update.message.reply_text("Invalid link. Must start with https://.")

    session["data"][state] = (None if _is_skip(text) and state in SKIP_ALLOWED else text)

    # Advance to next step
    step_names = [n for n, _ in OFFER_FLOW_STEPS]
    idx = step_names.index(state)
    if idx + 1 < len(step_names):
        next_state = step_names[idx + 1]
        session["state"] = next_state
        await update.message.reply_text(steps[next_state], parse_mode="HTML")
    else:
        await self._preview_and_confirm(update, session)
```

---

### Task 4.4 — Improve Logging with Structured Events (Restore `obs.py`)
**Issue:** Log messages use f-strings with inconsistent structure. The `obs.py` observability helper (found in `__pycache__`) was removed but provides a valuable structured logging pattern.

**Fix:** Restore `obs.py` (see Task 2.1) and replace key log calls:
```python
# Before:
logger.info("Admin %s blocked user %s", update.effective_user.id, target)

# After:
log_event("admin_action", action="block", admin=uid, target=target)
```
This enables log aggregation, searching by `action=block`, and easier alerting.

---

### Task 4.5 — Strengthen `is_valid_url` — HTTPS-Only for Referral Links
**File:** `csv_validator.py`
**Issue:** `is_valid_url()` accepts `http://` URLs. Financial referral links must use HTTPS. An `http://` link could be MITMed to replace referral codes.

**Fix:**
```python
def is_valid_url(url: str, require_https: bool = True) -> bool:
    if not url:
        return False
    if require_https and not url.startswith("https://"):
        return False
    elif not (url.startswith("https://") or url.startswith("http://")):
        return False
    return bool(URL_PATTERN.match(url))
```
Call `is_valid_url(text, require_https=True)` everywhere referral links are validated.

---

## 🔵 PHASE 5 — INDUSTRY-LEVEL LOGIC ENHANCEMENTS

### Task 5.1 — Add Database Schema Migration System
**Issue:** `init_db()` uses `CREATE TABLE IF NOT EXISTS` — fine for new installs, but there's no mechanism to add new columns to an existing DB. Any schema change will silently fail on deployed instances.

**Fix:** Add a `schema_version` table and version-gated migration steps:
```python
SCHEMA_MIGRATIONS = {
    1: [
        "ALTER TABLE offers ADD COLUMN metadata TEXT",
        "ALTER TABLE users ADD COLUMN last_seen INTEGER",
    ],
    2: [
        "CREATE INDEX IF NOT EXISTS idx_ph_offer_id ON posting_history(offer_id)",
    ],
}

def _run_migrations(self, con):
    con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    row = con.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0
    for version in sorted(SCHEMA_MIGRATIONS):
        if version > current:
            for stmt in SCHEMA_MIGRATIONS[version]:
                try:
                    con.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            con.execute("INSERT INTO schema_version VALUES (?)", (version,))
    con.commit()
```

---

### Task 5.2 — Add Webhook Support (Production Deployment)
**Issue:** The bot uses `run_polling()`. For production (Render, Railway, Fly.io), webhooks are more efficient — no long-polling, lower latency, and compatible with serverless.

**Fix:** Add environment-based routing in `main()`:
```python
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g. https://myapp.onrender.com

if WEBHOOK_URL:
    app.run_webhook(
        listen="0.0.0.0",
        port=health_port,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
    )
else:
    app.run_polling(drop_pending_updates=True)
```
Set `WEBHOOK_URL` in Replit Secrets for production, leave unset for local development.

---

### Task 5.3 — Offer Deduplication Across CSV Batches
**Issue:** `CSVValidator` deduplicates within a single CSV upload using `seen_fingerprints`, but does not check against already-posted offers in the database. The `db_layer.insert_offer()` handles DB-level duplicates via the `UNIQUE` constraint on `fingerprint`, but the CSV report shows them as "insert errors" rather than "duplicates".

**Fix:** Pre-check fingerprints against the DB during validation:
```python
def _validate_row(self, row, row_num):
    ...
    existing = db_manager.fingerprint_exists(fingerprint)
    if existing:
        self.warnings.append(
            f"Row {row_num}: Already in DB with status '{existing}' — skipped"
        )
        return None
    ...
```
This eliminates confusing "Duplicate offer" insert errors in the upload summary.

---

### Task 5.4 — Admin Audit Trail in Database
**Issue:** Admin actions (block/unblock/delete/requeue) are logged to stdout only. Logs rotate and are lost. No searchable audit history exists.

**Fix:** Add `admin_audit` table:
```sql
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    ts INTEGER NOT NULL
)
```
Add `db_manager.audit(admin_id, action, target, details)` and call it from every admin command handler.

---

### Task 5.5 — Add `replit.md` / `README.md` Setup Instructions
**Issue:** `README.md` is present but minimal. New deployers have no clear guide for environment variables, Telegram Bot setup, or first-run procedure.

**Fix — update `README.md` with:**
1. Required environment variables with descriptions:
   - `TELEGRAM_BOT_TOKEN` (required) — from @BotFather
   - `ADMIN_IDS` (required) — comma-separated Telegram user IDs
   - `SCHED_INTERVAL_HOURS` (optional, default 1.6) — scheduler cycle in hours
   - `PORT` (optional, default 8000) — health check port
   - `WEBHOOK_URL` (optional) — set for webhook mode in production
2. `.env.example` file with placeholder values
3. First-run steps: create bot, get token, set admin ID, run `python main.py`
4. CSV upload format reference

---

### Task 5.6 — Add Minimal Test Suite
**Issue:** Zero test coverage. The validation logic (`csv_validator.py`, `config_schema.py`, `db_layer.py`, `templates.py`) is entirely untested.

**Fix — create `tests/` directory with:**

```
tests/
├── test_csv_validator.py    # Test required columns, URL validation, duplicates, encoding
├── test_db_layer.py         # Test insert, duplicate fingerprint, mark_posted, stats
├── test_templates.py        # Test HTML escaping, multi-language rendering, icon assignment
└── conftest.py              # Shared fixtures (in-memory SQLite DB, mock config)
```

Minimum test cases:
- `test_csv_validator.py`: valid CSV accepted, missing required column rejected, duplicate URL within CSV deduplicated, HTTP URL rejected for referral link
- `test_db_layer.py`: insert_offer returns True/None for new, False/"Duplicate offer" for duplicate fingerprint, get_stats returns all service keys
- `test_templates.py`: HTML special chars in provider name are escaped, icon from service_config is used

**Add to `requirements.txt`:**
```
pytest>=8,<9
pytest-asyncio>=0.24
```

---

## 📋 PROJECT STRUCTURE IMPROVEMENTS

### Task 6.1 — Recommended Final Structure
```
FinService-Bot/
├── .env.example              # NEW: template with placeholder secrets
├── .gitignore                # ✅ exists, verified
├── Procfile                  # ✅ exists
├── README.md                 # UPDATED: full setup guide
├── requirements.txt          # UPDATED: add pytest
├── services_config.yaml      # ✅ exists
│
├── finreferrals/             # NEW: package the source
│   ├── __init__.py
│   ├── admin_commands.py     # UPDATED: type hints, session TTL, HTML escape
│   ├── config_schema.py      # UPDATED: lock on mutations, public channel validator
│   ├── csv_validator.py      # UPDATED: HTTPS-only, DB pre-check
│   ├── db_layer.py           # UPDATED: WAL, migration, audit table, NamedTuples
│   ├── main.py               # UPDATED: webhook support, health fix, rate limit
│   ├── obs.py                # RESTORED: structured logging helper
│   ├── scheduler.py          # UPDATED: accept bot parameter, icon fix
│   └── templates.py          # UPDATED: HTML escape all user fields
│
└── tests/
    ├── conftest.py
    ├── test_csv_validator.py
    ├── test_db_layer.py
    └── test_templates.py
```

---

## ✅ ACCEPTANCE CRITERIA SUMMARY

| Task | Acceptance Criterion |
|---|---|
| 0.1 | Old token is revoked; new token not in any tracked file |
| 1.1 | `<b>hacker</b>` as provider name renders as literal text in Telegram |
| 1.2 | `HEAD /requirements.txt` returns 405, not file metadata |
| 1.3 | `confirm_yes` callback on session without `offer` key returns graceful error |
| 1.4 | Sending 10 messages in 5s from one user triggers rate limit on 2nd+ |
| 2.1 | `obs.py` importable; `log_event("test", x=1)` emits `test x=1` to log |
| 2.2 | Scheduled posts show correct emoji per service (💳 for credit_card) |
| 2.3 | Session abandoned for >10min is cleared on next user interaction |
| 2.4 | Two concurrent `/channels` edits do not corrupt `services_config.yaml` |
| 2.5 | `SCHED_INTERVAL_HOURS=0` logs warning and uses 0.1h minimum |
| 2.6 | Health port conflict logs warning; bot continues running |
| 3.1 | `PRAGMA journal_mode` returns `wal` after init |
| 3.2 | `/stats` executes exactly 1 SQL query (verify via SQLite trace) |
| 3.3 | `EXPLAIN QUERY PLAN` for `posting_history` join shows index scan |
| 4.1 | `mypy finreferrals/` passes with no errors |
| 4.2 | `_is_valid_channel_id` is no longer imported from `config_schema` externally |
| 4.5 | Uploading CSV with `http://` referral link shows validation error |
| 5.1 | Running `init_db()` on existing DB with old schema applies migrations cleanly |
| 5.3 | Uploading CSV with offer already in DB shows "Already in DB" warning, not insert error |
| 5.6 | `pytest tests/` passes 100% |

---

## 🔧 REPLIT AGENT IMPLEMENTATION NOTES

When implementing in Replit Agent, follow this execution order:

1. **Start with Phase 0** — the exposed token must be rotated before any other work
2. **Phase 1 tasks are independent** — can be done in any order
3. **Phase 2 tasks** — Task 2.1 (restore obs.py) should come before Task 4.4
4. **Phase 3.1 (WAL)** — must be done before Phase 5.1 (migrations) as WAL is set during init
5. **Phase 4.1 (NamedTuples)** — do before Phase 2.2 (icon fix) as the fix uses NamedTuple fields
6. **Phase 5 and 6** — last, as they depend on all fixes being in place

**Environment variables to set in Replit Secrets (not in files):**
```
TELEGRAM_BOT_TOKEN=<new token from @BotFather>
ADMIN_IDS=<your telegram user id>
SCHED_INTERVAL_HOURS=1.6
PORT=8000
```

**Run tests after each phase:**
```bash
pip install pytest pytest-asyncio --break-system-packages
pytest tests/ -v
```