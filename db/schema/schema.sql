--
-- PostgreSQL database dump
--

\restrict mvkpd4WowtiAk3fOHiH6Mjd9I9Go6NSafTEzxE5lP8Hk4fmEusCYyvScmlrrTMe

-- Dumped from database version 15.19 (Debian 15.19-0+deb12u1)
-- Dumped by pg_dump version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'SQL_ASCII';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: agentmanager; Type: SCHEMA; Schema: -; Owner: llmmanager
--

CREATE SCHEMA agentmanager;


ALTER SCHEMA agentmanager OWNER TO llmmanager;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_access; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.agent_access (
    agent_id uuid NOT NULL,
    principal text NOT NULL,
    kind text NOT NULL,
    granted_by text NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_access_kind_check CHECK ((kind = ANY (ARRAY['user'::text, 'root'::text, 'group'::text])))
);


ALTER TABLE agentmanager.agent_access OWNER TO llmmanager;

--
-- Name: agents; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.agents (
    agent_id uuid NOT NULL,
    name text NOT NULL,
    agent_type text NOT NULL,
    owner_username text NOT NULL,
    role_title text,
    description text,
    harness text NOT NULL,
    state text DEFAULT 'PROVISIONING'::text NOT NULL,
    vmid integer,
    node text,
    agent_ip text,
    agent_token text,
    model_profile text,
    routing_policy text DEFAULT 'LOCAL_FIRST'::text NOT NULL,
    guardrail_profile text DEFAULT 'STEA_YOLO_DEV_V1'::text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agents_agent_type_check CHECK ((agent_type = ANY (ARRAY['STEA'::text, 'STBM'::text]))),
    CONSTRAINT agents_harness_check CHECK ((harness = ANY (ARRAY['HERMES'::text, 'PI'::text, 'RUFLO'::text, 'OPENCLAW'::text])))
);


ALTER TABLE agentmanager.agents OWNER TO llmmanager;

--
-- Name: audit_events; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.audit_events (
    event_id bigint NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    target text,
    result text DEFAULT 'ok'::text NOT NULL,
    detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE agentmanager.audit_events OWNER TO llmmanager;

--
-- Name: audit_events_event_id_seq; Type: SEQUENCE; Schema: agentmanager; Owner: llmmanager
--

CREATE SEQUENCE agentmanager.audit_events_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE agentmanager.audit_events_event_id_seq OWNER TO llmmanager;

--
-- Name: audit_events_event_id_seq; Type: SEQUENCE OWNED BY; Schema: agentmanager; Owner: llmmanager
--

ALTER SEQUENCE agentmanager.audit_events_event_id_seq OWNED BY agentmanager.audit_events.event_id;


--
-- Name: messages; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.messages (
    message_id bigint NOT NULL,
    session_id uuid NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    model text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT messages_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text, 'system'::text])))
);


ALTER TABLE agentmanager.messages OWNER TO llmmanager;

--
-- Name: messages_message_id_seq; Type: SEQUENCE; Schema: agentmanager; Owner: llmmanager
--

CREATE SEQUENCE agentmanager.messages_message_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE agentmanager.messages_message_id_seq OWNER TO llmmanager;

--
-- Name: messages_message_id_seq; Type: SEQUENCE OWNED BY; Schema: agentmanager; Owner: llmmanager
--

ALTER SEQUENCE agentmanager.messages_message_id_seq OWNED BY agentmanager.messages.message_id;


--
-- Name: plan_artifacts; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.plan_artifacts (
    plan_id uuid NOT NULL,
    title text NOT NULL,
    slug text NOT NULL,
    description text,
    jira_issue_key text,
    created_by text NOT NULL,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT plan_artifacts_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'ACTIVE'::text, 'SUPERSEDED'::text, 'ARCHIVED'::text])))
);


ALTER TABLE agentmanager.plan_artifacts OWNER TO llmmanager;

