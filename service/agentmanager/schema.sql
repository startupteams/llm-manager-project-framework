-- Agent Manager V2 schema (schema 'agentmanager' in llmmanager DB, CT115)
-- ADR-0003: co-locate schema with existing DB (no CREATEDB grant needed); search_path isolates tables.
CREATE TABLE IF NOT EXISTS agentmanager.users (
    user_id UUID PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT,
    is_root BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS agentmanager.agents (
    agent_id UUID PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    agent_type TEXT NOT NULL CHECK (agent_type IN ('STEA','STBM')),
    owner_username TEXT NOT NULL,
    role_title TEXT,
    description TEXT,
    harness TEXT NOT NULL CHECK (harness IN ('HERMES','PI','RUFLO','OPENCLAW')),
    state TEXT NOT NULL DEFAULT 'PROVISIONING',
    vmid INT,
    node TEXT,
    agent_ip TEXT,
    agent_token TEXT,           -- per-agent st-agentd bearer token (root-readable source of truth on agent VM)
    model_profile TEXT,
    routing_policy TEXT NOT NULL DEFAULT 'LOCAL_FIRST',
    guardrail_profile TEXT NOT NULL DEFAULT 'STEA_YOLO_DEV_V1',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS agentmanager.agent_access (
    agent_id UUID NOT NULL REFERENCES agentmanager.agents(agent_id) ON DELETE CASCADE,
    principal TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('user','root','group')),
    granted_by TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, principal)
);
CREATE TABLE IF NOT EXISTS agentmanager.plan_artifacts (
    plan_id UUID PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    description TEXT,
    jira_issue_key TEXT,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','ACTIVE','SUPERSEDED','ARCHIVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS agentmanager.plan_versions (
    version_id UUID PRIMARY KEY,
    plan_id UUID NOT NULL REFERENCES agentmanager.plan_artifacts(plan_id) ON DELETE CASCADE,
    version_number INT NOT NULL,
    original_filename TEXT NOT NULL,
    markdown_body TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    change_note TEXT,
    usage_instructions TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (plan_id, version_number)
);
CREATE TABLE IF NOT EXISTS agentmanager.projects (
    project_id UUID PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    jira_project_key TEXT,
    repo_url TEXT,
    default_model_profile TEXT,
    priority TEXT NOT NULL DEFAULT 'P1',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS agentmanager.work_items (
    item_id UUID PRIMARY KEY,
    project_id UUID REFERENCES agentmanager.projects(project_id),
    jira_issue_key TEXT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'READY' CHECK (status IN ('BACKLOG','READY','WORKING','WAITING_HUMAN','REVIEW_READY','DONE','BLOCKED')),
    priority TEXT NOT NULL DEFAULT 'P2',
    assigned_agent_id UUID REFERENCES agentmanager.agents(agent_id),
    active_plan_version_id UUID REFERENCES agentmanager.plan_versions(version_id),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS agentmanager.runs (
    run_id UUID PRIMARY KEY,
    item_id UUID REFERENCES agentmanager.work_items(item_id),
    agent_id UUID REFERENCES agentmanager.agents(agent_id),
    plan_version_id UUID REFERENCES agentmanager.plan_versions(version_id),
    task_packet JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING','REVIEW_READY','DONE','ERROR','CANCELLED')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    result_summary TEXT
);
CREATE TABLE IF NOT EXISTS agentmanager.sessions (
    session_id UUID PRIMARY KEY,
    agent_id UUID NOT NULL REFERENCES agentmanager.agents(agent_id) ON DELETE CASCADE,
    remote_session_id TEXT,
    title TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS agentmanager.messages (
    message_id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES agentmanager.sessions(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS agentmanager.audit_events (
    event_id BIGSERIAL PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    result TEXT NOT NULL DEFAULT 'ok',
    detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS agentmanager.provisioning_jobs (
    job_id UUID PRIMARY KEY,
    agent_id UUID NOT NULL REFERENCES agentmanager.agents(agent_id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'REQUESTED',
    step TEXT,
    error TEXT,
    vmid INT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS agentmanager.provisioning_steps (
    job_id UUID NOT NULL REFERENCES agentmanager.provisioning_jobs(job_id) ON DELETE CASCADE,
    seq INT NOT NULL,
    step TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    detail TEXT,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_agents_state ON agentmanager.agents(state);
CREATE INDEX IF NOT EXISTS idx_messages_session ON agentmanager.messages(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON agentmanager.audit_events(created_at DESC);
