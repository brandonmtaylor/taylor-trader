import logging
from pathlib import Path

import pytest

from taylortrader import secrets
from taylortrader.config import ConfigError, load_config
from taylortrader.logging_setup import MASK, RedactingFilter, redact
from taylortrader.secretscan import scan_text

# Fake credentials, assembled at runtime so no key-shaped literal ever sits in the repo.
FAKE_KEY = "PK" + "TEST" + "1234567890" + "ABCD"
FAKE_SECRET = "abcdEFGHijklMNOP" + "qrstUVWXyz0123456789abcd"  # secretscan: allow (fake)


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(secrets, "_read_keychain", lambda name: None)
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", FAKE_KEY)
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", FAKE_SECRET)
    monkeypatch.delenv(secrets.LIVE_CONFIRM_VAR, raising=False)


def test_credentials_never_print_values(fake_env):
    creds = secrets.load_alpaca_credentials("paper")
    for text in (repr(creds), str(creds), f"{creds}", repr(creds.key_id), str(creds.secret_key)):
        assert FAKE_KEY not in text and FAKE_SECRET not in text


def test_logs_redact_loaded_secrets(fake_env, caplog):
    secrets.load_alpaca_credentials("paper")
    record = logging.LogRecord("t", logging.INFO, "", 0, "auth with %s / %s", (FAKE_KEY, FAKE_SECRET), None)
    RedactingFilter().filter(record)
    msg = record.getMessage()
    assert FAKE_KEY not in msg and FAKE_SECRET not in msg and MASK in msg


def test_redaction_in_tracebacks(fake_env):
    secrets.load_alpaca_credentials("paper")
    try:
        raise RuntimeError(f"bad request with secret {FAKE_SECRET}")
    except RuntimeError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, "", 0, "boom", (), sys.exc_info())
    RedactingFilter().filter(record)
    assert FAKE_SECRET not in record.getMessage()


def test_redact_key_shapes_even_if_not_loaded():
    unloaded = "PK" + "ZZZZ9999" + "888877776666"
    assert unloaded not in redact(f"key {unloaded} here")


def test_missing_credentials_error_names_variable_not_value(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(secrets, "_read_keychain", lambda name: None)
    monkeypatch.delenv("ALPACA_PAPER_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET_KEY", raising=False)
    with pytest.raises(secrets.SecretsError, match="ALPACA_PAPER_KEY_ID"):
        secrets.load_alpaca_credentials("paper")


def test_live_requires_explicit_confirmation(fake_env, monkeypatch):
    monkeypatch.setenv("ALPACA_LIVE_KEY_ID", FAKE_KEY)
    monkeypatch.setenv("ALPACA_LIVE_SECRET_KEY", FAKE_SECRET)
    with pytest.raises(secrets.SecretsError, match="TAYLORTRADER_ALLOW_LIVE"):
        secrets.load_alpaca_credentials("live")
    monkeypatch.setenv(secrets.LIVE_CONFIRM_VAR, "yes")
    assert secrets.load_alpaca_credentials("live").paper is False


def test_config_rejects_secrets(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('mode = "paper"\nalpaca_secret_key = "x"\n')
    with pytest.raises(ConfigError, match="looks like a secret"):
        load_config(cfg)


def test_example_config_loads():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.example.toml")
    assert cfg.mode == "paper"
    assert cfg.strategies[0].name == "macd_rsi"


def test_secretscan_catches_alpaca_keys():
    # Built at runtime so this file never contains a key-shaped literal.
    fake_live_id = "AK" + "FAKE0000" + "TEST1111" + "XY"
    assert scan_text(f"PUBLIC_KEY = {fake_live_id}")
    assert scan_text(f"secret = {FAKE_SECRET}")
    assert not scan_text("mode = 'paper'")


def test_allow_marker_cannot_hide_key_ids_or_known_values():
    key_id = "PK" + "FAKE0000" + "TEST2222" + "QQ"
    assert scan_text(f"x = '{key_id}'  # secretscan: allow")
    known_value = "some-real-looking-value-123456"
    assert scan_text(f"note: {known_value}  # secretscan: allow", known={known_value})
