"""Shared runtime primitives for the Echo Live Chat private-cluster service.

The module contains the stateful boundaries that replace Cloudflare bindings:
PostgreSQL for DB/CACHE/ANALYTICS, bounded HTTP adapters for ENGINE_RUNTIME and
SHARED_BRAIN, signed visitor sessions, Stripe verification, and maintenance.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import shlex
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlparse

import asyncpg
import httpx

SERVICE = "echo-live-chat"
VERSION = "3.0.0"
PLANS: tuple[dict[str, Any], ...] = (
    {
        "id": "free",
        "name": "Free",
        "price": 0,
        "max_websites": 1,
        "max_agents": 1,
        "max_conversations_month": 100,
        "display": "Free",
    },
    {
        "id": "starter",
        "name": "Starter",
        "price": 2999,
        "max_websites": 3,
        "max_agents": 5,
        "max_conversations_month": 1000,
        "display": "$29.99/mo",
    },
    {
        "id": "business",
        "name": "Business",
        "price": 9999,
        "max_websites": 10,
        "max_agents": 25,
        "max_conversations_month": 10000,
        "display": "$99.99/mo",
    },
    {
        "id": "enterprise",
        "name": "Enterprise",
        "price": 29999,
        "max_websites": -1,
        "max_agents": -1,
        "max_conversations_month": -1,
        "display": "$299.99/mo",
    },
)


class JsonFormatter(logging.Formatter):
    """Emit bounded, value-free operational records."""

    _allowed = (
        "event",
        "method",
        "route",
        "status",
        "duration_ms",
        "request_id",
        "dependency",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "service": SERVICE,
            "event": getattr(record, "event", "runtime"),
        }
        for key in self._allowed[1:]:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging() -> logging.Logger:
    """Configure the service logger without request bodies or credentials."""

    logger = logging.getLogger(SERVICE)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger


LOGGER = configure_logging()


@dataclass(frozen=True)
class Settings:
    """Environment-derived service configuration; secret values are never logged."""

    database_url: str
    echo_api_key: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    engine_runtime: str
    shared_brain: str
    analytics: str
    cache: str
    allowed_origins: tuple[str, ...]
    public_base_url: str
    request_timeout: float
    pool_min: int
    pool_max: int
    admin_rate_limit: int
    visitor_rate_limit: int

    @classmethod
    def from_env(cls) -> "Settings":
        def secret_value(primary: str, legacy: str, file_env: str) -> str:
            value = os.getenv(primary) or os.getenv(legacy, "")
            secret_file = os.getenv(file_env, "")
            if not value and secret_file:
                try:
                    value = Path(secret_file).read_text(encoding="utf-8").strip()
                except OSError:
                    value = ""
            return value

        def normalize_dsn(raw: str) -> str:
            if not raw or raw.startswith(("postgres://", "postgresql://")):
                return raw
            options = dict(
                token.split("=", 1) for token in shlex.split(raw) if "=" in token
            )
            database = quote(
                options.get("dbname", options.get("database", "echo")), safe=""
            )
            user = quote(options.get("user", "echo-live-chat"), safe="")
            password = quote(options.get("password", ""), safe="")
            host = options.get("host", "/var/run/postgresql")
            port = options.get("port", "")
            if host.startswith("/"):
                query = f"user={user}&host={quote(host, safe='')}"
                if password:
                    query += f"&password={password}"
                if port:
                    query += f"&port={quote(port, safe='')}"
                return f"postgresql:///{database}?{query}"
            credentials = f"{user}:{password}" if password else user
            authority = f"{credentials}@{host}{f':{port}' if port else ''}"
            return f"postgresql://{authority}/{database}"

        origins = tuple(
            item.strip().rstrip("/")
            for item in os.getenv(
                "LIVE_CHAT_ALLOWED_ORIGINS",
                os.getenv(
                    "ECHO_LIVE_CHAT_CORS_ORIGINS",
                    "https://echo-ept.com,https://echo-op.com",
                ),
            ).split(",")
            if item.strip()
        )
        database_url = normalize_dsn(
            os.getenv("DATABASE_URL")
            or os.getenv("DB")
            or os.getenv("ECHO_LIVE_CHAT_DATABASE_DSN", "")
        )
        if not database_url:
            host = os.getenv("PGHOST", "127.0.0.1")
            port = os.getenv("PGPORT", "5432")
            user = os.getenv("PGUSER", "echo-live-chat")
            database = os.getenv("PGDATABASE", "echo")
            password = os.getenv("PGPASSWORD", "")
            auth = f"{user}:{password}" if password else user
            database_url = f"postgresql://{auth}@{host}:{port}/{database}"
        return cls(
            database_url=database_url,
            echo_api_key=secret_value(
                "ECHO_API_KEY",
                "ECHO_LIVE_CHAT_ADMIN_TOKEN",
                "ECHO_LIVE_CHAT_ADMIN_TOKEN_FILE",
            ),
            stripe_secret_key=secret_value(
                "STRIPE_SECRET_KEY",
                "ECHO_LIVE_CHAT_STRIPE_SECRET",
                "ECHO_LIVE_CHAT_STRIPE_API_SECRET_FILE",
            ),
            stripe_webhook_secret=secret_value(
                "STRIPE_WEBHOOK_SECRET",
                "ECHO_LIVE_CHAT_STRIPE_WEBHOOK_SECRET",
                "ECHO_LIVE_CHAT_STRIPE_WEBHOOK_SECRET_FILE",
            ),
            engine_runtime=os.getenv(
                "ENGINE_RUNTIME",
                os.getenv(
                    "ENGINE_RUNTIME_URL", os.getenv("ECHO_LIVE_CHAT_ENGINE_URL", "")
                ),
            ),
            shared_brain=os.getenv(
                "SHARED_BRAIN",
                os.getenv(
                    "SHARED_BRAIN_URL", os.getenv("ECHO_LIVE_CHAT_BRAIN_URL", "")
                ),
            ),
            analytics=os.getenv("ANALYTICS", "postgresql"),
            cache=os.getenv("CACHE", "postgresql"),
            allowed_origins=origins,
            public_base_url=os.getenv(
                "PUBLIC_BASE_URL", "https://live-chat.echo-op.com"
            ).rstrip("/"),
            request_timeout=max(
                0.5,
                min(
                    float(
                        os.getenv(
                            "UPSTREAM_TIMEOUT_SEC",
                            os.getenv("ECHO_LIVE_CHAT_REQUEST_TIMEOUT", "6"),
                        )
                    ),
                    20.0,
                ),
            ),
            pool_min=max(1, min(int(os.getenv("DB_POOL_MIN", "1")), 10)),
            pool_max=max(2, min(int(os.getenv("DB_POOL_MAX", "10")), 40)),
            admin_rate_limit=max(
                10, min(int(os.getenv("ECHO_LIVE_CHAT_ADMIN_RATE_LIMIT", "120")), 1000)
            ),
            visitor_rate_limit=max(
                5, min(int(os.getenv("ECHO_LIVE_CHAT_VISITOR_RATE_LIMIT", "30")), 200)
            ),
        )


def uid() -> str:
    """Return a compact, non-sequential identifier."""

    return secrets.token_hex(12)


def sanitize(value: Any, maximum: int = 2000) -> str:
    """Strip control characters and enforce a deterministic text bound."""

    text = str(value or "")
    return "".join(ch for ch in text if ch >= " " or ch in "\t\n\r")[:maximum]


def jsonable(value: Any) -> Any:
    """Convert asyncpg records and temporal values into JSON-compatible data."""

    if isinstance(value, asyncpg.Record):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def token_hash(token: str) -> str:
    """Hash opaque bearer material before persistence."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_secret_matches(provided: str, expected: str) -> bool:
    """Compare credentials without length or early-exit timing leaks."""

    supplied_digest = hashlib.sha256(provided.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return bool(expected) and hmac.compare_digest(supplied_digest, expected_digest)


def origin_allowed(origin: str | None, configured: tuple[str, ...]) -> bool:
    """Return whether an administrative browser origin is explicitly allowed."""

    if not origin:
        return True
    normalized = origin.rstrip("/").lower()
    return any(
        hmac.compare_digest(normalized, allowed.lower()) for allowed in configured
    )


def widget_origin_allowed(origin: str | None, domains: Any) -> bool:
    """Validate a widget origin against exact host or true subdomain suffixes."""

    if not domains:
        return True
    if isinstance(domains, str):
        try:
            domains = json.loads(domains)
        except json.JSONDecodeError:
            domains = domains.split(",")
    allowlist = [
        str(item).strip().lower().lstrip(".") for item in domains if str(item).strip()
    ]
    if not allowlist:
        return True
    try:
        host = (urlparse(origin or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return bool(host) and any(
        host == domain or host.endswith(f".{domain}") for domain in allowlist
    )


async def create_pool(settings: Settings) -> asyncpg.Pool:
    """Create and validate the PostgreSQL pool."""

    return await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.pool_min,
        max_size=settings.pool_max,
        command_timeout=10,
        server_settings={
            "application_name": SERVICE,
            "search_path": "cf_echo_live_chat,public",
        },
    )


async def apply_schema(pool: asyncpg.Pool) -> None:
    """Apply the additive service schema from the colocated SQL artifact."""

    sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    async with pool.acquire() as connection:
        await connection.execute(sql)


class DatabaseRateLimiter:
    """Atomic fixed-window limiter backed by PostgreSQL rather than process RAM."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def allow(self, bucket: str, limit: int, window_seconds: int) -> bool:
        now = datetime.now(timezone.utc)
        window_start = datetime.fromtimestamp(
            int(now.timestamp()) // window_seconds * window_seconds, tz=timezone.utc
        )
        expires = window_start + timedelta(seconds=window_seconds * 2)
        row = await self.pool.fetchrow(
            """
            INSERT INTO cf_echo_live_chat.rate_limits(bucket_key, window_started_at, request_count, expires_at)
            VALUES ($1, $2, 1, $3)
            ON CONFLICT (bucket_key) DO UPDATE SET
              window_started_at = CASE
                WHEN cf_echo_live_chat.rate_limits.window_started_at = EXCLUDED.window_started_at
                THEN cf_echo_live_chat.rate_limits.window_started_at ELSE EXCLUDED.window_started_at END,
              request_count = CASE
                WHEN cf_echo_live_chat.rate_limits.window_started_at = EXCLUDED.window_started_at
                THEN cf_echo_live_chat.rate_limits.request_count + 1 ELSE 1 END,
              expires_at = EXCLUDED.expires_at
            RETURNING request_count
            """,
            bucket,
            window_start,
            expires,
        )
        return bool(row and row["request_count"] <= limit)


async def issue_visitor_session(
    connection: asyncpg.Connection,
    *,
    tenant_id: str,
    widget_id: str,
    visitor_id: str,
    conversation_id: str,
    origin: str | None,
) -> str:
    """Persist a tenant-bound visitor session and return its opaque bearer once."""

    token = secrets.token_urlsafe(32)
    await connection.execute(
        """
        INSERT INTO cf_echo_live_chat.visitor_sessions
          (id, tenant_id, widget_id, visitor_id, conversation_id, token_hash, origin, expires_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,now() + interval '24 hours')
        """,
        uid(),
        tenant_id,
        widget_id,
        visitor_id,
        conversation_id,
        token_hash(token),
        origin,
    )
    return token


async def authenticate_visitor(pool: asyncpg.Pool, token: str) -> asyncpg.Record | None:
    """Resolve an unexpired visitor session from an opaque token."""

    if not token or len(token) > 256:
        return None
    row = await pool.fetchrow(
        """
        UPDATE cf_echo_live_chat.visitor_sessions
        SET last_seen_at=now()
        WHERE token_hash=$1 AND expires_at > now()
        RETURNING tenant_id, widget_id, visitor_id, conversation_id, origin
        """,
        token_hash(token),
    )
    return row


class ServiceAdapters:
    """Bounded adapters for Sentinel, engine, brain, analytics, and Stripe."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        timeout = httpx.Timeout(
            settings.request_timeout, connect=min(2.0, settings.request_timeout)
        )
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def suggest(
        self, *, message: str, context: str = "", system: str = ""
    ) -> str | None:
        endpoint = self.settings.engine_runtime.rstrip("/")
        if not endpoint:
            return None
        payload = {
            "question": message,
            "persona": {
                "name": "Echo Support",
                "role": "customer support",
                "tone": "concise, friendly, professional",
            },
            "history": [{"role": "system", "content": sanitize(system, 2000)}]
            if system
            else [],
            "context_tools": [
                {"name": "conversation_context", "summary": sanitize(context, 4000)}
            ]
            if context
            else [],
            "chat_fallback": True,
            "engine_id": "NONE",
        }
        try:
            if endpoint.endswith("/complete"):
                response = await self.client.post(
                    endpoint,
                    json={
                        "system": sanitize(system, 2000),
                        "prompt": sanitize(f"{context}\n{message}", 7000),
                    },
                )
            else:
                response = await self.client.post(f"{endpoint}/answer", json=payload)
            if response.status_code == 404 and not endpoint.endswith("/complete"):
                response = await self.client.post(
                    f"{endpoint}/query",
                    json={
                        "engine_id": "GEN-01",
                        "query": sanitize(f"{system}\n{context}\n{message}", 7000),
                    },
                )
            response.raise_for_status()
            body = response.json()
            return (
                sanitize(
                    body.get("answer") or body.get("response") or body.get("text"), 5000
                )
                or None
            )
        except (httpx.HTTPError, ValueError, TypeError):
            LOGGER.warning(
                "adapter degraded",
                extra={"event": "dependency_degraded", "dependency": "ENGINE_RUNTIME"},
            )
            return None

    async def remember(self, event: str, summary: str) -> None:
        endpoint = self.settings.shared_brain.rstrip("/")
        if not endpoint:
            return
        try:
            if endpoint.endswith("/answer"):
                await self.client.post(
                    endpoint,
                    json={
                        "question": sanitize(summary, 1000),
                        "persona": "echo",
                        "chat_fallback": True,
                        "engine_id": "NONE",
                    },
                )
            else:
                await self.client.post(
                    f"{endpoint}/ingest",
                    json={
                        "role": SERVICE,
                        "kind": event,
                        "content": sanitize(summary, 1000),
                        "importance": 0.3,
                    },
                )
        except httpx.HTTPError:
            LOGGER.warning(
                "adapter degraded",
                extra={"event": "dependency_degraded", "dependency": "SHARED_BRAIN"},
            )

    async def stripe_checkout(
        self, tenant_id: str, plan: Mapping[str, Any], success_url: str, cancel_url: str
    ) -> dict[str, Any]:
        if not self.settings.stripe_secret_key:
            raise RuntimeError("stripe_unconfigured")
        data = {
            "mode": "subscription",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": tenant_id,
            "metadata[tenant_id]": tenant_id,
            "metadata[plan_id]": str(plan["id"]),
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][product_data][name]": f"Echo Live Chat - {plan['name']}",
            "line_items[0][price_data][unit_amount]": str(plan["price"]),
            "line_items[0][price_data][recurring][interval]": "month",
            "line_items[0][quantity]": "1",
        }
        response = await self.client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            data=data,
            headers={"Authorization": f"Bearer {self.settings.stripe_secret_key}"},
        )
        response.raise_for_status()
        return response.json()


