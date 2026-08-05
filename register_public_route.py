#!/usr/bin/env python3
"""Idempotently route live-chat.echo-op.com to the FORGE loopback runtime."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


GATE = "http://127.0.0.1:8000"
CF_API = "https://api.cloudflare.com/client/v4"
ACCOUNT_ID = "b9af3a4bf161132bb7e5d3d365fb8bb0"
TUNNEL_ID = "53f370a8-78c8-4146-8b57-f1577f85b327"
CF_EMAIL = "bmcii1976@gmail.com"
HOSTNAME = "live-chat.echo-op.com"
SERVICE = "http://127.0.0.1:8465"


def sovereign_key() -> str:
    text = Path("/home/forge/.echo_sovereign_key").read_text(encoding="utf-8")
    match = re.search(r"^SOVEREIGN_KEY\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise RuntimeError("sovereign key file is unavailable")
    return match.group(1)


def vault_global_key() -> str:
    payload = {
        "envelope_version": 1,
        "capability": "echo.vault.get",
        "params": {"command": "get", "service": "cloudflare_global_api_key"},
        "context": {
            "bypass_reason": "Register the approved Echo Live Chat public tunnel route without exposing credentials."
        },
    }
    request = urllib.request.Request(
        f"{GATE}/sdk/invoke",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Echo-API-Key": sovereign_key(),
        },
        method="POST",
    )
    response = json.loads(urllib.request.urlopen(request, timeout=20).read())
    return str(response["result"]["body"]["secret"])


def cloudflare_request(
    method: str, path: str, global_key: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{CF_API}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={
            "X-Auth-Email": CF_EMAIL,
            "X-Auth-Key": global_key,
            "Content-Type": "application/json",
        },
        method=method,
    )
    response = json.loads(urllib.request.urlopen(request, timeout=30).read())
    if not response.get("success"):
        raise RuntimeError(f"Cloudflare rejected tunnel update: {response.get('errors')}")
    return response


def desired_config(config: dict[str, Any]) -> tuple[dict[str, Any], int, bool]:
    ingress = list(config.get("ingress") or [])
    existing = [
        row
        for row in ingress
        if row.get("hostname") == HOSTNAME and row.get("service") == SERVICE
    ]
    without_target = [row for row in ingress if row.get("hostname") != HOSTNAME]
    index = len(without_target)
    for position, row in enumerate(without_target):
        hostname = str(row.get("hostname") or "")
        if (
            hostname.startswith("*.")
            or "hostname" not in row
            or str(row.get("service") or "").startswith("http_status")
        ):
            index = position
            break
    without_target.insert(index, {"hostname": HOSTNAME, "service": SERVICE})
    updated = dict(config)
    updated["ingress"] = without_target
    unchanged = bool(existing) and ingress == without_target
    return updated, index, unchanged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        global_key = vault_global_key()
        path = f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{TUNNEL_ID}/configurations"
        current = cloudflare_request("GET", path, global_key)["result"]["config"]
        updated, index, unchanged = desired_config(current)
        if args.verify_only and not unchanged:
            raise RuntimeError("public route is absent, duplicated, misplaced, or stale")
        if not unchanged:
            cloudflare_request("PUT", path, global_key, {"config": updated})
        verified = cloudflare_request("GET", path, global_key)["result"]["config"]
        _, verified_index, verified_unchanged = desired_config(verified)
        if not verified_unchanged:
            raise RuntimeError("public route did not round-trip exactly")
        print(
            json.dumps(
                {
                    "ok": True,
                    "hostname": HOSTNAME,
                    "service": SERVICE,
                    "index": verified_index,
                    "changed": not unchanged,
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, KeyError, RuntimeError, urllib.error.URLError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
