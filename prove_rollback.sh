#!/usr/bin/env bash
# Exercise the real production rollback path without leaving the failed candidate active.
set -euo pipefail

SRC_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BASE_DIR=/opt/echo-live-chat
CURRENT_LINK="$BASE_DIR/current"
BEFORE="$(readlink -f "$CURRENT_LINK")"
set +e
LIVE_CHAT_FORCE_PROD_SMOKE_FAIL=1 "$SRC_DIR/deploy_echo_live_chat.sh" "$SRC_DIR"
rc=$?
set -e
if [ "$rc" -ne 5 ]; then
  echo "forced rollback proof returned unexpected status" >&2
  exit 1
fi
AFTER="$(readlink -f "$CURRENT_LINK")"
[ "$AFTER" = "$BEFORE" ] || { echo "rollback did not restore the prior release" >&2; exit 1; }
systemctl is-active --quiet echo-live-chat.service
curl -fsS --max-time 5 http://127.0.0.1:8465/health >/dev/null
echo '{"ok":true,"rollback":"proven"}'
