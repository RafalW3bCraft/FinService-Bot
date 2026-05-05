"""Shared pytest fixtures.

We point ``DB_PATH`` at a temp file *before* importing modules that touch
the database, so the suite never writes to the real ``fin_referrals.db``.
"""
import os
import sys
import tempfile

import pytest

# Make the project root importable when pytest is run from anywhere.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Yield a fresh DatabaseManager bound to a temp SQLite file."""
    db_file = tmp_path / "test.db"
    # Patch the module constant before constructing a manager.
    import db_layer
    monkeypatch.setattr(db_layer, "DB_PATH", str(db_file))
    mgr = db_layer.DatabaseManager(str(db_file))
    yield mgr


@pytest.fixture
def sample_offer():
    from templates import OfferData
    return OfferData(
        service_type="credit_card",
        provider="HDFC Bank",
        title_en="5% cashback",
        referral_link="https://hdfc.example.com/ref/abc",
        icon="💳",
    )