--
-- Name: plan_versions; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.plan_versions (
    version_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    version_number integer NOT NULL,
    original_filename text NOT NULL,
    markdown_body text NOT NULL,
    sha256 text NOT NULL,
    change_note text,
    usage_instructions text,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE agentmanager.plan_versions OWNER TO llmmanager;

--
-- Name: projects; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.projects (
    project_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    jira_project_key text,
    repo_url text,
    default_model_profile text,
    priority text DEFAULT 'P1'::text NOT NULL,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE agentmanager.projects OWNER TO llmmanager;

--
-- Name: provisioning_jobs; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.provisioning_jobs (
    job_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    state text DEFAULT 'REQUESTED'::text NOT NULL,
    step text,
    error text,
    vmid integer,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone
);


ALTER TABLE agentmanager.provisioning_jobs OWNER TO llmmanager;

--
-- Name: provisioning_steps; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.provisioning_steps (
    job_id uuid NOT NULL,
    seq integer NOT NULL,
    step text NOT NULL,
    state text DEFAULT 'PENDING'::text NOT NULL,
    detail text,
    ts timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE agentmanager.provisioning_steps OWNER TO llmmanager;

--
-- Name: runs; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.runs (
    run_id uuid NOT NULL,
    item_id uuid,
    agent_id uuid,
    plan_version_id uuid,
    task_packet jsonb NOT NULL,
    status text DEFAULT 'RUNNING'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    result_summary text,
    CONSTRAINT runs_status_check CHECK ((status = ANY (ARRAY['RUNNING'::text, 'REVIEW_READY'::text, 'DONE'::text, 'ERROR'::text, 'CANCELLED'::text])))
);


ALTER TABLE agentmanager.runs OWNER TO llmmanager;

--
-- Name: sessions; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.sessions (
    session_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    remote_session_id text,
    title text,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE agentmanager.sessions OWNER TO llmmanager;

--
-- Name: users; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.users (
    user_id uuid NOT NULL,
    username text NOT NULL,
    display_name text,
    is_root boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE agentmanager.users OWNER TO llmmanager;

--
-- Name: work_items; Type: TABLE; Schema: agentmanager; Owner: llmmanager
--

CREATE TABLE agentmanager.work_items (
    item_id uuid NOT NULL,
    project_id uuid,
    jira_issue_key text,
    title text NOT NULL,
    description text,
    status text DEFAULT 'READY'::text NOT NULL,
    priority text DEFAULT 'P2'::text NOT NULL,
    assigned_agent_id uuid,
    active_plan_version_id uuid,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT work_items_status_check CHECK ((status = ANY (ARRAY['BACKLOG'::text, 'READY'::text, 'WORKING'::text, 'WAITING_HUMAN'::text, 'REVIEW_READY'::text, 'DONE'::text, 'BLOCKED'::text])))
);


ALTER TABLE agentmanager.work_items OWNER TO llmmanager;

--
-- Name: agent_keys; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.agent_keys (
    key_id integer NOT NULL,
    external_id text,
    project_id text,
    agent_instance_id text,
    agent_name text,
    harness text,
    alias text,
    raw_key text,
    created_at timestamp with time zone DEFAULT now(),
    created_by text,
    last_used_at timestamp with time zone,
    enabled boolean DEFAULT true,
    revoked_at timestamp with time zone,
    monthly_budget numeric
);


ALTER TABLE public.agent_keys OWNER TO llmmanager;

--
-- Name: agent_keys_key_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.agent_keys_key_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.agent_keys_key_id_seq OWNER TO llmmanager;

--
-- Name: agent_keys_key_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.agent_keys_key_id_seq OWNED BY public.agent_keys.key_id;


--
-- Name: benchmark_runs; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.benchmark_runs (
    run_id integer NOT NULL,
    ts timestamp with time zone DEFAULT now(),
    preset text,
    concurrency integer,
    requests integer,
    ok integer,
    failed integer,
    duration_s numeric,
    output_tok_s numeric,
    ttft_avg_s numeric,
    ttft_max_s numeric,
    latency_avg_s numeric,
    gpu_watts_avg numeric,
    target text,
    notes text,
    deployment_revision_id bigint,
    prompt_tokens integer,
    output_tokens integer,
    aggregate_output_tok_s numeric,
    ram_used_gb numeric,
    vram_used_gb numeric,
    tested_context integer,
    raw_result_path text,
    hermes_tool_pass boolean,
    status text DEFAULT 'COMPLETED'::text
);


ALTER TABLE public.benchmark_runs OWNER TO llmmanager;

--
-- Name: benchmark_runs_run_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.benchmark_runs_run_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.benchmark_runs_run_id_seq OWNER TO llmmanager;

--
-- Name: benchmark_runs_run_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.benchmark_runs_run_id_seq OWNED BY public.benchmark_runs.run_id;


--
-- Name: deployment_revisions; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.deployment_revisions (
    revision_id bigint NOT NULL,
    revision_label text NOT NULL,
    model text NOT NULL,
    artifact text,
    artifact_revision text,
    quantization text,
    runtime text,
    runtime_version text,
    host text,
    vm text,
    guest_ip text,
    cpu_cores integer,
    ram_gb numeric,
    ram_speed text,
    gpu_model text,
    gpu_count integer,
    context integer,
    parallelism text,
    max_num_seqs integer,
    kv_cache text,
    launch_command text,
    systemd_unit text,
    aliases text,
    status text DEFAULT 'CANDIDATE'::text,
    visible_by_default boolean DEFAULT true,
    preset_id integer,
    notes text,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.deployment_revisions OWNER TO llmmanager;

--
-- Name: deployment_revisions_revision_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.deployment_revisions_revision_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.deployment_revisions_revision_id_seq OWNER TO llmmanager;

--
-- Name: deployment_revisions_revision_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.deployment_revisions_revision_id_seq OWNED BY public.deployment_revisions.revision_id;


--
-- Name: deployments; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.deployments (
    deployment_id integer NOT NULL,
    host_id integer,
    served_model text,
    max_model_len integer,
    enabled boolean DEFAULT true,
    config_version text,
    desired_service_state text DEFAULT 'SERVING'::text NOT NULL
);


ALTER TABLE public.deployments OWNER TO llmmanager;

--
-- Name: deployments_deployment_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.deployments_deployment_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.deployments_deployment_id_seq OWNER TO llmmanager;

--
-- Name: deployments_deployment_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.deployments_deployment_id_seq OWNED BY public.deployments.deployment_id;


--
-- Name: electricity_rates; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.electricity_rates (
    rate_id integer NOT NULL,
    component text NOT NULL,
    value_per_kwh numeric NOT NULL,
    season text NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    source_note text,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.electricity_rates OWNER TO llmmanager;

--
-- Name: electricity_rates_rate_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.electricity_rates_rate_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.electricity_rates_rate_id_seq OWNER TO llmmanager;

--
-- Name: electricity_rates_rate_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.electricity_rates_rate_id_seq OWNED BY public.electricity_rates.rate_id;


--
-- Name: emporia_channel_map; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.emporia_channel_map (
    device_gid bigint NOT NULL,
    channel_num text NOT NULL,
    mapped_role text,
    mapped_target text,
    confidence text DEFAULT 'high'::text,
    notes text
);


ALTER TABLE public.emporia_channel_map OWNER TO llmmanager;

--
-- Name: emporia_devices; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.emporia_devices (
    device_gid bigint NOT NULL,
    channel_num text NOT NULL,
    channel_name text,
    model text,
    last_seen timestamp with time zone DEFAULT now()
);


ALTER TABLE public.emporia_devices OWNER TO llmmanager;

--
-- Name: facility_power_rollup_1d; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.facility_power_rollup_1d (
    day date NOT NULL,
    device_gid bigint NOT NULL,
    channel_num text NOT NULL,
    kwh numeric,
    avg_watts numeric
);


ALTER TABLE public.facility_power_rollup_1d OWNER TO llmmanager;

--
-- Name: facility_power_samples; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.facility_power_samples (
    ts timestamp with time zone NOT NULL,
    device_gid bigint NOT NULL,
    channel_num text NOT NULL,
    watts numeric,
    kwh_interval numeric
);


ALTER TABLE public.facility_power_samples OWNER TO llmmanager;

--
-- Name: gpu_samples; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.gpu_samples (
    sample_id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    host_id integer,
    gpu_index integer NOT NULL,
    util_pct numeric,
    mem_used_mib numeric,
    power_watts numeric
);


ALTER TABLE public.gpu_samples OWNER TO llmmanager;

--
-- Name: gpu_samples_sample_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.gpu_samples_sample_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.gpu_samples_sample_id_seq OWNER TO llmmanager;

--
-- Name: gpu_samples_sample_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.gpu_samples_sample_id_seq OWNED BY public.gpu_samples.sample_id;


--
-- Name: host_power_rollup_1m; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.host_power_rollup_1m (
    rollup_id bigint NOT NULL,
    ts timestamp with time zone NOT NULL,
    host_id integer,
    gpu_watts_avg numeric,
    gpu_watts_peak numeric
);


ALTER TABLE public.host_power_rollup_1m OWNER TO llmmanager;

--
-- Name: host_power_rollup_1m_rollup_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.host_power_rollup_1m_rollup_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.host_power_rollup_1m_rollup_id_seq OWNER TO llmmanager;

--
-- Name: host_power_rollup_1m_rollup_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.host_power_rollup_1m_rollup_id_seq OWNED BY public.host_power_rollup_1m.rollup_id;


--
-- Name: hosting_command_presets; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.hosting_command_presets (
    id integer NOT NULL,
    scope_type text DEFAULT 'single_host'::text NOT NULL,
    scope_key text NOT NULL,
    engine text DEFAULT 'vllm'::text NOT NULL,
    friendly_name text NOT NULL,
    content text NOT NULL,
    content_hash text NOT NULL,
    sort_order integer DEFAULT 1000 NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    last_used_at timestamp with time zone,
    use_count integer DEFAULT 0 NOT NULL,
    deleted_at timestamp with time zone,
    created_by text,
    updated_by text
);


ALTER TABLE public.hosting_command_presets OWNER TO llmmanager;

--
-- Name: hosting_command_presets_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.hosting_command_presets_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.hosting_command_presets_id_seq OWNER TO llmmanager;

--
-- Name: hosting_command_presets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.hosting_command_presets_id_seq OWNED BY public.hosting_command_presets.id;


--
-- Name: hosts; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.hosts (
    host_id integer NOT NULL,
    name text NOT NULL,
    guest_ip text NOT NULL,
    gpu_count integer,
    gpu_model text,
    created_at timestamp with time zone DEFAULT now(),
    desired_power_state text DEFAULT 'RUNNING'::text NOT NULL,
    management_mode text DEFAULT 'MANAGED'::text NOT NULL,
    node text,
    vmid integer,
    desired_service_state text DEFAULT 'SERVING'::text NOT NULL
);


ALTER TABLE public.hosts OWNER TO llmmanager;

--
-- Name: hosts_host_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.hosts_host_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.hosts_host_id_seq OWNER TO llmmanager;

--
-- Name: hosts_host_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.hosts_host_id_seq OWNED BY public.hosts.host_id;


--
-- Name: manager_settings; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.manager_settings (
    key text NOT NULL,
    value text NOT NULL,
    updated_at timestamp with time zone DEFAULT now(),
    updated_by text
);


ALTER TABLE public.manager_settings OWNER TO llmmanager;

--
-- Name: model_registry; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.model_registry (
    id integer NOT NULL,
    logical_model_name text NOT NULL,
    host_id integer,
    engine text,
    backend_url text,
    context_limit integer,
    capabilities jsonb DEFAULT '{}'::jsonb,
    health text DEFAULT 'unknown'::text,
    routable boolean DEFAULT false,
    is_alias boolean DEFAULT false,
    alias_target text,
    last_verified timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.model_registry OWNER TO llmmanager;

--
-- Name: model_registry_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.model_registry_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.model_registry_id_seq OWNER TO llmmanager;

--
-- Name: model_registry_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.model_registry_id_seq OWNED BY public.model_registry.id;


--
-- Name: multi_node_deployments; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.multi_node_deployments (
    id integer NOT NULL,
    name text NOT NULL,
    model text NOT NULL,
    participating_hosts jsonb NOT NULL,
    topology jsonb,
    state text DEFAULT 'planned'::text,
    notes text,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.multi_node_deployments OWNER TO llmmanager;

--
-- Name: multi_node_deployments_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.multi_node_deployments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.multi_node_deployments_id_seq OWNER TO llmmanager;

--
-- Name: multi_node_deployments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.multi_node_deployments_id_seq OWNED BY public.multi_node_deployments.id;


--
-- Name: qualification_envelopes; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.qualification_envelopes (
    envelope_id integer NOT NULL,
    deployment_ip text,
    model text,
    recommended_concurrency integer,
    hard_concurrency_limit integer,
    qualified_context integer,
    benchmark_run_id integer,
    kv_cache_dtype text,
    notes text,
    ts timestamp with time zone DEFAULT now()
);


ALTER TABLE public.qualification_envelopes OWNER TO llmmanager;

--
-- Name: qualification_envelopes_envelope_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.qualification_envelopes_envelope_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.qualification_envelopes_envelope_id_seq OWNER TO llmmanager;

--
-- Name: qualification_envelopes_envelope_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.qualification_envelopes_envelope_id_seq OWNED BY public.qualification_envelopes.envelope_id;


--
-- Name: rdma_ring_nodes; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.rdma_ring_nodes (
    host text NOT NULL,
    ip text,
    vmid integer,
    guest_ip text,
    nic_model text,
    link_state text,
    qualified boolean DEFAULT false,
    evidence jsonb,
    notes text,
    rdma_enabled boolean DEFAULT true NOT NULL
);


ALTER TABLE public.rdma_ring_nodes OWNER TO llmmanager;

--
-- Name: recovery_events; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.recovery_events (
    id integer NOT NULL,
    host_id integer,
    ip text,
    event_type text,
    detail text,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.recovery_events OWNER TO llmmanager;

--
-- Name: recovery_events_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.recovery_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.recovery_events_id_seq OWNER TO llmmanager;

--
-- Name: recovery_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.recovery_events_id_seq OWNED BY public.recovery_events.id;


--
-- Name: request_routing_log; Type: TABLE; Schema: public; Owner: llmmanager
--

CREATE TABLE public.request_routing_log (
    id integer NOT NULL,
    request_id text,
    ts timestamp with time zone DEFAULT now(),
    model text,
    api_base text,
    prompt_tokens integer,
    completion_tokens integer,
    latency_ms integer,
    ttft_ms integer,
    key_alias text,
    provider_spend numeric
);


ALTER TABLE public.request_routing_log OWNER TO llmmanager;

--
-- Name: request_routing_log_id_seq; Type: SEQUENCE; Schema: public; Owner: llmmanager
--

CREATE SEQUENCE public.request_routing_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.request_routing_log_id_seq OWNER TO llmmanager;

--
-- Name: request_routing_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: llmmanager
--

ALTER SEQUENCE public.request_routing_log_id_seq OWNED BY public.request_routing_log.id;


--
-- Name: audit_events event_id; Type: DEFAULT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.audit_events ALTER COLUMN event_id SET DEFAULT nextval('agentmanager.audit_events_event_id_seq'::regclass);


--
-- Name: messages message_id; Type: DEFAULT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.messages ALTER COLUMN message_id SET DEFAULT nextval('agentmanager.messages_message_id_seq'::regclass);


--
-- Name: agent_keys key_id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.agent_keys ALTER COLUMN key_id SET DEFAULT nextval('public.agent_keys_key_id_seq'::regclass);


--
-- Name: benchmark_runs run_id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.benchmark_runs ALTER COLUMN run_id SET DEFAULT nextval('public.benchmark_runs_run_id_seq'::regclass);


--
-- Name: deployment_revisions revision_id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.deployment_revisions ALTER COLUMN revision_id SET DEFAULT nextval('public.deployment_revisions_revision_id_seq'::regclass);


--
-- Name: deployments deployment_id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.deployments ALTER COLUMN deployment_id SET DEFAULT nextval('public.deployments_deployment_id_seq'::regclass);


--
-- Name: electricity_rates rate_id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.electricity_rates ALTER COLUMN rate_id SET DEFAULT nextval('public.electricity_rates_rate_id_seq'::regclass);


--
-- Name: gpu_samples sample_id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.gpu_samples ALTER COLUMN sample_id SET DEFAULT nextval('public.gpu_samples_sample_id_seq'::regclass);


--
-- Name: host_power_rollup_1m rollup_id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.host_power_rollup_1m ALTER COLUMN rollup_id SET DEFAULT nextval('public.host_power_rollup_1m_rollup_id_seq'::regclass);


--
-- Name: hosting_command_presets id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.hosting_command_presets ALTER COLUMN id SET DEFAULT nextval('public.hosting_command_presets_id_seq'::regclass);


--
-- Name: hosts host_id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.hosts ALTER COLUMN host_id SET DEFAULT nextval('public.hosts_host_id_seq'::regclass);


--
-- Name: model_registry id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.model_registry ALTER COLUMN id SET DEFAULT nextval('public.model_registry_id_seq'::regclass);


--
-- Name: multi_node_deployments id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.multi_node_deployments ALTER COLUMN id SET DEFAULT nextval('public.multi_node_deployments_id_seq'::regclass);


--
-- Name: qualification_envelopes envelope_id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.qualification_envelopes ALTER COLUMN envelope_id SET DEFAULT nextval('public.qualification_envelopes_envelope_id_seq'::regclass);


--
-- Name: recovery_events id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.recovery_events ALTER COLUMN id SET DEFAULT nextval('public.recovery_events_id_seq'::regclass);


--
-- Name: request_routing_log id; Type: DEFAULT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.request_routing_log ALTER COLUMN id SET DEFAULT nextval('public.request_routing_log_id_seq'::regclass);


--
-- Name: agent_access agent_access_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.agent_access
    ADD CONSTRAINT agent_access_pkey PRIMARY KEY (agent_id, principal);


--
-- Name: agents agents_name_key; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.agents
    ADD CONSTRAINT agents_name_key UNIQUE (name);


--
-- Name: agents agents_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (agent_id);


--
-- Name: audit_events audit_events_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.audit_events
    ADD CONSTRAINT audit_events_pkey PRIMARY KEY (event_id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (message_id);


--
-- Name: plan_artifacts plan_artifacts_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.plan_artifacts
    ADD CONSTRAINT plan_artifacts_pkey PRIMARY KEY (plan_id);


--
-- Name: plan_artifacts plan_artifacts_slug_key; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.plan_artifacts
    ADD CONSTRAINT plan_artifacts_slug_key UNIQUE (slug);


--
-- Name: plan_versions plan_versions_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.plan_versions
    ADD CONSTRAINT plan_versions_pkey PRIMARY KEY (version_id);


--
-- Name: plan_versions plan_versions_plan_id_version_number_key; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.plan_versions
    ADD CONSTRAINT plan_versions_plan_id_version_number_key UNIQUE (plan_id, version_number);


--
-- Name: projects projects_name_key; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.projects
    ADD CONSTRAINT projects_name_key UNIQUE (name);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (project_id);


--
-- Name: provisioning_jobs provisioning_jobs_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.provisioning_jobs
    ADD CONSTRAINT provisioning_jobs_pkey PRIMARY KEY (job_id);


--
-- Name: provisioning_steps provisioning_steps_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.provisioning_steps
    ADD CONSTRAINT provisioning_steps_pkey PRIMARY KEY (job_id, seq);


--
-- Name: runs runs_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.runs
    ADD CONSTRAINT runs_pkey PRIMARY KEY (run_id);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (session_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (user_id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: work_items work_items_pkey; Type: CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.work_items
    ADD CONSTRAINT work_items_pkey PRIMARY KEY (item_id);


--
-- Name: agent_keys agent_keys_alias_key; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.agent_keys
    ADD CONSTRAINT agent_keys_alias_key UNIQUE (alias);


--
-- Name: agent_keys agent_keys_external_id_key; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.agent_keys
    ADD CONSTRAINT agent_keys_external_id_key UNIQUE (external_id);


--
-- Name: agent_keys agent_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.agent_keys
    ADD CONSTRAINT agent_keys_pkey PRIMARY KEY (key_id);


--
-- Name: benchmark_runs benchmark_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.benchmark_runs
    ADD CONSTRAINT benchmark_runs_pkey PRIMARY KEY (run_id);


--
-- Name: deployment_revisions deployment_revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.deployment_revisions
    ADD CONSTRAINT deployment_revisions_pkey PRIMARY KEY (revision_id);


--
-- Name: deployment_revisions deployment_revisions_revision_label_key; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.deployment_revisions
    ADD CONSTRAINT deployment_revisions_revision_label_key UNIQUE (revision_label);


--
-- Name: deployments deployments_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT deployments_pkey PRIMARY KEY (deployment_id);


--
-- Name: electricity_rates electricity_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.electricity_rates
    ADD CONSTRAINT electricity_rates_pkey PRIMARY KEY (rate_id);


--
-- Name: emporia_channel_map emporia_channel_map_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.emporia_channel_map
    ADD CONSTRAINT emporia_channel_map_pkey PRIMARY KEY (device_gid, channel_num);


--
-- Name: emporia_devices emporia_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.emporia_devices
    ADD CONSTRAINT emporia_devices_pkey PRIMARY KEY (device_gid, channel_num);


--
-- Name: facility_power_rollup_1d facility_power_rollup_1d_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.facility_power_rollup_1d
    ADD CONSTRAINT facility_power_rollup_1d_pkey PRIMARY KEY (day, device_gid, channel_num);


--
-- Name: facility_power_samples facility_power_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.facility_power_samples
    ADD CONSTRAINT facility_power_samples_pkey PRIMARY KEY (ts, device_gid, channel_num);


--
-- Name: gpu_samples gpu_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.gpu_samples
    ADD CONSTRAINT gpu_samples_pkey PRIMARY KEY (sample_id);


--
-- Name: host_power_rollup_1m host_power_rollup_1m_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.host_power_rollup_1m
    ADD CONSTRAINT host_power_rollup_1m_pkey PRIMARY KEY (rollup_id);


--
-- Name: hosting_command_presets hosting_command_presets_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.hosting_command_presets
    ADD CONSTRAINT hosting_command_presets_pkey PRIMARY KEY (id);


--
-- Name: hosts hosts_guest_ip_key; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.hosts
    ADD CONSTRAINT hosts_guest_ip_key UNIQUE (guest_ip);


--
-- Name: hosts hosts_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.hosts
    ADD CONSTRAINT hosts_pkey PRIMARY KEY (host_id);


--
-- Name: manager_settings manager_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.manager_settings
    ADD CONSTRAINT manager_settings_pkey PRIMARY KEY (key);


--
-- Name: model_registry model_registry_logical_model_name_host_id_key; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.model_registry
    ADD CONSTRAINT model_registry_logical_model_name_host_id_key UNIQUE (logical_model_name, host_id);


--
-- Name: model_registry model_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.model_registry
    ADD CONSTRAINT model_registry_pkey PRIMARY KEY (id);


--
-- Name: multi_node_deployments multi_node_deployments_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.multi_node_deployments
    ADD CONSTRAINT multi_node_deployments_pkey PRIMARY KEY (id);


--
-- Name: qualification_envelopes qualification_envelopes_deployment_ip_key; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.qualification_envelopes
    ADD CONSTRAINT qualification_envelopes_deployment_ip_key UNIQUE (deployment_ip);


--
-- Name: qualification_envelopes qualification_envelopes_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.qualification_envelopes
    ADD CONSTRAINT qualification_envelopes_pkey PRIMARY KEY (envelope_id);


--
-- Name: rdma_ring_nodes rdma_ring_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.rdma_ring_nodes
    ADD CONSTRAINT rdma_ring_nodes_pkey PRIMARY KEY (host);


--
-- Name: recovery_events recovery_events_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.recovery_events
    ADD CONSTRAINT recovery_events_pkey PRIMARY KEY (id);


--
-- Name: request_routing_log request_routing_log_pkey; Type: CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.request_routing_log
    ADD CONSTRAINT request_routing_log_pkey PRIMARY KEY (id);


--
-- Name: idx_agents_state; Type: INDEX; Schema: agentmanager; Owner: llmmanager
--

CREATE INDEX idx_agents_state ON agentmanager.agents USING btree (state);


--
-- Name: idx_audit_created; Type: INDEX; Schema: agentmanager; Owner: llmmanager
--

CREATE INDEX idx_audit_created ON agentmanager.audit_events USING btree (created_at DESC);


--
-- Name: idx_messages_session; Type: INDEX; Schema: agentmanager; Owner: llmmanager
--

CREATE INDEX idx_messages_session ON agentmanager.messages USING btree (session_id);


--
-- Name: idx_benchmark_runs_revision; Type: INDEX; Schema: public; Owner: llmmanager
--

CREATE INDEX idx_benchmark_runs_revision ON public.benchmark_runs USING btree (deployment_revision_id);


--
-- Name: idx_deployment_revisions_host; Type: INDEX; Schema: public; Owner: llmmanager
--

CREATE INDEX idx_deployment_revisions_host ON public.deployment_revisions USING btree (guest_ip);


--
-- Name: idx_gpu_samples_host_ts; Type: INDEX; Schema: public; Owner: llmmanager
--

CREATE INDEX idx_gpu_samples_host_ts ON public.gpu_samples USING btree (host_id, ts DESC);


--
-- Name: idx_gpu_samples_ts; Type: INDEX; Schema: public; Owner: llmmanager
--

CREATE INDEX idx_gpu_samples_ts ON public.gpu_samples USING btree (ts);


--
-- Name: idx_rollup_host_ts; Type: INDEX; Schema: public; Owner: llmmanager
--

CREATE INDEX idx_rollup_host_ts ON public.host_power_rollup_1m USING btree (host_id, ts DESC);


--
-- Name: uq_hcp_active_content; Type: INDEX; Schema: public; Owner: llmmanager
--

CREATE UNIQUE INDEX uq_hcp_active_content ON public.hosting_command_presets USING btree (scope_type, scope_key, engine, content_hash) WHERE (deleted_at IS NULL);


--
-- Name: agent_access agent_access_agent_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.agent_access
    ADD CONSTRAINT agent_access_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agentmanager.agents(agent_id) ON DELETE CASCADE;


--
-- Name: messages messages_session_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.messages
    ADD CONSTRAINT messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES agentmanager.sessions(session_id) ON DELETE CASCADE;


--
-- Name: plan_versions plan_versions_plan_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.plan_versions
    ADD CONSTRAINT plan_versions_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES agentmanager.plan_artifacts(plan_id) ON DELETE CASCADE;


--
-- Name: provisioning_jobs provisioning_jobs_agent_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.provisioning_jobs
    ADD CONSTRAINT provisioning_jobs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agentmanager.agents(agent_id) ON DELETE CASCADE;


--
-- Name: provisioning_steps provisioning_steps_job_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.provisioning_steps
    ADD CONSTRAINT provisioning_steps_job_id_fkey FOREIGN KEY (job_id) REFERENCES agentmanager.provisioning_jobs(job_id) ON DELETE CASCADE;


--
-- Name: runs runs_agent_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.runs
    ADD CONSTRAINT runs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agentmanager.agents(agent_id);


--
-- Name: runs runs_item_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.runs
    ADD CONSTRAINT runs_item_id_fkey FOREIGN KEY (item_id) REFERENCES agentmanager.work_items(item_id);


--
-- Name: runs runs_plan_version_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.runs
    ADD CONSTRAINT runs_plan_version_id_fkey FOREIGN KEY (plan_version_id) REFERENCES agentmanager.plan_versions(version_id);


--
-- Name: sessions sessions_agent_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.sessions
    ADD CONSTRAINT sessions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agentmanager.agents(agent_id) ON DELETE CASCADE;


--
-- Name: work_items work_items_active_plan_version_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.work_items
    ADD CONSTRAINT work_items_active_plan_version_id_fkey FOREIGN KEY (active_plan_version_id) REFERENCES agentmanager.plan_versions(version_id);


--
-- Name: work_items work_items_assigned_agent_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.work_items
    ADD CONSTRAINT work_items_assigned_agent_id_fkey FOREIGN KEY (assigned_agent_id) REFERENCES agentmanager.agents(agent_id);


--
-- Name: work_items work_items_project_id_fkey; Type: FK CONSTRAINT; Schema: agentmanager; Owner: llmmanager
--

ALTER TABLE ONLY agentmanager.work_items
    ADD CONSTRAINT work_items_project_id_fkey FOREIGN KEY (project_id) REFERENCES agentmanager.projects(project_id);


--
-- Name: deployments deployments_host_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT deployments_host_id_fkey FOREIGN KEY (host_id) REFERENCES public.hosts(host_id);


--
-- Name: gpu_samples gpu_samples_host_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.gpu_samples
    ADD CONSTRAINT gpu_samples_host_id_fkey FOREIGN KEY (host_id) REFERENCES public.hosts(host_id);


--
-- Name: host_power_rollup_1m host_power_rollup_1m_host_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.host_power_rollup_1m
    ADD CONSTRAINT host_power_rollup_1m_host_id_fkey FOREIGN KEY (host_id) REFERENCES public.hosts(host_id);


--
-- Name: model_registry model_registry_host_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: llmmanager
--

ALTER TABLE ONLY public.model_registry
    ADD CONSTRAINT model_registry_host_id_fkey FOREIGN KEY (host_id) REFERENCES public.hosts(host_id);


--
-- PostgreSQL database dump complete
--

\unrestrict mvkpd4WowtiAk3fOHiH6Mjd9I9Go6NSafTEzxE5lP8Hk4fmEusCYyvScmlrrTMe

