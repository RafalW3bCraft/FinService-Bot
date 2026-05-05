from templates import OfferData


def test_insert_offer_returns_true_for_new(tmp_db, sample_offer):
    ok, msg = tmp_db.insert_offer(sample_offer)
    assert ok is True
    assert "Queued" in (msg or "")


def test_insert_offer_returns_duplicate(tmp_db, sample_offer):
    tmp_db.insert_offer(sample_offer)
    ok, msg = tmp_db.insert_offer(sample_offer)
    assert ok is False
    assert msg == "Duplicate offer"


def test_get_stats_returns_all_service_keys(tmp_db):
    from config_schema import config_manager
    stats = tmp_db.get_stats()
    for key in config_manager.all_service_keys():
        assert key in stats
        assert stats[key] == {"queued": 0, "posted": 0, "failed": 0}


def test_get_stats_aggregates(tmp_db, sample_offer):
    tmp_db.insert_offer(sample_offer)
    other = OfferData(
        service_type="credit_card",
        provider="ICICI",
        title_en="Free",
        referral_link="https://icici.example.com/ref/b",
    )
    tmp_db.insert_offer(other)
    stats = tmp_db.get_stats()
    assert stats["credit_card"]["queued"] == 2


def test_wal_pragma_enabled(tmp_db):
    import sqlite3
    con = sqlite3.connect(tmp_db.db_path)
    mode = con.execute("PRAGMA journal_mode").fetchone()[0].lower()
    con.close()
    assert mode == "wal"


def test_migrations_record_versions(tmp_db):
    import sqlite3
    con = sqlite3.connect(tmp_db.db_path)
    rows = con.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    con.close()
    versions = [r[0] for r in rows]
    assert 1 in versions
    assert 2 in versions


def test_audit_writes_row(tmp_db):
    tmp_db.audit(123, "block", target="456", details="test")
    import sqlite3
    con = sqlite3.connect(tmp_db.db_path)
    row = con.execute(
        "SELECT admin_id, action, target FROM admin_audit WHERE admin_id = ?",
        (123,),
    ).fetchone()
    con.close()
    assert row == (123, "block", "456")


def test_list_audit_returns_newest_first(tmp_db):
    tmp_db.audit(1, "block", target="a")
    tmp_db.audit(2, "delete_offer", target="b")
    tmp_db.audit(3, "requeue", details="hours=None count=0")
    rows = tmp_db.list_audit(limit=10)
    assert len(rows) == 3
    actions_in_order = [r[2] for r in rows]
    # Inserted in the order block, delete_offer, requeue → newest-first
    # listing should reverse that.
    assert actions_in_order[0] == "requeue"
    assert actions_in_order[-1] == "block"


def test_list_audit_respects_limit(tmp_db):
    for i in range(5):
        tmp_db.audit(i, "block", target=str(i))
    assert len(tmp_db.list_audit(limit=2)) == 2


def test_prune_posting_history_removes_old(tmp_db):
    import sqlite3
    con = sqlite3.connect(tmp_db.db_path)
    con.execute(
        "INSERT INTO posting_history (offer_id, channel_id, posted_at, success) VALUES (?,?,?,?)",
        (1, "@x", 0, 1),
    )
    con.commit()
    con.close()
    n = tmp_db.prune_posting_history(keep_days=1)
    assert n == 1


def test_scheduler_maybe_prune_runs_on_first_call_when_overdue(tmp_db, monkeypatch):
    """`_maybe_prune_history` should call into the DB when the interval has
    elapsed and short-circuit otherwise."""
    import scheduler as sched_mod

    # Avoid touching the real Bot/network: stub the constructor.
    monkeypatch.setattr(sched_mod, "Bot", lambda *a, **kw: object())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setattr(sched_mod, "db_manager", tmp_db)

    sched = sched_mod.PostingScheduler()
    calls = {"n": 0}

    def fake_prune(keep_days):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(tmp_db, "prune_posting_history", fake_prune)

    # Not yet due → no call.
    sched._maybe_prune_history()
    assert calls["n"] == 0

    # Force interval to elapse → exactly one call.
    sched._last_prune_ts -= sched.PRUNE_INTERVAL_SECONDS + 1
    sched._maybe_prune_history()
    assert calls["n"] == 1

    # Immediate re-call short-circuits.
    sched._maybe_prune_history()
    assert calls["n"] == 1
