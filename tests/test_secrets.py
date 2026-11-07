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
    for wrong in ("yes", "0", "false", ""):  # only the exact value the dashboard writes unlocks it
        monkeypatch.setenv(secrets.LIVE_CONFIRM_VAR, wrong)
        with pytest.raises(secrets.SecretsError, match="TAYLORTRADER_ALLOW_LIVE"):
            secrets.load_alpaca_credentials("live")
    monkeypatch.setenv(secrets.LIVE_CONFIRM_VAR, "1")
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


def test_the_live_flag_isnt_treated_as_a_secret(fake_env, monkeypatch):
    # The go-live flag is "1". Registering it would mask every 1 in every alert ("$1,234" -> "$***,234").
    monkeypatch.setenv(secrets.LIVE_CONFIRM_VAR, "1")
    assert secrets.live_trading_confirmed()
    assert redact("bought $1,234.51 of VTI") == "bought $1,234.51 of VTI"
    secrets._LOADED_SECRETS.add("12")  # and a short value from anywhere else is never masked either
    try:
        assert redact("12 shares") == "12 shares"
    finally:
        secrets._LOADED_SECRETS.discard("12")


# --- security review (2026-11-06) -------------------------------------------------------------------

def test_a_keychain_timeout_doesnt_put_the_secret_in_the_error(monkeypatch):
    import subprocess

    from taylortrader import secrets as s

    seen = {}

    def hang(cmd, **kw):
        seen.update(cmd=cmd, input=kw.get("input"))
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(s, "keychain_available", lambda: True)
    monkeypatch.setattr(s.subprocess, "run", hang)
    value = "s3cret-value-for-the-test"
    with pytest.raises(s.SecretsError) as e:
        s.set_secret("EMAIL_PASSWORD", value)
    assert value not in str(e.value) and e.value.__suppress_context__
    assert value not in " ".join(seen["cmd"]) and value in seen["input"]   # stdin, never arguments
    assert value in s.loaded_secret_values()                               # already masked in logs


def test_values_are_quoted_for_the_keychain_command(monkeypatch):
    from taylortrader import secrets as s

    seen = {}
    monkeypatch.setattr(s.subprocess, "run", lambda cmd, **kw: seen.update(input=kw["input"]))
    monkeypatch.setattr(s, "_read_keychain", lambda name: 'a"b\\c')
    s._write_keychain("EMAIL_PASSWORD", 'a"b\\c')
    assert seen["input"].endswith('-w "a\\"b\\\\c"\n')


def test_email_verifies_the_server():
    import ssl
    from unittest import mock

    from pydantic import SecretStr

    from taylortrader.alerts import EmailNotifier

    with mock.patch("smtplib.SMTP") as smtp:
        EmailNotifier("a@example.com", SecretStr("x"), ["b@example.com"]).send("t", "m")
    ctx = smtp.return_value.__enter__.return_value.starttls.call_args.kwargs["context"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


@pytest.mark.parametrize("line", [
    "key = sk-" + "ant-api03-abcdefghijklmnopqrstuvwx",   # split so this file doesn't trip the scan
    "url = https://ntfy" + ".sh/my-private-topic-123",
    "ping https://hc-ping" + ".com/0a1b2c3d-1111-2222-3333-444455556666",
])
def test_secretscan_catches_more_shapes(line):
    from taylortrader.secretscan import scan_text

    assert scan_text(line)


@pytest.mark.parametrize(("alerts", "msg"), [
    ({"smtp_port": "x"}, "smtp_port"), ({"email_to": "me@example.com"}, "email_to"),
    ({"ntfy_server": "not a url"}, "ntfy_server"), ({"smtp_host": "a b"}, "smtp_host"),
])
def test_alert_settings_are_validated(alerts, msg):
    from taylortrader.config import ConfigError, parse_config

    with pytest.raises(ConfigError, match=msg):
        parse_config({"alerts": alerts})


def test_safe_defaults_and_trading_feeds():
    from taylortrader.config import ConfigError, parse_config

    cfg = parse_config({})
    assert cfg.dry_run is True and cfg.feed == "yahoo" and cfg.mcp.allow_live_selling is False
    for bad in ({"feed": "sip"}, {"ai": {"endpoint": "http://example.com/v1"}},
                {"alerts": {"ntfy_server": "http://ntfy.example.org"}},
                {"strategies": [{"name": "macd_rsi", "symbols": ["A/../../X"]}]}):
        with pytest.raises(ConfigError):
            parse_config(bad)
    assert parse_config({"ai": {"endpoint": "http://localhost:11434/v1"}}).ai.endpoint.startswith("http://localhost")


def test_pinned_github_actions_arent_secrets():
    from taylortrader.secretscan import scan_text

    sha = "11d5960a" + "0b2e3f4a5c6d7e8f9a0b1c2d3e4f5a6b"
    assert scan_text(f"      - uses: actions/checkout@{sha} # v4.4.0\n") == []
    assert scan_text(f"token: {'A' * 8}{sha}\n") or scan_text(f"secret = \"{'Zz9' * 13}x\"\n")