def verify_stripe_signature(
    payload: bytes, header: str, secret: str, tolerance_seconds: int = 300
) -> bool:
    """Verify Stripe's timestamped v1 HMAC with replay protection."""

    if not secret or len(payload) > 1_000_000:
        return False
    timestamp = ""
    signatures: list[str] = []
    for part in header.split(","):
        key, separator, value = part.partition("=")
        if not separator:
            continue
        if key.strip() == "t":
            timestamp = value.strip()
        elif key.strip() == "v1":
            signatures.append(value.strip())
    try:
        stamp = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - stamp) > tolerance_seconds:
        return False
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    return any(
        len(signature) == len(expected) and hmac.compare_digest(signature, expected)
        for signature in signatures
    )


async def process_stripe_event(pool: asyncpg.Pool, event: Mapping[str, Any]) -> bool:
    """Idempotently apply supported Stripe subscription state transitions."""

    event_id = sanitize(event.get("id"), 200)
    event_type = sanitize(event.get("type"), 100)
    if not event_id or not event_type:
        raise ValueError("invalid Stripe event")
    payload = (
        event.get("data", {}).get("object", {})
        if isinstance(event.get("data"), Mapping)
        else {}
    )
    async with pool.acquire() as connection, connection.transaction():
        inserted = await connection.fetchval(
            """
            INSERT INTO cf_echo_live_chat.webhook_events(provider,event_id,event_type,payload)
            VALUES ('stripe',$1,$2,$3::jsonb) ON CONFLICT DO NOTHING RETURNING event_id
            """,
            event_id,
            event_type,
            json.dumps(event, separators=(",", ":")),
        )
        if not inserted:
            return False
        metadata = payload.get("metadata", {}) if isinstance(payload, Mapping) else {}
        tenant_id = sanitize(
            metadata.get("tenant_id") or payload.get("client_reference_id"), 100
        )
        plan_id = sanitize(metadata.get("plan_id"), 32)
        if (
            event_type == "checkout.session.completed"
            and tenant_id
            and plan_id in {p["id"] for p in PLANS}
        ):
            plan = next(item for item in PLANS if item["id"] == plan_id)
            updated = await connection.execute(
                """
                UPDATE cf_echo_live_chat.tenants SET plan=$1, max_widgets=$2, max_agents=$3,
                  max_conversations_month=$4, stripe_customer_id=COALESCE($5,stripe_customer_id),
                  stripe_subscription_id=COALESCE($6,stripe_subscription_id), plan_updated_at=now(), updated_at=now()
                WHERE id=$7
                """,
                plan_id,
                plan["max_websites"],
                plan["max_agents"],
                plan["max_conversations_month"],
                sanitize(payload.get("customer"), 200) or None,
                sanitize(payload.get("subscription"), 200) or None,
                tenant_id,
            )
            if not updated.endswith("1"):
                raise ValueError("Stripe checkout event references an unknown tenant")
        elif event_type in {
            "customer.subscription.deleted",
            "customer.subscription.paused",
        }:
            subscription_id = sanitize(payload.get("id"), 200)
            updated = await connection.execute(
                """
                UPDATE cf_echo_live_chat.tenants SET plan='free', max_widgets=1, max_agents=1,
                  max_conversations_month=100, plan_updated_at=now(), updated_at=now()
                WHERE stripe_subscription_id=$1
                """,
                subscription_id,
            )
            if not updated.endswith("1"):
                raise ValueError("Stripe subscription event references an unknown tenant")
    return True


