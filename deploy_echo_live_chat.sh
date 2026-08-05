#!/usr/bin/env bash
# Staging-first, atomic-release deploy gate for Echo Live Chat.
set -euo pipefail

SRC_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BASE_DIR=/home/forge/echo-live-chat
RELEASES_DIR="$BASE_DIR/releases"
CURRENT_LINK="$BASE_DIR/current"
UNIT=echo-live-chat.service
TIMER=echo-live-chat-maintenance.timer
PROD_PORT=8465
STAGING_PORT=8466
PUBLIC_BASE=https://live-chat.echo-op.com
RUN_USER=echo-live-chat
DB_ROLE=echo-live-chat
CREDENTIAL_DIR=/etc/echo/credentials/echo-live-chat
ADMIN_TOKEN_FILE="$CREDENTIAL_DIR/admin-token"
SESSION_KEY_FILE="$CREDENTIAL_DIR/session-key"
STRIPE_API_SECRET_FILE="$CREDENTIAL_DIR/stripe-api-secret"
STRIPE_WEBHOOK_SECRET_FILE="$CREDENTIAL_DIR/stripe-webhook-secret"
RUNTIME_MOUNT=/opt/echo-live-chat-runtime
STAGING_MOUNT=/opt/echo-live-chat-staging
PROD_MOUNT=/opt/echo-live-chat-runtime
TEST_PYTHON="${LIVE_CHAT_TEST_PYTHON:-/home/forge/echo-worker-server/venv/bin/python}"
RELEASE_ID="$(date -u +%Y%m%dT%H%M%S%NZ)-$(git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || echo source)"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
OLD_TARGET=""
STAGING_UNIT=""
UNIT_BACKUP_DIR="$BASE_DIR/unit-backups/$RELEASE_ID"
EXPECTED_CATALOG_SHA=3466f4aa8d500ef4d4298b49c04166161dd9f42cfcc4044a1538f24ae8a5a521
EXPECTED_STRICT_SHA=08b06de2bf73c901798540b16184dbc77371b795c5a3c40094c76f825537d156
STRICT_SOURCE=/mnt/cf_kv_r2/workers/echo-live-chat/source/index.js

