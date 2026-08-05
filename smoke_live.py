#!/usr/bin/env python3
"""Live release smoke for every route in the recovered Worker contract."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REQUIRED_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
}


class SmokeFailure(RuntimeError):
    pass


@dataclass
class Result:
    status: int
    headers: dict[str, str]
    body: Any


class Client:
    def __init__(self, base: str, admin_token: str) -> None:
        self.base = base.rstrip("/")
        self.admin_token = admin_token
        self.tested: set[tuple[str, str]] = set()

    def request(
        self,
        method: str,
        path: str,
        *,
        contract_path: str | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Result:
        encoded = None
        merged_headers = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
            merged_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base}{path}", data=encoded, headers=merged_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                status = response.status
                raw = response.read()
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
        except urllib.error.URLError as exc:
            raise SmokeFailure(f"{method} {path}: connection failed: {exc.reason}") from exc

        if status not in expected:
            preview = raw.decode("utf-8", "replace")[:500]
            raise SmokeFailure(
                f"{method} {path}: expected {expected}, got {status}: {preview}"
            )
        for key, value in REQUIRED_SECURITY_HEADERS.items():
            if response_headers.get(key) != value:
                raise SmokeFailure(
                    f"{method} {path}: missing or invalid security header {key}"
                )
        if contract_path:
            self.tested.add((method, contract_path))

        content_type = response_headers.get("content-type", "")
        if "json" in content_type and raw:
            try:
                parsed: Any = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SmokeFailure(f"{method} {path}: invalid JSON response") from exc
        else:
            parsed = raw.decode("utf-8", "replace")
        return Result(status=status, headers=response_headers, body=parsed)

    def admin_headers(self, tenant_id: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        if tenant_id:
            headers["X-Tenant-ID"] = tenant_id
        return headers


def load_contract() -> set[tuple[str, str]]:
    contract_path = Path(__file__).with_name("evidence") / "route_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    return {(route["method"], route["path"]) for route in contract["routes"]}


def require_mapping(result: Result, operation: str) -> dict[str, Any]:
    if not isinstance(result.body, dict):
        raise SmokeFailure(f"{operation}: expected a JSON object")
    return result.body


def run(base: str, admin_token: str) -> dict[str, Any]:
    client = Client(base, admin_token)
    expected_contract = load_contract()
    tenant_ids: list[str] = []
    cleanup_error = ""
    suffix = uuid.uuid4().hex[:12]
    origin = "https://echo-op.com"

    try:
        for path in ("/health", "/", "/status", "/diagnostics"):
            result = client.request("GET", path)
            data = require_mapping(result, path)
            if not data.get("ok"):
                raise SmokeFailure(f"GET {path}: unhealthy response")

        client.request("GET", "/agents", expected=(401,))
        client.request(
            "GET",
            "/agents",
            headers={"Authorization": "Bearer invalid-smoke-token", "X-Tenant-ID": "none"},
            expected=(401,),
        )
        client.request(
            "GET",
            "/agents",
            headers={"Origin": "https://evil.invalid"},
            expected=(403,),
        )
        client.request(
            "POST",
            "/tenants",
            body={"name": "x" * 1_048_577},
            headers=client.admin_headers(),
            expected=(413,),
        )

        tenant = require_mapping(
            client.request(
                "POST",
                "/tenants",
                contract_path="/tenants",
                body={"name": f"__smoke__{suffix}", "domain": "echo-op.com"},
                headers=client.admin_headers(),
            ),
            "create tenant",
        )
        tenant_id = str(tenant["id"])
        tenant_ids.append(tenant_id)
        widget_id = str(tenant["widget_id"])
        widget_key = str(tenant["widget_key"])
        admin = client.admin_headers(tenant_id)

        migrated = require_mapping(client.request(
            "POST",
            "/admin/migrate-stripe",
            contract_path="/admin/migrate-stripe",
            body={},
            headers=client.admin_headers(),
        ), "migrate Stripe schema")
        if not migrated.get("ok"):
            raise SmokeFailure("POST /admin/migrate-stripe: schema readiness is false")
        agent = require_mapping(
            client.request(
                "POST",
                "/agents",
                contract_path="/agents",
                body={
                    "name": "Release Smoke Agent",
                    "email": f"smoke-{suffix}@example.invalid",
                    "role": "admin",
                },
                headers=admin,
            ),
            "create agent",
        )
        agent_id = str(agent["id"])
        canned = require_mapping(
            client.request(
                "POST",
                "/canned",
                contract_path="/canned",
                body={
                    "shortcut": f"smoke-{suffix}",
                    "title": "Release smoke",
                    "content": "This is an automated release verification response.",
                },
                headers=admin,
            ),
            "create canned response",
        )
        canned_id = str(canned["id"])
        trigger = require_mapping(
            client.request(
                "POST",
                "/triggers",
                contract_path="/triggers",
                body={
                    "name": f"Release smoke {suffix}",
                    "event": "page_view",
                    "conditions": {"path": "/smoke"},
                    "actions": [{"type": "noop"}],
                },
                headers=admin,
            ),
            "create trigger",
        )
        trigger_id = str(trigger["id"])
        client.request(
            "POST",
            "/tags",
            contract_path="/tags",
            body={"name": f"release-smoke-{suffix}", "color": "#14b8a6"},
            headers=admin,
        )

        client.request("GET", "/agents", contract_path="/agents", headers=admin)
        client.request(
            "GET", "/analytics/agents", contract_path="/analytics/agents", headers=admin
        )
        client.request(
            "GET", "/analytics/daily?days=7", contract_path="/analytics/daily", headers=admin
        )
        client.request(
            "GET", "/analytics/overview", contract_path="/analytics/overview", headers=admin
        )
        client.request("GET", "/canned", contract_path="/canned", headers=admin)
        client.request(
            "GET", "/conversations", contract_path="/conversations", headers=admin
        )
        client.request("GET", "/plans", contract_path="/plans")
        client.request("GET", "/tags", contract_path="/tags", headers=admin)
        client.request("GET", "/tenants/me", contract_path="/tenants/me", headers=admin)
        client.request("GET", "/triggers", contract_path="/triggers", headers=admin)
        client.request("GET", "/visitors", contract_path="/visitors", headers=admin)
        client.request("GET", "/widgets", contract_path="/widgets", headers=admin)
        client.request(
            "GET", f"/widgets/{widget_id}", contract_path="/widgets/{}", headers=admin
        )
        script = client.request(
            "GET",
            f"/widget.js?{urllib.parse.urlencode({'id': widget_id})}",
            contract_path="/widget.js",
        )
        if "__echoLiveChat" not in str(script.body):
            raise SmokeFailure("GET /widget.js: expected widget bootstrap marker")

        other = require_mapping(
            client.request(
                "POST",
                "/tenants",
                contract_path="/tenants",
                body={"name": f"__smoke__other-{suffix}", "domain": "echo-op.com"},
                headers=client.admin_headers(),
            ),
            "create isolation tenant",
        )
        other_tenant_id = str(other["id"])
        tenant_ids.append(other_tenant_id)
        client.request(
            "GET",
            f"/widgets/{widget_id}",
            contract_path="/widgets/{}",
            headers=client.admin_headers(other_tenant_id),
            expected=(404,),
        )

        client.request(
            "PUT",
            f"/agents/{agent_id}",
            contract_path="/agents/{}",
            body={"status": "online", "auto_assign": False},
            headers=admin,
        )
        client.request(
            "PUT",
            f"/widgets/{widget_id}",
            contract_path="/widgets/{}",
            body={"greeting": "Release smoke ready", "allowed_domains": ["echo-op.com"]},
            headers=admin,
        )
        client.request(
            "PUT",
            f"/widgets/{widget_id}",
            contract_path="/widgets/{}",
            body={"allowed_domains": "echo-op.com"},
            headers=admin,
            expected=(400,),
        )
        client.request(
            "POST",
            "/widgets",
            contract_path="/widgets",
            body={"name": "Over-limit smoke widget"},
            headers=admin,
            expected=(409,),
        )
        client.request(
            "POST",
            "/plans/upgrade",
            contract_path="/plans/upgrade",
            body={"plan_id": "free"},
            headers=admin,
            expected=(400,),
        )
        client.request(
            "POST",
            "/ai/suggest-reply",
            contract_path="/ai/suggest-reply",
            body={"message": "Can you help me?", "context": "Release smoke"},
            headers=admin,
        )
        client.request(
            "POST",
            "/ai/auto-tag",
            contract_path="/ai/auto-tag",
            body={"messages": "Customer asks for release verification."},
            headers=admin,
        )

        visitor = require_mapping(
            client.request(
                "POST",
                "/v/init",
                contract_path="/v/init",
                body={
                    "widget_id": widget_id,
                    "widget_key": widget_key,
                    "name": "Release Visitor",
                    "page_url": f"{origin}/smoke",
                },
                headers={"Origin": origin},
            ),
            "visitor init",
        )
        visitor_id = str(visitor["visitor_id"])
        conversation_id = str(visitor["conversation_id"])
        visitor_headers = {
            "Authorization": f"Bearer {visitor['session_token']}",
            "Origin": origin,
        }
        client.request(
            "GET",
            "/v/messages",
            contract_path="/v/messages",
            headers={
                "Authorization": f"Bearer {visitor['session_token']}",
                "Origin": "https://evil.invalid",
            },
            expected=(403,),
        )
        client.request(
            "POST",
            "/v/message",
            contract_path="/v/message",
            body={"content": "Release smoke message", "visitor_name": "Release Visitor"},
            headers=visitor_headers,
        )
        client.request(
            "GET", "/v/messages", contract_path="/v/messages", headers=visitor_headers
        )
        client.request(
            "POST",
            "/v/visitor",
            contract_path="/v/visitor",
            body={
                "name": "Release Visitor Updated",
                "email": f"visitor-{suffix}@example.invalid",
                "custom_data": {"source": "release-smoke"},
            },
            headers=visitor_headers,
        )
        rating = require_mapping(
            client.request(
                "POST",
                "/v/rate",
                contract_path="/v/rate",
                body={"rating": 5, "feedback": "Automated release verification"},
                headers=visitor_headers,
            ),
            "visitor rating",
        )
        if not rating.get("updated"):
            raise SmokeFailure("POST /v/rate: conversation was not updated")
        client.request(
            "GET",
            f"/conversations/{conversation_id}",
            contract_path="/conversations/{}",
            headers=admin,
        )
        client.request(
            "GET",
            f"/visitors/{visitor_id}",
            contract_path="/visitors/{}",
            headers=admin,
        )

        client.request(
            "POST",
            "/webhooks/stripe",
            contract_path="/webhooks/stripe",
            body={"id": f"evt_smoke_{suffix}", "type": "customer.subscription.updated"},
            headers={"Stripe-Signature": "t=0,v1=invalid"},
            expected=(400,),
        )

        deleted_agent = require_mapping(
            client.request(
                "DELETE",
                f"/agents/{agent_id}",
                contract_path="/agents/{}",
                headers=admin,
            ),
            "delete agent",
        )
        deleted_canned = require_mapping(
            client.request(
                "DELETE",
                f"/canned/{canned_id}",
                contract_path="/canned/{}",
                headers=admin,
            ),
            "delete canned response",
        )
        deleted_trigger = require_mapping(
            client.request(
                "DELETE",
                f"/triggers/{trigger_id}",
                contract_path="/triggers/{}",
                headers=admin,
            ),
            "delete trigger",
        )
        if not all(
            result.get("deleted")
            for result in (deleted_agent, deleted_canned, deleted_trigger)
        ):
            raise SmokeFailure("DELETE semantic check failed")

        missing = expected_contract - client.tested
        unexpected = client.tested - expected_contract
        if missing or unexpected:
            raise SmokeFailure(
                f"route coverage mismatch: missing={sorted(missing)} unexpected={sorted(unexpected)}"
            )
    finally:
        for tenant_id in reversed(tenant_ids):
            try:
                cleanup = client.request(
                    "POST",
                    "/_smoke/cleanup",
                    body={"tenant_id": tenant_id},
                    headers={
                        **client.admin_headers(),
                        "X-Echo-Smoke-Test": "1",
                    },
                )
                if not require_mapping(cleanup, "smoke cleanup").get("deleted"):
                    cleanup_error = f"smoke tenant {tenant_id[:8]} was not deleted"
            except SmokeFailure as exc:
                cleanup_error = str(exc)

    if cleanup_error:
        raise SmokeFailure(cleanup_error)
    return {
        "ok": True,
        "source_routes": len(expected_contract),
        "tested_routes": len(client.tested),
        "coverage": round(len(client.tested) / len(expected_contract), 4),
        "security_headers": sorted(REQUIRED_SECURITY_HEADERS),
        "smoke_data_cleaned": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    credential = parser.add_mutually_exclusive_group(required=True)
    credential.add_argument("--admin-token")
    credential.add_argument("--admin-token-file")
    parser.add_argument("--session-key", default="")
    parser.add_argument("--session-key-file", default="")
    args = parser.parse_args()
    admin_token = args.admin_token or ""
    if args.admin_token_file:
        try:
            admin_token = Path(args.admin_token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(json.dumps({"ok": False, "error": f"credential file unavailable: {exc}"}))
            return 1
    if not admin_token:
        print(json.dumps({"ok": False, "error": "admin credential is empty"}))
        return 1
    try:
        result = run(args.base, admin_token)
    except (SmokeFailure, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