async def run_maintenance(pool: asyncpg.Pool) -> dict[str, int | bool]:
    """Run scheduled rollups and expiry cleanup under a single-flight lock."""

    async with pool.acquire() as connection:
        locked = await connection.fetchval(
            "SELECT pg_try_advisory_lock(hashtext('echo-live-chat-maintenance'))"
        )
        if not locked:
            return {
                "ran": False,
                "analytics_rows": 0,
                "expired_sessions": 0,
                "expired_rates": 0,
            }
        try:
            result = await connection.execute(
                """
                INSERT INTO cf_echo_live_chat.analytics_daily
                  (tenant_id,date,conversations_started,conversations_resolved,messages_sent,messages_received,ai_responses,avg_satisfaction,unique_visitors)
                SELECT t.id, CURRENT_DATE,
                  COALESCE(c.conversations_started,0),
                  COALESCE(c.conversations_resolved,0),
                  COALESCE(m.messages_sent,0),
                  COALESCE(m.messages_received,0),
                  COALESCE(m.ai_responses,0),
                  COALESCE(c.avg_satisfaction,0),
                  COALESCE(c.unique_visitors,0)
                FROM cf_echo_live_chat.tenants t
                LEFT JOIN LATERAL (
                  SELECT
                    count(*) FILTER (WHERE started_at::date=CURRENT_DATE) conversations_started,
                    count(*) FILTER (WHERE closed_at::date=CURRENT_DATE) conversations_resolved,
                    avg(rating) FILTER (WHERE rating IS NOT NULL) avg_satisfaction,
                    count(DISTINCT visitor_id) FILTER (WHERE started_at::date=CURRENT_DATE) unique_visitors
                  FROM cf_echo_live_chat.conversations WHERE tenant_id=t.id
                ) c ON true
                LEFT JOIN LATERAL (
                  SELECT
                    count(*) FILTER (WHERE sender_type='agent' AND created_at::date=CURRENT_DATE) messages_sent,
                    count(*) FILTER (WHERE sender_type='visitor' AND created_at::date=CURRENT_DATE) messages_received,
                    count(*) FILTER (WHERE ai_generated AND created_at::date=CURRENT_DATE) ai_responses
                  FROM cf_echo_live_chat.messages WHERE tenant_id=t.id
                ) m ON true
                ON CONFLICT (tenant_id,date) DO UPDATE SET
                  conversations_started=EXCLUDED.conversations_started,
                  conversations_resolved=EXCLUDED.conversations_resolved,
                  messages_sent=EXCLUDED.messages_sent,
                  messages_received=EXCLUDED.messages_received,
                  ai_responses=EXCLUDED.ai_responses,
                  avg_satisfaction=EXCLUDED.avg_satisfaction,
                  unique_visitors=EXCLUDED.unique_visitors
                """
            )
            expired_sessions = int(
                (
                    await connection.execute(
                        "DELETE FROM cf_echo_live_chat.visitor_sessions WHERE expires_at < now()"
                    )
                ).split()[-1]
            )
            expired_rates = int(
                (
                    await connection.execute(
                        "DELETE FROM cf_echo_live_chat.rate_limits WHERE expires_at < now()"
                    )
                ).split()[-1]
            )
            await connection.execute(
                "DELETE FROM cf_echo_live_chat.activity_log WHERE created_at < now() - interval '30 days'"
            )
            analytics_rows = int(result.split()[-1])
            return {
                "ran": True,
                "analytics_rows": analytics_rows,
                "expired_sessions": expired_sessions,
                "expired_rates": expired_rates,
            }
        finally:
            await connection.execute(
                "SELECT pg_advisory_unlock(hashtext('echo-live-chat-maintenance'))"
            )


