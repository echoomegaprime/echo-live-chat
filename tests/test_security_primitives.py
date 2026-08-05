from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

from live_chat_core import (
    Settings,
    constant_time_secret_matches,
    origin_allowed,
    verify_stripe_signature,
    widget_origin_allowed,
)


ROOT = Path(__file__).resolve().parents[1]


def test_admin_secret_comparison_fails_closed() -> None:
    assert constant_time_secret_matches("correct", "correct")
    assert not constant_time_secret_matches("wrong", "correct")
    assert not constant_time_secret_matches("", "")
    assert not constant_time_secret_matches("supplied", "")


def test_origins_are_exact_and_widget_subdomains_are_bounded() -> None:
    allowed = ("https://echo-op.com", "https://www.echo-op.com")
    assert origin_allowed(None, allowed)
    assert origin_allowed("https://echo-op.com", allowed)
    assert not origin_allowed("https://echo-op.com.evil.invalid", allowed)
    assert not origin_allowed("null", allowed)

    assert widget_origin_allowed("https://echo-op.com", ["echo-op.com"])
    assert widget_origin_allowed("https://chat.echo-op.com", ["echo-op.com"])
    assert not widget_origin_allowed("https://echo-op.com.evil.invalid", ["echo-op.com"])
    assert not widget_origin_allowed("not a URL", ["echo-op.com"])


def test_stripe_signature_verifies_timestamp_and_rejects_replay() -> None:
    payload = b'{"id":"evt_test","type":"checkout.session.completed"}'
    secret = "test-signing-secret"
    timestamp = int(time.time())
    signature = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    header = f"t={timestamp},v1={signature}"
    assert verify_stripe_signature(payload, header, secret)
    assert not verify_stripe_signature(payload + b" ", header, secret)
    assert not verify_stripe_signature(payload, header, "wrong")
    assert not verify_stripe_signature(payload, f"t={timestamp - 1000},v1={signature}", secret)
    assert not verify_stripe_signature(payload, header, "")


def test_stripe_api_and_webhook_credentials_are_separate(tmp_path, monkeypatch) -> None:
    api_file = tmp_path / "stripe-api"
    webhook_file = tmp_path / "stripe-webhook"
    api_file.write_text("api-value", encoding="utf-8")
    webhook_file.write_text("webhook-value", encoding="utf-8")
    for name in (
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "ECHO_LIVE_CHAT_STRIPE_SECRET",
        "ECHO_LIVE_CHAT_STRIPE_WEBHOOK_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ECHO_LIVE_CHAT_STRIPE_API_SECRET_FILE", str(api_file))
    monkeypatch.setenv("ECHO_LIVE_CHAT_STRIPE_WEBHOOK_SECRET_FILE", str(webhook_file))
    settings = Settings.from_env()
    assert settings.stripe_secret_key == "api-value"
    assert settings.stripe_webhook_secret == "webhook-value"


def test_keyword_dsn_selects_postgresql_unix_socket(monkeypatch) -> None:
    monkeypatch.setenv("ECHO_LIVE_CHAT_DATABASE_DSN", "dbname=echo user=echo-live-chat")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB", raising=False)
    dsn = Settings.from_env().database_url
    assert dsn.startswith("postgresql:///echo?")
    assert "user=echo-live-chat" in dsn
    assert "host=%2Fvar%2Frun%2Fpostgresql" in dsn
    assert "localhost" not in dsn


def test_smoke_cleanup_is_bounded_and_not_in_strict_decorators() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "left(name,9)='__smoke__'" in source
    assert 'app.add_api_route(\n    "/_smoke/cleanup"' in source
    assert '@app.post("/_smoke/cleanup")' not in source
    assert "SELECT max_agents FROM cf_echo_live_chat.tenants WHERE id=$1 FOR UPDATE" in source
    assert "SELECT max_widgets FROM cf_echo_live_chat.tenants WHERE id=$1 FOR UPDATE" in source
    assert 'raise error("allowed_domains must be an array")' in source
    assert 'raise error("business_hours must be an object")' in source
    assert "json.loads(value)" in source
    assert "openapi_url=None" in source


def test_deploy_passes_value_free_credential_file_to_production_smoke() -> None:
    deploy = (ROOT / "deploy_echo_live_chat.sh").read_text(encoding="utf-8")
    smoke = (ROOT / "smoke_live.py").read_text(encoding="utf-8")
    assert '--admin-token-file "$ADMIN_TOKEN_FILE"' in deploy
    assert 'credential.add_argument("--admin-token-file")' in smoke
    assert "stripe-api-secret" in deploy
    assert "stripe-webhook-secret" in deploy


def test_maintenance_query_avoids_conversation_message_cartesian_join() -> None:
    source = (ROOT / "live_chat_core.py").read_text(encoding="utf-8")
    assert "LEFT JOIN LATERAL" in source
    assert "avg(rating) FILTER" in source
    assert "avg(DISTINCT c.rating)" not in source
    assert "LEFT JOIN cf_echo_live_chat.messages m ON m.tenant_id=t.id" not in source
