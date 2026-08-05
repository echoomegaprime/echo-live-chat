"""FastAPI/PostgreSQL replacement for the recovered Echo Live Chat Worker."""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import asyncpg
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from live_chat_core import (
    LOGGER,
    PLANS,
    SERVICE,
    VERSION,
    DatabaseRateLimiter,
    ServiceAdapters,
    Settings,
    apply_schema,
    authenticate_visitor,
    constant_time_secret_matches,
    create_pool,
    generate_widget_script,
    issue_visitor_session,
    jsonable,
    origin_allowed,
    process_stripe_event,
    sanitize,
    uid,
    verify_stripe_signature,
    widget_origin_allowed,
)

SETTINGS = Settings.from_env()
PLAN_BY_ID = {plan["id"]: plan for plan in PLANS}


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    pool = await create_pool(SETTINGS)
    if os.getenv("APPLY_SCHEMA_ON_START", "0") == "1":
        await apply_schema(pool)
    await pool.fetchval("SELECT 1")
    application.state.pool = pool
    application.state.rate_limiter = DatabaseRateLimiter(pool)
    application.state.adapters = ServiceAdapters(SETTINGS)
    yield
    await application.state.adapters.close()
    await pool.close()


app = FastAPI(
    title="Echo Live Chat",
    version=VERSION,
    lifespan=lifespan,
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)


def error(
    detail: str, status: int = 400, headers: dict[str, str] | None = None
) -> HTTPException:
    return HTTPException(status_code=status, detail=detail, headers=headers)


def bearer(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    return (
        authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    )


async def require_admin(
    request: Request, x_echo_api_key: str = Header(default="")
) -> None:
    provided = x_echo_api_key or bearer(request)
    if not constant_time_secret_matches(provided, SETTINGS.echo_api_key):
        raise error("Unauthorized", 401)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        allowed = await request.app.state.rate_limiter.allow(
            client_bucket(request, "admin-write"), SETTINGS.admin_rate_limit, 60
        )
        if not allowed:
            raise error("Rate limited", 429, {"Retry-After": "60"})


async def tenant_id(request: Request, _auth: None = Depends(require_admin)) -> str:
    value = sanitize(
        request.headers.get("x-tenant-id") or request.query_params.get("tenant_id"), 100
    )
    if not value:
        raise error("Tenant ID required", 400)
    exists = await request.app.state.pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM cf_echo_live_chat.tenants WHERE id=$1 AND status <> 'deleted')",
        value,
    )
    if not exists:
        raise error("Tenant not found", 404)
    return value


