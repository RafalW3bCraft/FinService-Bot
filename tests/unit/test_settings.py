import os
from pathlib import Path

import pytest

from finservice_bot.settings import Settings, SettingsError


VALID_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE12345"


def valid_mapping(**overrides: str) -> dict[str, str]:
    values = {
        "TELEGRAM_BOT_TOKEN": VALID_TOKEN,
        "ADMIN_IDS": "10",
    }
    values.update(overrides)
    return values


def test_settings_requires_token():
    with pytest.raises(SettingsError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_mapping({})


def test_settings_requires_at_least_one_admin():
    with pytest.raises(SettingsError, match="ADMIN_IDS"):
        Settings.from_mapping({"TELEGRAM_BOT_TOKEN": VALID_TOKEN})


def test_settings_parses_admins_without_exposing_token():
    settings = Settings.from_mapping(valid_mapping(ADMIN_IDS="10, 20"))

    assert settings.admin_ids == frozenset({10, 20})
    assert "abcdefghijklmnopqrstuvwxyz" not in repr(settings)


def test_settings_parses_typed_overrides():
    settings = Settings.from_mapping(
        valid_mapping(
            SERVICE_CONFIG_PATH="/tmp/services.yaml",
            MAX_CSV_BYTES="2048",
            MAX_CSV_ROWS="25",
            SESSION_TTL_MINUTES="45",
            CLAIM_TTL_SECONDS="90",
            PUBLISH_BATCH_SIZE="12",
            ALLOWED_REFERRAL_DOMAINS="bank.example, lender.example",
        )
    )

    assert settings.service_config_path == Path("/tmp/services.yaml")
    assert settings.max_csv_bytes == 2048
    assert settings.max_csv_rows == 25
    assert settings.session_ttl_minutes == 45
    assert settings.claim_ttl_seconds == 90
    assert settings.publish_batch_size == 12
    assert settings.allowed_referral_domains == frozenset({"bank.example", "lender.example"})


def test_settings_loads_env_file_without_mutating_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    env_file = tmp_path / "settings.env"
    env_file.write_text(
        f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}\nADMIN_IDS=10,20\n",
        encoding="utf-8",
    )
    environment_before = os.environ.copy()

    settings = Settings.load(env_file)

    assert settings.admin_ids == frozenset({10, 20})
    assert os.environ == environment_before


def test_environment_overrides_env_file(tmp_path, monkeypatch):
    file_token = "111111:abcdefghijklmnopqrstuvwxyzABCDE12345"
    environment_token = "222222:abcdefghijklmnopqrstuvwxyzABCDE12345"
    env_file = tmp_path / "settings.env"
    env_file.write_text(
        f"TELEGRAM_BOT_TOKEN={file_token}\nADMIN_IDS=10\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", environment_token)
    monkeypatch.setenv("ADMIN_IDS", "20")

    settings = Settings.load(env_file)

    assert settings.telegram_bot_token == environment_token
    assert settings.admin_ids == frozenset({20})


def test_settings_uses_documented_defaults():
    settings = Settings.from_mapping(valid_mapping())

    assert settings.service_config_path == Path("config/services.yaml")
    assert settings.max_csv_bytes == 5 * 1024 * 1024
    assert settings.max_csv_rows == 1_000
    assert settings.session_ttl_minutes == 30
    assert settings.claim_ttl_seconds == 120
    assert settings.publish_batch_size == 10
    assert settings.allowed_referral_domains == frozenset()


@pytest.mark.parametrize("name", ["SERVICE_CONFIG_PATH"])
def test_paths_must_not_be_blank(name):
    with pytest.raises(SettingsError, match=name):
        Settings.from_mapping(valid_mapping(**{name: "   "}))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_CSV_BYTES", "-1"),
        ("MAX_CSV_ROWS", "invalid"),
        ("SESSION_TTL_MINUTES", "0"),
        ("CLAIM_TTL_SECONDS", "-1"),
        ("PUBLISH_BATCH_SIZE", "invalid"),
    ],
)
def test_numeric_settings_must_be_positive_and_well_formed(name, value):
    with pytest.raises(SettingsError, match=name):
        Settings.from_mapping(valid_mapping(**{name: value}))


def test_admin_ids_must_be_integers():
    with pytest.raises(SettingsError, match="ADMIN_IDS"):
        Settings.from_mapping(valid_mapping(ADMIN_IDS="10,not-an-id"))


def test_admin_ids_must_be_positive():
    with pytest.raises(SettingsError, match="ADMIN_IDS"):
        Settings.from_mapping(valid_mapping(ADMIN_IDS="0,-10"))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_CSV_BYTES", str(20 * 1024 * 1024 + 1)),
        ("MAX_CSV_ROWS", "10001"),
        ("SESSION_TTL_MINUTES", "1441"),
        ("CLAIM_TTL_SECONDS", "3601"),
        ("PUBLISH_BATCH_SIZE", "101"),
    ],
)
def test_numeric_settings_have_safe_upper_bounds(name, value):
    with pytest.raises(SettingsError, match=name):
        Settings.from_mapping(valid_mapping(**{name: value}))


def test_allowed_domains_are_normalized_and_validated():
    settings = Settings.from_mapping(
        valid_mapping(ALLOWED_REFERRAL_DOMAINS=" Bank.Example, .lender.example ")
    )
    assert settings.allowed_referral_domains == frozenset({"bank.example", "lender.example"})

    with pytest.raises(SettingsError, match="ALLOWED_REFERRAL_DOMAINS"):
        Settings.from_mapping(valid_mapping(ALLOWED_REFERRAL_DOMAINS="localhost"))


def test_settings_load_rejects_missing_env_file(tmp_path):
    with pytest.raises(SettingsError, match="does not exist"):
        Settings.load(tmp_path / "missing.env")
