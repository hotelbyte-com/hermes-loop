-- Hermes Issue Control Plane Phase 1A
--
-- PostgreSQL is the durable fact source. Redis and local process state are
-- intentionally absent from this schema. The vector extension is installed
-- now so later memory references can add embeddings without changing the
-- repository seam or creating a second authority.

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS issue_control_leases (
    cluster_name TEXT PRIMARY KEY,
    primary_node TEXT NOT NULL,
    standby_node TEXT NOT NULL,
    leader_node TEXT,
    lease_epoch BIGINT NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    renewed_at TIMESTAMPTZ NOT NULL,
    last_takeover_at TIMESTAMPTZ,
    CHECK (primary_node <> standby_node),
    CHECK (leader_node IS NULL OR leader_node IN (primary_node, standby_node))
);

CREATE TABLE IF NOT EXISTS issue_control_nodes (
    node_id TEXT PRIMARY KEY,
    configured_role TEXT NOT NULL CHECK (configured_role IN ('primary', 'standby')),
    ready BOOLEAN NOT NULL DEFAULT FALSE,
    observed_epoch BIGINT NOT NULL DEFAULT 0 CHECK (observed_epoch >= 0),
    last_seen_at TIMESTAMPTZ,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS issue_sessions (
    session_id TEXT PRIMARY KEY,
    issue_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'discovered', 'triaged', 'planned', 'executing', 'reviewing',
            'awaiting_human', 'pr_open', 'checks_green', 'merged',
            'verified', 'closed', 'failed_retryable', 'quarantined'
        )
    ),
    context_version BIGINT NOT NULL CHECK (context_version >= 1),
    task_graph_ref TEXT,
    active_run_id TEXT,
    risk_tier TEXT NOT NULL CHECK (risk_tier IN ('unknown', 'low', 'medium', 'high')),
    lease_epoch BIGINT NOT NULL CHECK (lease_epoch >= 1),
    last_github_version BIGINT NOT NULL DEFAULT -1 CHECK (last_github_version >= -1),
    last_github_tiebreaker TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_issue_sessions_one_active
    ON issue_sessions (issue_key)
    WHERE ended_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_issue_sessions_state
    ON issue_sessions (state)
    WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS issue_events (
    ledger_sequence BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    issue_key TEXT NOT NULL,
    github_version BIGINT NOT NULL CHECK (github_version >= 0),
    event_type TEXT NOT NULL,
    actor_kind TEXT NOT NULL CHECK (
        actor_kind IN ('human', 'bot', 'hermes', 'system', 'unknown')
    ),
    occurred_at TIMESTAMPTZ NOT NULL,
    sanitized_payload_ref TEXT NOT NULL CHECK (
        length(sanitized_payload_ref) BETWEEN 1 AND 2048
        AND sanitized_payload_ref ~ '^s3://[^/[:space:]]+/.+$'
    ),
    session_id TEXT NOT NULL REFERENCES issue_sessions(session_id),
    run_id TEXT NOT NULL CHECK (length(btrim(run_id)) > 0),
    lease_epoch BIGINT NOT NULL CHECK (lease_epoch >= 1),
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issue_events_issue_sequence
    ON issue_events (issue_key, ledger_sequence);

CREATE TABLE IF NOT EXISTS issue_session_snapshots (
    snapshot_sequence BIGSERIAL PRIMARY KEY,
    issue_key TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES issue_sessions(session_id),
    state TEXT NOT NULL,
    context_version BIGINT NOT NULL CHECK (context_version >= 1),
    task_graph_ref TEXT,
    active_run_id TEXT,
    risk_tier TEXT NOT NULL,
    mutation_kind TEXT NOT NULL,
    event_id TEXT,
    run_id TEXT NOT NULL CHECK (length(btrim(run_id)) > 0),
    lease_epoch BIGINT NOT NULL CHECK (lease_epoch >= 1),
    recorded_at TIMESTAMPTZ NOT NULL,
    UNIQUE (session_id, context_version)
);

CREATE INDEX IF NOT EXISTS idx_issue_snapshots_issue_sequence
    ON issue_session_snapshots (issue_key, snapshot_sequence);

CREATE TABLE IF NOT EXISTS issue_memory_references (
    memory_sequence BIGSERIAL PRIMARY KEY,
    issue_key TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES issue_sessions(session_id),
    run_id TEXT,
    source_ref TEXT NOT NULL,
    embedding_model TEXT,
    embedding_dimensions INTEGER CHECK (embedding_dimensions > 0),
    embedding vector,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, source_ref)
);

CREATE TABLE IF NOT EXISTS issue_reconciliation_status (
    repository TEXT PRIMARY KEY,
    run_id TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    newest_github_updated_at TIMESTAMPTZ,
    open_issue_count BIGINT NOT NULL DEFAULT 0 CHECK (open_issue_count >= 0),
    observed_issue_count BIGINT NOT NULL DEFAULT 0 CHECK (observed_issue_count >= 0),
    error TEXT,
    lease_epoch BIGINT CHECK (lease_epoch >= 1)
);

CREATE OR REPLACE FUNCTION reject_append_only_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS issue_events_append_only ON issue_events;
CREATE TRIGGER issue_events_append_only
BEFORE UPDATE OR DELETE ON issue_events
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS issue_snapshots_append_only ON issue_session_snapshots;
CREATE TRIGGER issue_snapshots_append_only
BEFORE UPDATE OR DELETE ON issue_session_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