log() { printf '[live-chat-deploy %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

cleanup() {
  if [ -n "$STAGING_UNIT" ]; then
    systemctl stop "$STAGING_UNIT.service" >/dev/null 2>&1 || true
    systemctl reset-failed "$STAGING_UNIT.service" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

wait_for_health() {
  local port="$1"
  for _ in $(seq 1 30); do
    curl -fsS --max-time 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

run_production_smokes() {
  python3 "$CURRENT_LINK/smoke_live.py" --base "http://127.0.0.1:$PROD_PORT" \
    --admin-token-file "$ADMIN_TOKEN_FILE" --session-key-file "$SESSION_KEY_FILE" || return 1
  python3 "$CURRENT_LINK/smoke_live.py" --base "$PUBLIC_BASE" \
    --admin-token-file "$ADMIN_TOKEN_FILE" --session-key-file "$SESSION_KEY_FILE" || return 1
}

record_receipt() {
  local event_name="$1"
  local active_release="${2:-$RELEASE_DIR}"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -d echo \
    -v candidate_release="$RELEASE_DIR" \
    -v active_release="$active_release" \
    -v event_name="$event_name" >/dev/null <<'SQL'
INSERT INTO cf_echo_live_chat.migration_receipts
    (candidate_release, active_release, event_name, recorded_at)
VALUES (:'candidate_release', :'active_release', :'event_name', now())
ON CONFLICT (candidate_release, event_name) DO UPDATE
SET active_release=EXCLUDED.active_release, recorded_at=EXCLUDED.recorded_at;
SQL
}

backup_units() {
  mkdir -p "$UNIT_BACKUP_DIR"
  local name
  for name in "$UNIT" echo-live-chat-maintenance.service "$TIMER"; do
    if [ -f "/etc/systemd/system/$name" ]; then
      cp -a "/etc/systemd/system/$name" "$UNIT_BACKUP_DIR/$name"
    else
      : > "$UNIT_BACKUP_DIR/$name.absent"
    fi
  done
}

restore_units() {
  local name
  for name in "$UNIT" echo-live-chat-maintenance.service "$TIMER"; do
    if [ -f "$UNIT_BACKUP_DIR/$name.absent" ]; then
      rm -f "/etc/systemd/system/$name"
    else
      install -m 0644 "$UNIT_BACKUP_DIR/$name" "/etc/systemd/system/$name"
    fi
  done
}

rollback_release() {
  if [ -z "$OLD_TARGET" ]; then
    systemctl disable --now "$TIMER" "$UNIT" >/dev/null 2>&1 || true
    rm -f "$CURRENT_LINK" "$BASE_DIR/app.py"
    restore_units
    systemctl daemon-reload
    return 0
  fi
  case "$OLD_TARGET" in "$RELEASES_DIR"/*) ;; *) return 1 ;; esac
  ln -s "releases/$(basename "$OLD_TARGET")" "$BASE_DIR/.rollback.$RELEASE_ID"
  mv -Tf "$BASE_DIR/.rollback.$RELEASE_ID" "$CURRENT_LINK"
  ln -sfn current/app.py "$BASE_DIR/app.py"
  restore_units
  systemctl daemon-reload
  systemctl restart "$UNIT" || return 1
  wait_for_health "$PROD_PORT" || return 1
  run_production_smokes || return 1
  record_receipt rollback_smoke "$OLD_TARGET" || return 1
}

if [ "$(id -u)" -ne 0 ]; then
  echo "deploy_echo_live_chat.sh must run as root" >&2
  exit 2
fi
exec 9>/run/lock/echo-live-chat-deploy.lock
flock -n 9 || { echo "another Echo Live Chat deploy holds the release lock" >&2; exit 2; }

for required in app.py live_chat_core.py schema.sql requirements.txt migration_contract.json evidence/route_contract.json smoke_live.py register_public_route.py; do
  [ -f "$SRC_DIR/$required" ] || { echo "missing required release file: $required" >&2; exit 2; }
done
git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "release source is not a Git worktree" >&2; exit 2; }
git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" diff --quiet || { echo "release source has unstaged tracked changes" >&2; exit 2; }
git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" diff --cached --quiet || { echo "release source has staged uncommitted changes" >&2; exit 2; }
[ -z "$(git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" ls-files --others --exclude-standard)" ] || { echo "release source has untracked files" >&2; exit 2; }
[ -r "$STRICT_SOURCE" ] || { echo "strict recovered source is unavailable" >&2; exit 2; }
[ -x "$TEST_PYTHON" ] || { echo "verified test runner is unavailable" >&2; exit 2; }
if ss -ltnH "sport = :$STAGING_PORT" | grep -q .; then
  echo "staging port is occupied" >&2
  exit 2
fi
if [ ! -L "$CURRENT_LINK" ] && ss -ltnH "sport = :$PROD_PORT" | grep -q .; then
  echo "production port is occupied without an active release" >&2
  exit 2
fi

ACTUAL_STRICT_SHA="$(sha256sum "$STRICT_SOURCE" | awk '{print $1}')"
INVENTORY_SHA="$(sudo -u postgres psql -d echo -Atc "SELECT btrim(source_sha256) FROM inventory.cf_migration_status WHERE lower(worker_name)=lower('echo-live-chat')")"
[ "$ACTUAL_STRICT_SHA" = "$EXPECTED_STRICT_SHA" ] || { echo "strict source provenance mismatch" >&2; exit 3; }
[ "$INVENTORY_SHA" = "$EXPECTED_CATALOG_SHA" ] || { echo "catalog source provenance mismatch" >&2; exit 3; }

install -d -m 0755 "$BASE_DIR" "$RELEASES_DIR"
mkdir -m 0755 "$RELEASE_DIR"
git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" archive --format=tar HEAD | tar -xf - -C "$RELEASE_DIR"
chmod -R u=rwX,go=rX "$RELEASE_DIR"
python3 -c "import glob,py_compile; [py_compile.compile(path,doraise=True) for path in glob.glob('$RELEASE_DIR/*.py')]"
(cd "$RELEASE_DIR" && "$TEST_PYTHON" -m pytest -q --confcutdir="$RELEASE_DIR" tests)
python3 -m venv "$RELEASE_DIR/.venv"
PIP_CACHE_DIR="$BASE_DIR/pip-cache" "$RELEASE_DIR/.venv/bin/python" -m pip install \
  --disable-pip-version-check --no-input --only-binary=:all: \
  --requirement "$RELEASE_DIR/requirements.txt" >/dev/null
# systemd verifies ExecStart before it creates the service's read-only bind
# namespace. Keep only an executable mount anchor on the host; at runtime the
# active release is mounted over this directory and supplies the real venv.
install -d -o root -g root -m 0755 "$PROD_MOUNT/.venv/bin"
ln -sfn /usr/bin/python3 "$PROD_MOUNT/.venv/bin/python"
systemd-analyze verify "$RELEASE_DIR/systemd/echo-live-chat.service" \
  "$RELEASE_DIR/systemd/echo-live-chat-maintenance.service" \
  "$RELEASE_DIR/systemd/echo-live-chat-maintenance.timer"

if ! getent passwd "$RUN_USER" >/dev/null; then
  useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin --user-group "$RUN_USER"
fi
[ "$(id -Gn "$RUN_USER")" = "$RUN_USER" ] || { echo "service identity has supplemental groups" >&2; exit 3; }
if ! sudo -u postgres psql -d echo -Atc "SELECT 1 FROM pg_roles WHERE rolname='$DB_ROLE'" | grep -q 1; then
  sudo -u postgres createuser --no-createdb --no-createrole --no-superuser "$DB_ROLE"
fi
role_safe="$(sudo -u postgres psql -d echo -Atc "SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls FROM pg_roles WHERE rolname='$DB_ROLE'")"
[ "$role_safe" = t ] || { echo "database role privilege validation failed" >&2; exit 3; }
sudo -u postgres psql --single-transaction -v ON_ERROR_STOP=1 -d echo < "$RELEASE_DIR/schema.sql" >/dev/null
legacy_widget_rows="$(sudo -u postgres psql -d echo -Atc 'SELECT count(*) FROM cf_echo_live_chat.legacy_widgets_text_v1')"
legacy_other_rows="$(sudo -u postgres psql -d echo -Atc "SELECT
  (SELECT count(*) FROM cf_echo_live_chat.legacy_activity_log_text_v1)+
  (SELECT count(*) FROM cf_echo_live_chat.legacy_agents_text_v1)+
  (SELECT count(*) FROM cf_echo_live_chat.legacy_analytics_daily_text_v1)+
  (SELECT count(*) FROM cf_echo_live_chat.legacy_canned_responses_text_v1)+
  (SELECT count(*) FROM cf_echo_live_chat.legacy_conversations_text_v1)+
  (SELECT count(*) FROM cf_echo_live_chat.legacy_messages_text_v1)+
  (SELECT count(*) FROM cf_echo_live_chat.legacy_tags_text_v1)+
  (SELECT count(*) FROM cf_echo_live_chat.legacy_tenants_text_v1)+
  (SELECT count(*) FROM cf_echo_live_chat.legacy_triggers_text_v1)+
  (SELECT count(*) FROM cf_echo_live_chat.legacy_visitors_text_v1)")"
typed_widget_rows="$(sudo -u postgres psql -d echo -Atc 'SELECT count(*) FROM cf_echo_live_chat.legacy_widgets_text_v1 l JOIN cf_echo_live_chat.widgets w ON w.id=l.id')"
[ "$legacy_widget_rows" = 1 ] || { echo "recovered widget row-count mismatch" >&2; exit 3; }
[ "$legacy_other_rows" = 0 ] || { echo "recovered empty-table row-count mismatch" >&2; exit 3; }
[ "$typed_widget_rows" = 1 ] || { echo "typed widget import identity mismatch" >&2; exit 3; }

install -d -o root -g root -m 0700 "$CREDENTIAL_DIR"
for pair in "$ADMIN_TOKEN_FILE:32" "$SESSION_KEY_FILE:48"; do
  file="${pair%%:*}"; bytes="${pair##*:}"
  [ ! -L "$file" ] || { echo "credential path must not be a symlink" >&2; exit 3; }
  if [ ! -s "$file" ]; then
    umask 077
    python3 -c "import secrets; print(secrets.token_hex($bytes))" > "$file"
  fi
  chown root:root "$file"; chmod 0400 "$file"
done
for file in "$STRIPE_API_SECRET_FILE" "$STRIPE_WEBHOOK_SECRET_FILE"; do
  if [ ! -e "$file" ]; then
    install -o root -g root -m 0400 /dev/null "$file"
  fi
  [ ! -L "$file" ] || { echo "Stripe credential path must not be a symlink" >&2; exit 3; }
  chown root:root "$file"; chmod 0400 "$file"
done

STAGING_ADMIN="live-chat-staging-admin"
STAGING_SESSION="live-chat-staging-session-key-with-sufficient-entropy"
STAGING_UNIT="echo-live-chat-staging-$RELEASE_ID"
systemd-run --quiet --unit="$STAGING_UNIT" \
  --property="User=$RUN_USER" --property="Group=$RUN_USER" \
  --property="WorkingDirectory=$STAGING_MOUNT" \
  --property="BindReadOnlyPaths=$RELEASE_DIR:$STAGING_MOUNT" \
  --property=ProtectHome=tmpfs --property=ProtectSystem=strict \
  --property=PrivateTmp=yes --property=PrivateDevices=yes --property=NoNewPrivileges=yes \
  --setenv="ECHO_LIVE_CHAT_DATABASE_DSN=dbname=echo user=$DB_ROLE" \
  --setenv="ECHO_LIVE_CHAT_ADMIN_TOKEN=$STAGING_ADMIN" \
  --setenv="ECHO_LIVE_CHAT_SESSION_KEY=$STAGING_SESSION" \
  --setenv="ECHO_LIVE_CHAT_CORS_ORIGINS=https://echo-op.com" \
  /usr/bin/env "$STAGING_MOUNT/.venv/bin/python" -m uvicorn app:app \
    --host 127.0.0.1 --port "$STAGING_PORT" --log-level warning --no-access-log
wait_for_health "$STAGING_PORT" || { log "staging readiness RED; production untouched"; exit 4; }
python3 "$RELEASE_DIR/smoke_live.py" --base "http://127.0.0.1:$STAGING_PORT" \
  --admin-token "$STAGING_ADMIN" --session-key "$STAGING_SESSION"
record_receipt staging_smoke
systemctl stop "$STAGING_UNIT.service"
systemctl reset-failed "$STAGING_UNIT.service" >/dev/null 2>&1 || true
STAGING_UNIT=""
log "staging smoke GREEN"

if [ -L "$CURRENT_LINK" ]; then OLD_TARGET="$(readlink -f "$CURRENT_LINK")"; fi
backup_units
install -m 0644 "$RELEASE_DIR/systemd/echo-live-chat.service" "/etc/systemd/system/$UNIT"
install -m 0644 "$RELEASE_DIR/systemd/echo-live-chat-maintenance.service" /etc/systemd/system/echo-live-chat-maintenance.service
install -m 0644 "$RELEASE_DIR/systemd/echo-live-chat-maintenance.timer" "/etc/systemd/system/$TIMER"
ln -s "releases/$RELEASE_ID" "$BASE_DIR/.current.$RELEASE_ID"
mv -Tf "$BASE_DIR/.current.$RELEASE_ID" "$CURRENT_LINK"
ln -sfn current/app.py "$BASE_DIR/app.py"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null

promote_ok=true
systemctl restart "$UNIT" || promote_ok=false
if [ "$promote_ok" = true ]; then wait_for_health "$PROD_PORT" || promote_ok=false; fi
if [ "${LIVE_CHAT_FORCE_PROD_SMOKE_FAIL:-0}" = 1 ]; then promote_ok=false; fi
if [ "$promote_ok" = true ]; then
  run_production_smokes || promote_ok=false
fi
if [ "$promote_ok" != true ]; then
  if rollback_release; then
    log "promotion failed; rollback smoke GREEN"
    exit 5
  fi
  log "promotion and rollback both failed"
  exit 6
fi
systemctl enable --now "$TIMER" >/dev/null
record_receipt production_smoke "$RELEASE_DIR"
log "PROMOTED $RELEASE_ID; production smoke GREEN"