async def body_dict(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise error("Invalid JSON body", 400) from None
    if not isinstance(body, dict):
        raise error("JSON object required", 400)
    return body


def visitor_token(request: Request) -> str:
    return bearer(request) or sanitize(request.headers.get("x-visitor-token"), 256)


async def visitor_session(request: Request) -> asyncpg.Record:
    session = await authenticate_visitor(request.app.state.pool, visitor_token(request))
    if not session:
        raise error("Invalid or expired visitor session", 401)
    origin = request.headers.get("origin")
    if (
        session["origin"]
        and origin
        and session["origin"].rstrip("/") != origin.rstrip("/")
    ):
        raise error("Origin does not match session", 403)
    return session


def set_visitor_cors(response: Response, origin: str | None) -> None:
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"


def decoded_json(value: Any, expected_type: type, fallback: Any) -> Any:
    """Normalize asyncpg's default JSONB strings before partial-update merging."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value if isinstance(value, expected_type) else fallback


def client_bucket(request: Request, namespace: str) -> str:
    peer = request.client.host if request.client else "unknown"
    forwarded = ""
    if peer in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    address = forwarded or peer
    return f"{namespace}:{hashlib.sha256(address.encode()).hexdigest()[:24]}"


@app.middleware("http")
async def security_and_observability(request: Request, call_next: Any) -> Response:
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id", "")[:64] or uid()
    origin = request.headers.get("origin")
    is_visitor = request.url.path.startswith("/v/") or request.url.path == "/widget.js"
    is_webhook = request.url.path == "/webhooks/stripe"
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > 1_048_576:
        response = JSONResponse(
            {"ok": False, "error": "Request body too large"}, status_code=413
        )
    elif (
        origin
        and not is_visitor
        and not is_webhook
        and not origin_allowed(origin, SETTINGS.allowed_origins)
    ):
        response = JSONResponse(
            {"ok": False, "error": "Origin not allowed"}, status_code=403
        )
    elif request.method == "OPTIONS":
        if not origin_allowed(
            origin, SETTINGS.allowed_origins
        ) and not request.url.path.startswith("/v/"):
            response = Response(status_code=403)
        else:
            response = Response(status_code=204)
            if origin:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, DELETE, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-Echo-API-Key, X-Tenant-ID, X-Visitor-Token"
            )
            response.headers["Access-Control-Max-Age"] = "600"
    else:
        response = await call_next(request)
        if origin and not is_visitor and not is_webhook:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
    response.headers.update(
        {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            "X-Request-ID": request_id,
        }
    )
    response.headers.setdefault("Cache-Control", "no-store")
    route = getattr(request.scope.get("route"), "path", request.url.path)
    LOGGER.info(
        "request",
        extra={
            "event": "request",
            "method": request.method,
            "route": route,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "request_id": request_id,
        },
    )
    return response


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": str(exc.detail)},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(
    _request: Request, _exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse({"ok": False, "error": "Invalid request"}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_error(_request: Request, _exc: Exception) -> JSONResponse:
    LOGGER.error("unhandled", extra={"event": "unhandled_error"})
    return JSONResponse(
        {"ok": False, "error": "Internal server error"}, status_code=500
    )


@app.delete("/agents/{id}")
async def delete_agent(
    id: str, request: Request, tid: str = Depends(tenant_id)
) -> dict[str, Any]:
    result = await request.app.state.pool.execute(
        "DELETE FROM cf_echo_live_chat.agents WHERE id=$1 AND tenant_id=$2", id, tid
    )
    return {"ok": True, "deleted": result.endswith("1")}


@app.delete("/canned/{id}")
async def delete_canned(
    id: str, request: Request, tid: str = Depends(tenant_id)
) -> dict[str, Any]:
    result = await request.app.state.pool.execute(
        "DELETE FROM cf_echo_live_chat.canned_responses WHERE id=$1 AND tenant_id=$2",
        id,
        tid,
    )
    return {"ok": True, "deleted": result.endswith("1")}


@app.delete("/triggers/{id}")
async def delete_trigger(
    id: str, request: Request, tid: str = Depends(tenant_id)
) -> dict[str, Any]:
    result = await request.app.state.pool.execute(
        "DELETE FROM cf_echo_live_chat.triggers WHERE id=$1 AND tenant_id=$2", id, tid
    )
    return {"ok": True, "deleted": result.endswith("1")}


@app.get("/agents")
async def list_agents(request: Request, tid: str = Depends(tenant_id)) -> Any:
    return jsonable(
        await request.app.state.pool.fetch(
            "SELECT * FROM cf_echo_live_chat.agents WHERE tenant_id=$1 ORDER BY created_at",
            tid,
        )
    )


@app.get("/analytics/agents")
async def analytics_agents(request: Request, tid: str = Depends(tenant_id)) -> Any:
    rows = await request.app.state.pool.fetch(
        """
        SELECT a.id,a.name,a.status,
          count(c.id) FILTER (WHERE c.status IN ('open','active')) AS active_conversations,
          count(c.id) AS total_conversations, avg(c.rating) AS avg_rating
        FROM cf_echo_live_chat.agents a LEFT JOIN cf_echo_live_chat.conversations c
          ON c.assigned_agent_id=a.id AND c.tenant_id=a.tenant_id
        WHERE a.tenant_id=$1 GROUP BY a.id ORDER BY a.name
    """,
        tid,
    )
    return jsonable(rows)


@app.get("/analytics/daily")
async def analytics_daily(
    request: Request, days: int = Query(30, ge=1, le=90), tid: str = Depends(tenant_id)
) -> Any:
    return jsonable(
        await request.app.state.pool.fetch(
            "SELECT * FROM cf_echo_live_chat.analytics_daily WHERE tenant_id=$1 AND date >= CURRENT_DATE-$2::int ORDER BY date",
            tid,
            days,
        )
    )


@app.get("/analytics/overview")
async def analytics_overview(request: Request, tid: str = Depends(tenant_id)) -> Any:
    row = await request.app.state.pool.fetchrow(
        """
        SELECT (SELECT count(*) FROM cf_echo_live_chat.conversations WHERE tenant_id=$1) total_conversations,
          (SELECT count(*) FROM cf_echo_live_chat.messages WHERE tenant_id=$1) total_messages,
          (SELECT count(*) FROM cf_echo_live_chat.visitors WHERE tenant_id=$1) total_visitors,
          (SELECT count(*) FROM cf_echo_live_chat.agents WHERE tenant_id=$1) total_agents,
          (SELECT count(*) FROM cf_echo_live_chat.conversations WHERE tenant_id=$1 AND status IN ('open','active')) open_conversations,
          (SELECT avg(rating) FROM cf_echo_live_chat.conversations WHERE tenant_id=$1 AND rating IS NOT NULL) avg_rating
    """,
        tid,
    )
    return jsonable(row)


@app.get("/canned")
async def list_canned(request: Request, tid: str = Depends(tenant_id)) -> Any:
    return jsonable(
        await request.app.state.pool.fetch(
            "SELECT * FROM cf_echo_live_chat.canned_responses WHERE tenant_id=$1 ORDER BY use_count DESC,title",
            tid,
        )
    )


@app.get("/conversations")
async def list_conversations(
    request: Request,
    status: str | None = None,
    agent_id: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    tid: str = Depends(tenant_id),
) -> Any:
    return jsonable(
        await request.app.state.pool.fetch(
            """
        SELECT c.*,v.name visitor_name,v.email visitor_email,a.name agent_name
        FROM cf_echo_live_chat.conversations c
        LEFT JOIN cf_echo_live_chat.visitors v ON v.id=c.visitor_id AND v.tenant_id=c.tenant_id
        LEFT JOIN cf_echo_live_chat.agents a ON a.id=c.assigned_agent_id AND a.tenant_id=c.tenant_id
        WHERE c.tenant_id=$1 AND ($2::text IS NULL OR c.status=$2) AND ($3::text IS NULL OR c.assigned_agent_id=$3)
        ORDER BY c.last_message_at DESC LIMIT $4
    """,
            tid,
            status,
            agent_id,
            limit,
        )
    )


@app.get("/conversations/{id}")
async def get_conversation(
    id: str, request: Request, tid: str = Depends(tenant_id)
) -> Any:
    row = await request.app.state.pool.fetchrow(
        """
        SELECT c.*,v.name visitor_name,v.email visitor_email,v.page_url,v.country,v.city,v.custom_data,a.name agent_name
        FROM cf_echo_live_chat.conversations c
        LEFT JOIN cf_echo_live_chat.visitors v ON v.id=c.visitor_id AND v.tenant_id=c.tenant_id
        LEFT JOIN cf_echo_live_chat.agents a ON a.id=c.assigned_agent_id AND a.tenant_id=c.tenant_id
        WHERE c.id=$1 AND c.tenant_id=$2
    """,
        id,
        tid,
    )
    if not row:
        raise error("Conversation not found", 404)
    result = jsonable(row)
    result["messages"] = jsonable(
        await request.app.state.pool.fetch(
            "SELECT * FROM cf_echo_live_chat.messages WHERE conversation_id=$1 AND tenant_id=$2 ORDER BY created_at LIMIT 200",
            id,
            tid,
        )
    )
    return result


@app.get("/plans")
async def plans() -> Any:
    return PLANS


@app.get("/tags")
async def list_tags(request: Request, tid: str = Depends(tenant_id)) -> Any:
    return jsonable(
        await request.app.state.pool.fetch(
            "SELECT * FROM cf_echo_live_chat.tags WHERE tenant_id=$1 ORDER BY use_count DESC,name",
            tid,
        )
    )


@app.get("/tenants/me")
async def get_tenant(request: Request, tid: str = Depends(tenant_id)) -> Any:
    row = await request.app.state.pool.fetchrow(
        "SELECT * FROM cf_echo_live_chat.tenants WHERE id=$1 AND status <> 'deleted'",
        tid,
    )
    if not row:
        raise error("Tenant not found", 404)
    return jsonable(row)


@app.get("/triggers")
async def list_triggers(request: Request, tid: str = Depends(tenant_id)) -> Any:
    return jsonable(
        await request.app.state.pool.fetch(
            "SELECT * FROM cf_echo_live_chat.triggers WHERE tenant_id=$1 ORDER BY created_at DESC",
            tid,
        )
    )


@app.get("/v/messages")
async def visitor_messages(
    request: Request,
    response: Response,
    after: datetime | None = None,
    session: asyncpg.Record = Depends(visitor_session),
) -> Any:
    set_visitor_cors(response, session["origin"])
    return jsonable(
        await request.app.state.pool.fetch(
            """
        SELECT id,sender_type,sender_name,content,content_type,ai_generated,created_at
        FROM cf_echo_live_chat.messages WHERE conversation_id=$1 AND tenant_id=$2
          AND ($3::timestamptz IS NULL OR created_at>$3) ORDER BY created_at LIMIT 50
    """,
            session["conversation_id"],
            session["tenant_id"],
            after,
        )
    )


@app.get("/visitors")
async def list_visitors(
    request: Request,
    search: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    tid: str = Depends(tenant_id),
) -> Any:
    pattern = f"%{sanitize(search, 200)}%" if search else None
    return jsonable(
        await request.app.state.pool.fetch(
            "SELECT * FROM cf_echo_live_chat.visitors WHERE tenant_id=$1 AND ($2::text IS NULL OR name ILIKE $2 OR email ILIKE $2) ORDER BY last_seen DESC LIMIT $3",
            tid,
            pattern,
            limit,
        )
    )


@app.get("/visitors/{id}")
async def get_visitor(id: str, request: Request, tid: str = Depends(tenant_id)) -> Any:
    row = await request.app.state.pool.fetchrow(
        "SELECT * FROM cf_echo_live_chat.visitors WHERE id=$1 AND tenant_id=$2", id, tid
    )
    if not row:
        raise error("Visitor not found", 404)
    result = jsonable(row)
    result["conversations"] = jsonable(
        await request.app.state.pool.fetch(
            "SELECT id,status,started_at,last_message_at FROM cf_echo_live_chat.conversations WHERE visitor_id=$1 AND tenant_id=$2 ORDER BY started_at DESC LIMIT 20",
            id,
            tid,
        )
    )
    return result


@app.get("/widget.js")
async def widget_javascript(
    request: Request, id: str = Query(..., min_length=1, max_length=100)
) -> PlainTextResponse:
    row = await request.app.state.pool.fetchrow(
        "SELECT * FROM cf_echo_live_chat.widgets WHERE id=$1 AND enabled", id
    )
    if not row:
        raise error("Widget not found", 404)
    response = PlainTextResponse(
        generate_widget_script(dict(row), SETTINGS.public_base_url),
        media_type="application/javascript",
    )
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.get("/widgets")
async def list_widgets(request: Request, tid: str = Depends(tenant_id)) -> Any:
    return jsonable(
        await request.app.state.pool.fetch(
            "SELECT * FROM cf_echo_live_chat.widgets WHERE tenant_id=$1 ORDER BY created_at",
            tid,
        )
    )


@app.get("/widgets/{id}")
async def get_widget(id: str, request: Request, tid: str = Depends(tenant_id)) -> Any:
    row = await request.app.state.pool.fetchrow(
        "SELECT * FROM cf_echo_live_chat.widgets WHERE id=$1 AND tenant_id=$2", id, tid
    )
    if not row:
        raise error("Widget not found", 404)
    return jsonable(row)


@app.post("/admin/migrate-stripe")
async def migrate_stripe(request: Request, _auth: None = Depends(require_admin)) -> Any:
    columns = await request.app.state.pool.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='cf_echo_live_chat' AND table_name='tenants' AND column_name=ANY($1::text[])",
        ["plan", "stripe_customer_id", "stripe_subscription_id", "plan_updated_at"],
    )
    return {
        "ok": len(columns) == 4,
        "columns_ready": sorted(row["column_name"] for row in columns),
    }


@app.post("/agents")
async def create_agent(request: Request, tid: str = Depends(tenant_id)) -> Any:
    body = await body_dict(request)
    name = sanitize(body.get("name"), 100)
    email = sanitize(body.get("email"), 200).lower()
    role = sanitize(body.get("role") or "agent", 20)
    if not name or not email or role not in {"agent", "admin", "owner"}:
        raise error("Valid name, email, and role required")
    identifier = uid()
    async with request.app.state.pool.acquire() as connection, connection.transaction():
        maximum = await connection.fetchval(
            "SELECT max_agents FROM cf_echo_live_chat.tenants WHERE id=$1 FOR UPDATE", tid
        )
        count = await connection.fetchval(
            "SELECT count(*) FROM cf_echo_live_chat.agents WHERE tenant_id=$1", tid
        )
        if maximum != -1 and count >= maximum:
            raise error("Agent limit reached", 409)
        row = await connection.fetchrow(
            "INSERT INTO cf_echo_live_chat.agents(id,tenant_id,name,email,role) VALUES($1,$2,$3,$4,$5) ON CONFLICT(tenant_id,email) DO UPDATE SET name=EXCLUDED.name,role=EXCLUDED.role,updated_at=now() RETURNING id",
            identifier,
            tid,
            name,
            email,
            role,
        )
    return {"ok": True, "id": row["id"]}


@app.post("/ai/auto-tag")
async def auto_tag(request: Request, tid: str = Depends(tenant_id)) -> Any:
    body = await body_dict(request)
    answer = await request.app.state.adapters.suggest(
        message="Return only a JSON array of 2 to 4 short support tags.",
        context=sanitize(body.get("messages"), 3000),
    )
    try:
        tags = json.loads(answer or "[]")
        tags = [sanitize(tag, 40) for tag in tags if isinstance(tag, str)][:4]
    except (json.JSONDecodeError, TypeError):
        tags = []
    return {"ok": True, "tags": tags or ["support"], "degraded": not bool(answer)}


@app.post("/ai/suggest-reply")
async def suggest_reply(request: Request, tid: str = Depends(tenant_id)) -> Any:
    body = await body_dict(request)
    message = sanitize(body.get("message"), 1000)
    if not message:
        raise error("Message required")
    suggestion = await request.app.state.adapters.suggest(
        message=message,
        context=sanitize(body.get("context"), 2000),
        system="Suggest a concise, professional customer-support reply.",
    )
    return {
        "ok": True,
        "suggestion": suggestion
        or "Thank you for reaching out. Let me look into this for you.",
        "degraded": not bool(suggestion),
    }


@app.post("/canned")
async def create_canned(request: Request, tid: str = Depends(tenant_id)) -> Any:
    body = await body_dict(request)
    shortcut = sanitize(body.get("shortcut"), 50)
    title = sanitize(body.get("title"), 200)
    content = sanitize(body.get("content"), 5000)
    if not shortcut or not title or not content:
        raise error("Shortcut, title, and content required")
    identifier = uid()
    row = await request.app.state.pool.fetchrow(
        "INSERT INTO cf_echo_live_chat.canned_responses(id,tenant_id,shortcut,title,content,category) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(tenant_id,shortcut) DO UPDATE SET title=EXCLUDED.title,content=EXCLUDED.content,category=EXCLUDED.category,updated_at=now() RETURNING id",
        identifier,
        tid,
        shortcut,
        title,
        content,
        sanitize(body.get("category") or "general", 50),
    )
    return {"ok": True, "id": row["id"]}


@app.post("/plans/upgrade")
async def upgrade_plan(request: Request, tid: str = Depends(tenant_id)) -> Any:
    body = await body_dict(request)
    plan = PLAN_BY_ID.get(str(body.get("plan_id") or ""))
    if not plan or not plan["price"]:
        raise error("Invalid plan")
    if not await request.app.state.pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM cf_echo_live_chat.tenants WHERE id=$1 AND status='active')",
        tid,
    ):
        raise error("Tenant not found", 404)
    if not SETTINGS.stripe_secret_key:
        raise error("Stripe not configured", 503)

    def safe_url(value: Any, fallback: str) -> str:
        candidate = sanitize(value or fallback, 500)
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or not parsed.netloc:
            raise error("HTTPS callback URL required")
        return candidate

    success = safe_url(
        body.get("success_url"), f"{SETTINGS.public_base_url}/billing?status=success"
    )
    cancel = safe_url(
        body.get("cancel_url"), f"{SETTINGS.public_base_url}/billing?status=cancelled"
    )
    try:
        session = await request.app.state.adapters.stripe_checkout(
            tid, plan, success, cancel
        )
    except httpx.HTTPError:
        raise error("Stripe request failed", 502) from None
    return {
        "ok": True,
        "checkout_url": session.get("url"),
        "session_id": session.get("id"),
    }


@app.post("/tags")
async def create_tag(request: Request, tid: str = Depends(tenant_id)) -> Any:
    body = await body_dict(request)
    name = sanitize(body.get("name"), 50)
    color = sanitize(body.get("color") or "#6b7280", 10)
    if not name or not color.startswith("#"):
        raise error("Valid tag name and color required")
    identifier = uid()
    row = await request.app.state.pool.fetchrow(
        "INSERT INTO cf_echo_live_chat.tags(id,tenant_id,name,color) VALUES($1,$2,$3,$4) ON CONFLICT(tenant_id,name) DO UPDATE SET color=EXCLUDED.color RETURNING id",
        identifier,
        tid,
        name,
        color,
    )
    return {"ok": True, "id": row["id"]}


@app.post("/tenants")
async def create_tenant(request: Request, _auth: None = Depends(require_admin)) -> Any:
    body = await body_dict(request)
    name = sanitize(body.get("name"), 200)
    domain = sanitize(body.get("domain"), 200).lower()
    if not name:
        raise error("Tenant name required")
    tid, wid, key = uid(), uid(), uid()
    domains = [domain] if domain else []
    async with request.app.state.pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "INSERT INTO cf_echo_live_chat.tenants(id,name,domain) VALUES($1,$2,$3)",
            tid,
            name,
            domain or None,
        )
        await connection.execute(
            "INSERT INTO cf_echo_live_chat.widgets(id,tenant_id,public_key,allowed_domains) VALUES($1,$2,$3,$4::jsonb)",
            wid,
            tid,
            key,
            json.dumps(domains),
        )
    return {"ok": True, "id": tid, "widget_id": wid, "widget_key": key}


@app.post("/triggers")
async def create_trigger(request: Request, tid: str = Depends(tenant_id)) -> Any:
    body = await body_dict(request)
    name = sanitize(body.get("name"), 200)
    event = sanitize(body.get("event"), 50)
    if not name or not event:
        raise error("Name and event required")
    identifier = uid()
    await request.app.state.pool.execute(
        "INSERT INTO cf_echo_live_chat.triggers(id,tenant_id,name,event,conditions,actions) VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb)",
        identifier,
        tid,
        name,
        event,
        json.dumps(body.get("conditions") or {}),
        json.dumps(body.get("actions") or []),
    )
    return {"ok": True, "id": identifier}


@app.post("/v/init")
async def visitor_init(request: Request, response: Response) -> Any:
    if not await request.app.state.rate_limiter.allow(
        client_bucket(request, "visitor-init"), SETTINGS.visitor_rate_limit, 60
    ):
        raise error("Rate limited", 429, {"Retry-After": "60"})
    body = await body_dict(request)
    wid = sanitize(body.get("widget_id"), 100)
    key = sanitize(body.get("widget_key") or request.headers.get("x-widget-key"), 100)
    widget = await request.app.state.pool.fetchrow(
        "SELECT * FROM cf_echo_live_chat.widgets WHERE id=$1 AND public_key=$2 AND enabled",
        wid,
        key,
    )
    if not widget:
        raise error("Widget not found", 404)
    origin = request.headers.get("origin")
    if not widget_origin_allowed(origin, widget["allowed_domains"]):
        raise error("Domain not allowed", 403)
    set_visitor_cors(response, origin)
    tid = widget["tenant_id"]
    vid, cid = uid(), uid()
    iphash = client_bucket(request, "visitor").split(":", 1)[1]
    async with request.app.state.pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "INSERT INTO cf_echo_live_chat.visitors(id,tenant_id,widget_id,name,email,ip_address_hash,user_agent,page_url,referrer) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            vid,
            tid,
            wid,
            sanitize(body.get("name"), 100) or None,
            sanitize(body.get("email"), 200) or None,
            iphash,
            sanitize(body.get("user_agent"), 300),
            sanitize(body.get("page_url"), 500),
            sanitize(body.get("referrer"), 500),
        )
        agent = await connection.fetchrow(
            "SELECT a.id,a.name FROM cf_echo_live_chat.agents a WHERE a.tenant_id=$1 AND a.status='online' AND a.auto_assign ORDER BY (SELECT count(*) FROM cf_echo_live_chat.conversations c WHERE c.assigned_agent_id=a.id AND c.status IN ('open','active')) LIMIT 1",
            tid,
        )
        await connection.execute(
            "INSERT INTO cf_echo_live_chat.conversations(id,tenant_id,widget_id,visitor_id,assigned_agent_id,status) VALUES($1,$2,$3,$4,$5,$6)",
            cid,
            tid,
            wid,
            vid,
            agent["id"] if agent else None,
            "active" if agent else "open",
        )
        token = await issue_visitor_session(
            connection,
            tenant_id=tid,
            widget_id=wid,
            visitor_id=vid,
            conversation_id=cid,
            origin=origin,
        )
    return {
        "ok": True,
        "conversation_id": cid,
        "visitor_id": vid,
        "session_token": token,
        "widget": {
            "greeting": widget["greeting"],
            "primary_color": widget["primary_color"],
            "position": widget["position"],
            "collect_email": widget["collect_email"],
            "collect_name": widget["collect_name"],
            "show_branding": widget["show_branding"],
        },
        "agent_online": bool(agent),
        "agent_name": agent["name"] if agent else None,
    }


@app.post("/v/message")
async def visitor_message(
    request: Request,
    response: Response,
    session: asyncpg.Record = Depends(visitor_session),
) -> Any:
    if not await request.app.state.rate_limiter.allow(
        f"visitor-message:{session['visitor_id']}", SETTINGS.visitor_rate_limit, 60
    ):
        raise error("Rate limited", 429, {"Retry-After": "60"})
    set_visitor_cors(response, session["origin"])
    body = await body_dict(request)
    content = sanitize(body.get("content"), 5000)
    if not content:
        raise error("Message content required")
    mid = uid()
    conv = await request.app.state.pool.fetchrow(
        "SELECT c.*,w.ai_fallback,w.ai_engine_id,w.ai_system_prompt FROM cf_echo_live_chat.conversations c JOIN cf_echo_live_chat.widgets w ON w.id=c.widget_id AND w.tenant_id=c.tenant_id WHERE c.id=$1 AND c.tenant_id=$2 AND c.visitor_id=$3",
        session["conversation_id"],
        session["tenant_id"],
        session["visitor_id"],
    )
    if not conv:
        raise error("Conversation not found", 404)
    await request.app.state.pool.execute(
        "INSERT INTO cf_echo_live_chat.messages(id,conversation_id,tenant_id,sender_type,sender_id,sender_name,content) VALUES($1,$2,$3,'visitor',$4,$5,$6)",
        mid,
        conv["id"],
        conv["tenant_id"],
        session["visitor_id"],
        sanitize(body.get("visitor_name") or "Visitor", 100),
        content,
    )
    await request.app.state.pool.execute(
        "UPDATE cf_echo_live_chat.conversations SET last_message_at=now() WHERE id=$1 AND tenant_id=$2",
        conv["id"],
        conv["tenant_id"],
    )
    ai_reply = None
    if conv["ai_fallback"] and not conv["assigned_agent_id"]:
        recent = await request.app.state.pool.fetch(
            "SELECT sender_type,content FROM cf_echo_live_chat.messages WHERE conversation_id=$1 AND tenant_id=$2 ORDER BY created_at DESC LIMIT 5",
            conv["id"],
            conv["tenant_id"],
        )
        context = "\n".join(
            f"{r['sender_type']}: {r['content']}" for r in reversed(recent)
        )
        ai_reply = await request.app.state.adapters.suggest(
            message=content,
            context=context,
            system=conv["ai_system_prompt"]
            or "Be concise, friendly, and professional.",
        )
        if ai_reply:
            await request.app.state.pool.execute(
                "INSERT INTO cf_echo_live_chat.messages(id,conversation_id,tenant_id,sender_type,sender_name,content,ai_generated) VALUES($1,$2,$3,'ai','AI Assistant',$4,true)",
                uid(),
                conv["id"],
                conv["tenant_id"],
                ai_reply,
            )
    return {
        "ok": True,
        "message_id": mid,
        "ai_reply": ai_reply,
        "ai_degraded": bool(conv["ai_fallback"] and not ai_reply),
    }


@app.post("/v/rate")
async def visitor_rate(
    request: Request,
    response: Response,
    session: asyncpg.Record = Depends(visitor_session),
) -> Any:
    set_visitor_cors(response, session["origin"])
    body = await body_dict(request)
    try:
        rating = int(body.get("rating"))
    except (TypeError, ValueError):
        raise error("Rating must be 1 through 5") from None
    if not 1 <= rating <= 5:
        raise error("Rating must be 1 through 5")
    result = await request.app.state.pool.execute(
        "UPDATE cf_echo_live_chat.conversations SET rating=$1,feedback=$2 WHERE id=$3 AND tenant_id=$4 AND visitor_id=$5",
        rating,
        sanitize(body.get("feedback"), 1000),
        session["conversation_id"],
        session["tenant_id"],
        session["visitor_id"],
    )
    return {"ok": True, "updated": result.endswith("1")}


@app.post("/v/visitor")
async def update_visitor(
    request: Request,
    response: Response,
    session: asyncpg.Record = Depends(visitor_session),
) -> Any:
    set_visitor_cors(response, session["origin"])
    body = await body_dict(request)
    name = sanitize(body.get("name"), 100) or None
    email = sanitize(body.get("email"), 200) or None
    custom = (
        body.get("custom_data") if isinstance(body.get("custom_data"), dict) else {}
    )
    await request.app.state.pool.execute(
        "UPDATE cf_echo_live_chat.visitors SET name=COALESCE($1,name),email=COALESCE($2,email),custom_data=$3::jsonb,last_seen=now() WHERE id=$4 AND tenant_id=$5",
        name,
        email,
        json.dumps(custom),
        session["visitor_id"],
        session["tenant_id"],
    )
    return {"ok": True}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> Any:
    if not await request.app.state.rate_limiter.allow(
        client_bucket(request, "stripe-webhook"), 60, 60
    ):
        raise error("Rate limited", 429, {"Retry-After": "60"})
    payload = await request.body()
    if not verify_stripe_signature(
        payload,
        request.headers.get("stripe-signature", ""),
        SETTINGS.stripe_webhook_secret,
    ):
        raise error("Invalid signature", 400)
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        raise error("Invalid payload") from None
    processed = await process_stripe_event(request.app.state.pool, event)
    return {"received": True, "processed": processed}


@app.post("/widgets")
async def create_widget(request: Request, tid: str = Depends(tenant_id)) -> Any:
    body = await body_dict(request)
    wid, key = uid(), uid()
    name = sanitize(body.get("name") or "Widget", 100)
    domains = (
        body.get("allowed_domains")
        if isinstance(body.get("allowed_domains"), list)
        else []
    )
    async with request.app.state.pool.acquire() as connection, connection.transaction():
        maximum = await connection.fetchval(
            "SELECT max_widgets FROM cf_echo_live_chat.tenants WHERE id=$1 FOR UPDATE", tid
        )
        count = await connection.fetchval(
            "SELECT count(*) FROM cf_echo_live_chat.widgets WHERE tenant_id=$1", tid
        )
        if maximum != -1 and count >= maximum:
            raise error("Widget limit reached", 409)
        await connection.execute(
            "INSERT INTO cf_echo_live_chat.widgets(id,tenant_id,name,public_key,primary_color,greeting,position,allowed_domains) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)",
            wid,
            tid,
            name,
            key,
            sanitize(body.get("primary_color") or "#14b8a6", 10),
            sanitize(body.get("greeting") or "Hi! How can we help?", 500),
            sanitize(body.get("position") or "bottom-right", 20),
            json.dumps(domains),
        )
    return {"ok": True, "id": wid, "widget_key": key}


@app.put("/agents/{id}")
async def update_agent(id: str, request: Request, tid: str = Depends(tenant_id)) -> Any:
    body = await body_dict(request)
    allowed = {
        k: body[k]
        for k in ("name", "status", "role", "max_concurrent", "auto_assign")
        if k in body
    }
    if not allowed:
        raise error("No fields to update")
    row = await request.app.state.pool.fetchrow(
        "UPDATE cf_echo_live_chat.agents SET name=COALESCE($1,name),status=COALESCE($2,status),role=COALESCE($3,role),max_concurrent=COALESCE($4,max_concurrent),auto_assign=COALESCE($5,auto_assign),updated_at=now() WHERE id=$6 AND tenant_id=$7 RETURNING id",
        sanitize(allowed.get("name"), 100) or None,
        sanitize(allowed.get("status"), 20) or None,
        sanitize(allowed.get("role"), 20) or None,
        int(allowed["max_concurrent"]) if "max_concurrent" in allowed else None,
        bool(allowed["auto_assign"]) if "auto_assign" in allowed else None,
        id,
        tid,
    )
    if not row:
        raise error("Agent not found", 404)
    return {"ok": True}


@app.put("/widgets/{id}")
async def update_widget(
    id: str, request: Request, tid: str = Depends(tenant_id)
) -> Any:
    body = await body_dict(request)
    fields = {
        k: body[k]
        for k in (
            "name",
            "position",
            "primary_color",
            "greeting",
            "offline_message",
            "allowed_domains",
            "business_hours",
            "ai_engine_id",
            "ai_system_prompt",
            "collect_email",
            "collect_name",
            "show_branding",
            "ai_fallback",
            "auto_open_delay",
            "enabled",
        )
        if k in body
    }
    if not fields:
        raise error("No fields to update")
    if "allowed_domains" in fields and not isinstance(fields["allowed_domains"], list):
        raise error("allowed_domains must be an array")
    if "business_hours" in fields and not isinstance(fields["business_hours"], dict):
        raise error("business_hours must be an object")
    current = await request.app.state.pool.fetchrow(
        "SELECT * FROM cf_echo_live_chat.widgets WHERE id=$1 AND tenant_id=$2", id, tid
    )
    if not current:
        raise error("Widget not found", 404)
    merged = dict(current)
    merged["allowed_domains"] = decoded_json(merged["allowed_domains"], list, [])
    merged["business_hours"] = decoded_json(merged["business_hours"], dict, {})
    merged.update(fields)
    delay = max(0, min(int(merged["auto_open_delay"]), 120))
    await request.app.state.pool.execute(
        """UPDATE cf_echo_live_chat.widgets SET name=$1,position=$2,primary_color=$3,greeting=$4,offline_message=$5,allowed_domains=$6::jsonb,business_hours=$7::jsonb,ai_engine_id=$8,ai_system_prompt=$9,collect_email=$10,collect_name=$11,show_branding=$12,ai_fallback=$13,auto_open_delay=$14,enabled=$15,updated_at=now() WHERE id=$16 AND tenant_id=$17""",
        sanitize(merged["name"], 100),
        sanitize(merged["position"], 20),
        sanitize(merged["primary_color"], 10),
        sanitize(merged["greeting"], 500),
        sanitize(merged["offline_message"], 1000),
        json.dumps(merged["allowed_domains"]),
        json.dumps(merged["business_hours"]),
        sanitize(merged["ai_engine_id"], 100),
        sanitize(merged["ai_system_prompt"], 2000) or None,
        bool(merged["collect_email"]),
        bool(merged["collect_name"]),
        bool(merged["show_branding"]),
        bool(merged["ai_fallback"]),
        delay,
        bool(merged["enabled"]),
        id,
        tid,
    )
    return {"ok": True}


async def health(request: Request) -> dict[str, Any]:
    return {
        "ok": True,
        "service": SERVICE,
        "version": VERSION,
        "database": bool(await request.app.state.pool.fetchval("SELECT 1")),
        "stripe_configured": bool(SETTINGS.stripe_secret_key),
    }


async def diagnostics(request: Request) -> dict[str, Any]:
    db_started = time.perf_counter()
    await request.app.state.pool.fetchval("SELECT 1")
    return {
        "ok": True,
        "service": SERVICE,
        "version": VERSION,
        "database": {
            "ok": True,
            "latency_ms": round((time.perf_counter() - db_started) * 1000, 2),
        },
        "bindings": {
            "DB": True,
            "CACHE": SETTINGS.cache == "postgresql",
            "ANALYTICS": SETTINGS.analytics == "postgresql",
            "ENGINE_RUNTIME": bool(SETTINGS.engine_runtime),
            "SHARED_BRAIN": bool(SETTINGS.shared_brain),
            "STRIPE_SECRET_KEY": bool(SETTINGS.stripe_secret_key),
            "ECHO_API_KEY": bool(SETTINGS.echo_api_key),
        },
    }


async def cleanup_smoke_tenant(
    request: Request, _auth: None = Depends(require_admin)
) -> dict[str, Any]:
    """Remove only a tenant created by the release smoke suite."""
    if request.headers.get("x-echo-smoke-test") != "1":
        raise error("Not found", 404)
    body = await body_dict(request)
    tid = sanitize(body.get("tenant_id"), 100)
    result = await request.app.state.pool.execute(
        "DELETE FROM cf_echo_live_chat.tenants WHERE id=$1 AND left(name,9)='__smoke__'",
        tid,
    )
    return {"ok": True, "deleted": result.endswith("1")}


app.add_api_route("/health", health, methods=["GET"], include_in_schema=False)
app.add_api_route("/diagnostics", diagnostics, methods=["GET"], include_in_schema=False)
app.add_api_route("/", health, methods=["GET"], include_in_schema=False)
app.add_api_route("/status", health, methods=["GET"], include_in_schema=False)
app.add_api_route(
    "/_smoke/cleanup",
    cleanup_smoke_tenant,
    methods=["POST"],
    include_in_schema=False,
)
