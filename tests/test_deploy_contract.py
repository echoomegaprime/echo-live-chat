from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_systemd_release_attestation_shape() -> None:
    unit = (ROOT / "systemd" / "echo-live-chat.service").read_text()
    assert "User=echo-live-chat" in unit
    assert "WorkingDirectory=/opt/echo-live-chat-runtime" in unit
    assert "BindReadOnlyPaths=/home/forge/echo-live-chat/current:/opt/echo-live-chat-runtime" in unit
    assert "/opt/echo-live-chat-runtime/.venv/bin/python -m uvicorn app:app" in unit
    assert "--host 127.0.0.1 --port 8465" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=tmpfs" in unit
    assert "LoadCredential=admin_token:" in unit
    assert "LoadCredential=session_key:" in unit
    assert "LoadCredential=stripe_api_secret:" in unit
    assert "LoadCredential=stripe_webhook_secret:" in unit
    assert "PUBLIC_BASE_URL=https://live-chat.echo-op.com" in unit
    assert ".echo_sovereign_key" not in unit


def test_timer_is_single_native_schedule() -> None:
    timer = (ROOT / "systemd" / "echo-live-chat-maintenance.timer").read_text()
    service = (ROOT / "systemd" / "echo-live-chat-maintenance.service").read_text()
    assert "OnCalendar=*:0/5" in timer
    assert "Persistent=true" in timer
    assert "Unit=echo-live-chat-maintenance.service" in timer
    assert "live_chat_core.py maintenance" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service


def test_deploy_is_staging_first_and_has_real_rollback() -> None:
    deploy = (ROOT / "deploy_echo_live_chat.sh").read_text()
    rollback = (ROOT / "prove_rollback.sh").read_text()
    required = (
        "STAGING_PORT=8466",
        "staging smoke GREEN",
        "rollback_release",
        "LIVE_CHAT_FORCE_PROD_SMOKE_FAIL",
        "promotion failed; rollback smoke GREEN",
        "mv -Tf",
        "systemd-analyze verify",
        "--single-transaction",
        "EXPECTED_CATALOG_SHA=3466f4aa",
        "EXPECTED_STRICT_SHA=08b06de2",
        'git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" diff --quiet',
        'git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" archive --format=tar HEAD',
        '(cd "$RELEASE_DIR" && "$TEST_PYTHON" -m pytest',
        'wait_for_health "$PROD_PORT" || return 1',
        '--admin-token-file "$ADMIN_TOKEN_FILE"',
        "PUBLIC_BASE=https://live-chat.echo-op.com",
        'smoke_live.py" --base "$PUBLIC_BASE"',
        "recovered widget row-count mismatch",
        "typed widget import identity mismatch",
    )
    for marker in required:
        assert marker in deploy
    assert "LIVE_CHAT_FORCE_PROD_SMOKE_FAIL=1" in rollback
    assert '"rollback":"proven"' in rollback


def test_schema_and_contract_names_are_consistent() -> None:
    schema = (ROOT / "schema.sql").read_text()
    contract = json.loads((ROOT / "migration_contract.json").read_text())
    deploy = (ROOT / "deploy_echo_live_chat.sh").read_text()
    assert "CREATE SCHEMA IF NOT EXISTS cf_echo_live_chat" in schema
    assert "cf_echo_live_chat.migration_receipts" in schema
    assert contract["replacement"]["service_dir"] == "/home/forge/echo-live-chat"
    assert contract["runtime"]["api_unit"] == "echo-live-chat.service"
    assert "cf_echo_live_chat.migration_receipts" in deploy
    for column in ("candidate_release", "active_release", "event_name", "recorded_at"):
        assert column in schema
        assert column in deploy
    assert "ADD COLUMN IF NOT EXISTS active_release" in schema
    assert "DROP CONSTRAINT IF EXISTS visitor_sessions_conversation_fk" in schema
    assert "IF existing_definition IS NULL" in schema
    assert "legacy_widgets_text_v1" in schema
    assert "Recovered Widget Tenant" in schema
    assert "REVOKE ALL ON TABLE" in schema
    assert "gen_random_bytes(24)" in schema
