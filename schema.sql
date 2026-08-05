CREATE SCHEMA IF NOT EXISTS cf_echo_live_chat;

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT,
    plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'starter', 'business', 'enterprise')),
    max_agents INTEGER NOT NULL DEFAULT 1,
    max_widgets INTEGER NOT NULL DEFAULT 1,
    max_conversations_month INTEGER NOT NULL DEFAULT 100,
    ai_enabled BOOLEAN NOT NULL DEFAULT true,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    plan_updated_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleted')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.agents (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    avatar_url TEXT,
    role TEXT NOT NULL DEFAULT 'agent' CHECK (role IN ('agent', 'admin', 'owner')),
    status TEXT NOT NULL DEFAULT 'offline' CHECK (status IN ('online', 'away', 'offline')),
    max_concurrent INTEGER NOT NULL DEFAULT 5 CHECK (max_concurrent BETWEEN 1 AND 100),
    auto_assign BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.widgets (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT 'Default Widget',
    public_key TEXT NOT NULL UNIQUE,
    position TEXT NOT NULL DEFAULT 'bottom-right' CHECK (position IN ('bottom-right', 'bottom-left')),
    primary_color TEXT NOT NULL DEFAULT '#14b8a6',
    greeting TEXT NOT NULL DEFAULT 'Hi! How can we help you today?',
    offline_message TEXT NOT NULL DEFAULT 'We are currently offline. Leave a message and we will get back to you.',
    collect_email BOOLEAN NOT NULL DEFAULT true,
    collect_name BOOLEAN NOT NULL DEFAULT true,
    show_branding BOOLEAN NOT NULL DEFAULT true,
    auto_open_delay INTEGER NOT NULL DEFAULT 0 CHECK (auto_open_delay BETWEEN 0 AND 120),
    allowed_domains JSONB NOT NULL DEFAULT '[]'::jsonb,
    business_hours JSONB NOT NULL DEFAULT '{}'::jsonb,
    ai_fallback BOOLEAN NOT NULL DEFAULT true,
    ai_engine_id TEXT NOT NULL DEFAULT 'GEN-01',
    ai_system_prompt TEXT,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.visitors (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    widget_id TEXT NOT NULL REFERENCES cf_echo_live_chat.widgets(id) ON DELETE CASCADE,
    name TEXT,
    email TEXT,
    ip_address_hash TEXT,
    user_agent TEXT,
    country TEXT,
    city TEXT,
    page_url TEXT,
    referrer TEXT,
    sessions INTEGER NOT NULL DEFAULT 1,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    custom_data JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.visitor_sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    widget_id TEXT NOT NULL REFERENCES cf_echo_live_chat.widgets(id) ON DELETE CASCADE,
    visitor_id TEXT NOT NULL REFERENCES cf_echo_live_chat.visitors(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    origin TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    widget_id TEXT NOT NULL REFERENCES cf_echo_live_chat.widgets(id) ON DELETE CASCADE,
    visitor_id TEXT NOT NULL REFERENCES cf_echo_live_chat.visitors(id) ON DELETE CASCADE,
    assigned_agent_id TEXT REFERENCES cf_echo_live_chat.agents(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'active', 'pending', 'closed', 'archived')),
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    subject TEXT,
    channel TEXT NOT NULL DEFAULT 'chat',
    rating SMALLINT CHECK (rating BETWEEN 1 AND 5),
    feedback TEXT,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_message_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES cf_echo_live_chat.conversations(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    sender_type TEXT NOT NULL CHECK (sender_type IN ('visitor', 'agent', 'system', 'ai')),
    sender_id TEXT,
    sender_name TEXT,
    content TEXT NOT NULL CHECK (char_length(content) BETWEEN 1 AND 5000),
    content_type TEXT NOT NULL DEFAULT 'text',
    attachment_url TEXT,
    attachment_name TEXT,
    ai_generated BOOLEAN NOT NULL DEFAULT false,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.canned_responses (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    shortcut TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    use_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, shortcut)
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.triggers (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    event TEXT NOT NULL,
    conditions JSONB NOT NULL DEFAULT '{}'::jsonb,
    actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT true,
    fire_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.tags (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#6b7280',
    use_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.analytics_daily (
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    conversations_started INTEGER NOT NULL DEFAULT 0,
    conversations_resolved INTEGER NOT NULL DEFAULT 0,
    messages_sent INTEGER NOT NULL DEFAULT 0,
    messages_received INTEGER NOT NULL DEFAULT 0,
    ai_responses INTEGER NOT NULL DEFAULT 0,
    avg_response_time_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_satisfaction DOUBLE PRECISION NOT NULL DEFAULT 0,
    unique_visitors INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, date)
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.activity_log (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES cf_echo_live_chat.tenants(id) ON DELETE CASCADE,
    actor TEXT,
    action TEXT NOT NULL,
    target TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.rate_limits (
    bucket_key TEXT PRIMARY KEY,
    window_started_at TIMESTAMPTZ NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.webhook_events (
    provider TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, event_id)
);

CREATE TABLE IF NOT EXISTS cf_echo_live_chat.migration_receipts (
    id BIGSERIAL PRIMARY KEY,
    candidate_release TEXT NOT NULL,
    active_release TEXT NOT NULL DEFAULT '',
    event_name TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (candidate_release, event_name)
);

ALTER TABLE cf_echo_live_chat.migration_receipts
    ADD COLUMN IF NOT EXISTS active_release TEXT NOT NULL DEFAULT '';
ALTER TABLE cf_echo_live_chat.migration_receipts
    ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ NOT NULL DEFAULT now();

DO $$
DECLARE
    existing_definition TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO existing_definition
    FROM pg_constraint
    WHERE conrelid='cf_echo_live_chat.visitor_sessions'::regclass
      AND conname='visitor_sessions_conversation_fk';
    IF existing_definition IS NULL OR existing_definition NOT LIKE '%ON DELETE CASCADE%' THEN
        ALTER TABLE cf_echo_live_chat.visitor_sessions
            DROP CONSTRAINT IF EXISTS visitor_sessions_conversation_fk;
        ALTER TABLE cf_echo_live_chat.visitor_sessions
            ADD CONSTRAINT visitor_sessions_conversation_fk FOREIGN KEY (conversation_id)
            REFERENCES cf_echo_live_chat.conversations(id) ON DELETE CASCADE
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_live_chat_agents_tenant ON cf_echo_live_chat.agents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_live_chat_widgets_tenant ON cf_echo_live_chat.widgets(tenant_id);
CREATE INDEX IF NOT EXISTS idx_live_chat_visitors_tenant_seen ON cf_echo_live_chat.visitors(tenant_id, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_live_chat_sessions_hash_expiry ON cf_echo_live_chat.visitor_sessions(token_hash, expires_at);
CREATE INDEX IF NOT EXISTS idx_live_chat_conversations_tenant_status ON cf_echo_live_chat.conversations(tenant_id, status, last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_chat_conversations_visitor ON cf_echo_live_chat.conversations(visitor_id, status);
CREATE INDEX IF NOT EXISTS idx_live_chat_messages_conversation_created ON cf_echo_live_chat.messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_live_chat_messages_tenant_created ON cf_echo_live_chat.messages(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_chat_activity_tenant_created ON cf_echo_live_chat.activity_log(tenant_id, created_at DESC);

REVOKE ALL ON SCHEMA cf_echo_live_chat FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA cf_echo_live_chat FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA cf_echo_live_chat FROM PUBLIC;
GRANT USAGE ON SCHEMA cf_echo_live_chat TO "echo-live-chat";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA cf_echo_live_chat TO "echo-live-chat";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA cf_echo_live_chat TO "echo-live-chat";
ALTER DEFAULT PRIVILEGES IN SCHEMA cf_echo_live_chat REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA cf_echo_live_chat GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "echo-live-chat";
ALTER DEFAULT PRIVILEGES IN SCHEMA cf_echo_live_chat GRANT USAGE, SELECT ON SEQUENCES TO "echo-live-chat";