def generate_widget_script(widget: Mapping[str, Any], api_base: str) -> str:
    """Generate a dependency-free embed that uses signed visitor sessions."""

    config = json.dumps(
        {
            "id": widget["id"],
            "key": widget["public_key"],
            "api": api_base,
            "color": widget.get("primary_color", "#14b8a6"),
            "greeting": widget.get("greeting", "Hi! How can we help?"),
            "position": widget.get("position", "bottom-right"),
        },
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return f"""(()=>{{'use strict';if(window.__echoLiveChat)return;window.__echoLiveChat=1;const C={config};
const root=document.createElement('div');root.id='echo-live-chat';const side=C.position.includes('left')?'left':'right';
root.innerHTML='<button aria-label="Open support chat">Chat</button><section hidden><header></header><main></main><form><input maxlength="5000" aria-label="Message"><button>Send</button></form></section>';
const style=document.createElement('style');style.textContent=`#echo-live-chat{{position:fixed;bottom:20px;${{side}}:20px;z-index:2147483000;font:14px system-ui}}#echo-live-chat>button,#echo-live-chat form button{{background:${{C.color}};color:#fff;border:0;border-radius:999px;padding:12px 18px}}#echo-live-chat section{{width:min(360px,calc(100vw - 32px));height:480px;background:#fff;color:#111;border:1px solid #ddd;border-radius:16px;box-shadow:0 16px 45px #0003;overflow:hidden}}#echo-live-chat header{{padding:16px;background:${{C.color}};color:#fff}}#echo-live-chat main{{height:350px;overflow:auto;padding:12px}}#echo-live-chat form{{display:flex;padding:10px;border-top:1px solid #ddd}}#echo-live-chat input{{flex:1;padding:10px}}`;document.head.append(style);document.body.append(root);
const open=root.querySelector(':scope>button'),panel=root.querySelector('section'),head=root.querySelector('header'),main=root.querySelector('main'),form=root.querySelector('form'),input=root.querySelector('input');head.textContent=C.greeting;let session='',cid='',after='';
async function init(){{const r=await fetch(C.api+'/v/init',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{widget_id:C.id,widget_key:C.key,page_url:location.href,referrer:document.referrer,user_agent:navigator.userAgent}})}});if(!r.ok)throw Error('chat unavailable');const d=await r.json();session=d.session_token;cid=d.conversation_id;}}
function add(text,mine){{const p=document.createElement('p');p.textContent=text;p.style.textAlign=mine?'right':'left';main.append(p);main.scrollTop=main.scrollHeight;}}
async function poll(){{if(!session)return;const r=await fetch(C.api+'/v/messages?after='+encodeURIComponent(after),{{headers:{{authorization:'Bearer '+session}}}});if(r.ok){{for(const m of await r.json()){{if(m.sender_type!=='visitor')add(m.content,false);after=m.created_at;}}}}}}
open.onclick=async()=>{{panel.hidden=!panel.hidden;if(!panel.hidden&&!session){{try{{await init();setInterval(poll,3000)}}catch(_e){{head.textContent='Chat is temporarily unavailable'}}}}}};
form.onsubmit=async e=>{{e.preventDefault();const content=input.value.trim();if(!content||!session)return;input.value='';add(content,true);await fetch(C.api+'/v/message',{{method:'POST',headers:{{'content-type':'application/json',authorization:'Bearer '+session}},body:JSON.stringify({{content}})}});}};}})();"""


async def _maintenance_command() -> int:
    settings = Settings.from_env()
    pool = await create_pool(settings)
    try:
        result = await run_maintenance(pool)
        LOGGER.info(
            "maintenance",
            extra={
                "event": "maintenance_complete",
                "status": 200 if result["ran"] else 204,
            },
        )
        return 0
    finally:
        await pool.close()


def main() -> int:
    """Run the native scheduled-maintenance entrypoint."""

    parser = argparse.ArgumentParser(description="Echo Live Chat runtime operations")
    parser.add_argument("command", choices=("maintenance",))
    arguments = parser.parse_args()
    if arguments.command == "maintenance":
        return asyncio.run(_maintenance_command())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
