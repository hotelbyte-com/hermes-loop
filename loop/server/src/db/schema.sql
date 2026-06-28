-- Loop control plane schema
-- D-020 Soul / Agent / Instance three-layer model
-- D-022 MessageDelivery diagnostics (the moat)
--
-- Storage: node:sqlite (DatabaseSync). Text PKs, INTEGER epoch-ms timestamps,
-- JSON columns stored as TEXT and parsed in app code.

-- A workspace is the top-level tenancy boundary for collaboration.
CREATE TABLE IF NOT EXISTS workspace (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

-- Soul: a migratable role asset (SOUL.md + skills + knowledge). Stable across
-- workspaces/machines. This is the portable identity in D-020.
CREATE TABLE IF NOT EXISTS soul (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('agent','human')),
  role TEXT,
  description TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE (workspace_id, name)
);

-- Human participant (an author / recipient of messages).
CREATE TABLE IF NOT EXISTS human (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE (workspace_id, name)
);

-- Agent: the collaborative role inside a workspace — the @target. Backed by a Soul.
CREATE TABLE IF NOT EXISTS agent (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  soul_id TEXT NOT NULL REFERENCES soul(id) ON DELETE CASCADE,
  display_name TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE (workspace_id, display_name)
);

-- Machine: host of Instances. "One desktop app = one Machine" (D-013).
CREATE TABLE IF NOT EXISTS machine (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  owner TEXT,
  created_at INTEGER NOT NULL
);

-- Instance: an Agent running on a Machine under a given runtime. Carries online
-- state. The runtime is EXTERNAL (D-021): claude-code / opencode / codex / glm /
-- deepseek / hermes. Machine is the host, not the identity source.
CREATE TABLE IF NOT EXISTS instance (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
  machine_id TEXT NOT NULL REFERENCES machine(id) ON DELETE CASCADE,
  runtime TEXT NOT NULL,
  online INTEGER NOT NULL DEFAULT 0,
  last_seen_at INTEGER,
  created_at INTEGER NOT NULL,
  UNIQUE (agent_id, machine_id, runtime)
);

-- Channel: a collaboration surface. broadcast_policy + context_scope drive the
-- delivery decider (no behavioral string matching — see delivery/decider.ts).
CREATE TABLE IF NOT EXISTS channel (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'channel',
  broadcast_policy TEXT NOT NULL,            -- JSON
  context_scope TEXT NOT NULL DEFAULT 'channel',
  created_at INTEGER NOT NULL,
  UNIQUE (workspace_id, name)
);

-- Channel membership. member_kind discriminates human vs agent (single table, typed).
CREATE TABLE IF NOT EXISTS channel_member (
  channel_id TEXT NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
  member_id TEXT NOT NULL,
  member_kind TEXT NOT NULL CHECK (member_kind IN ('human','agent')),
  role TEXT NOT NULL DEFAULT 'member',
  PRIMARY KEY (channel_id, member_id, member_kind)
);

-- Thread: a topic within a channel. parent_thread_id allows nesting.
CREATE TABLE IF NOT EXISTS thread (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
  parent_thread_id TEXT REFERENCES thread(id) ON DELETE CASCADE,
  title TEXT,
  created_at INTEGER NOT NULL
);

-- Task: parent/child work items (D-015 PM delivery loop).
CREATE TABLE IF NOT EXISTS task (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  parent_task_id TEXT REFERENCES task(id) ON DELETE CASCADE,
  thread_id TEXT REFERENCES thread(id) ON DELETE SET NULL,
  assignee_id TEXT,
  assignee_kind TEXT CHECK (assignee_kind IN ('human','agent')),
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at INTEGER NOT NULL
);

-- Message: authored content. `mentions` holds PARSED structured tokens produced
-- by delivery/parser.ts (protocol tokenization), never matched as raw substrings.
CREATE TABLE IF NOT EXISTS message (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
  thread_id TEXT REFERENCES thread(id) ON DELETE SET NULL,
  author_id TEXT NOT NULL,
  author_kind TEXT NOT NULL CHECK (author_kind IN ('human','agent')),
  body TEXT NOT NULL,
  mentions TEXT NOT NULL DEFAULT '[]',        -- JSON: parsed mention tokens
  broadcast_policy_override TEXT,            -- JSON or NULL
  context_scope TEXT,                        -- NULL = inherit channel default
  notices TEXT NOT NULL DEFAULT '[]',        -- JSON: message-level delivery notices (e.g. broadcast blocked)
  created_at INTEGER NOT NULL
);

-- MessageDelivery: the diagnostics snapshot. One row per (message, candidate
-- recipient). This is the moat (D-022): every hit / miss / wake / defer is auditable
-- and surfaces in the console delivery-diagnostics panel.
CREATE TABLE IF NOT EXISTS message_delivery (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES message(id) ON DELETE CASCADE,
  recipient_id TEXT NOT NULL,
  recipient_kind TEXT NOT NULL CHECK (recipient_kind IN ('human','agent')),
  delivery_state TEXT NOT NULL CHECK (delivery_state IN ('delivered','excluded','deferred')),
  wake INTEGER NOT NULL DEFAULT 0,
  reason_code TEXT NOT NULL,
  reason_detail TEXT,
  matched_rule_id TEXT,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_delivery_message ON message_delivery(message_id);
CREATE INDEX IF NOT EXISTS idx_message_channel ON message(channel_id);
CREATE INDEX IF NOT EXISTS idx_message_thread ON message(thread_id);
CREATE INDEX IF NOT EXISTS idx_member_channel ON channel_member(channel_id);
CREATE INDEX IF NOT EXISTS idx_instance_agent ON instance(agent_id);

-- Dispatch: the runtime execution bridge (D-024). When a message delivery WAKES an
-- agent (verdict.wake && recipientKind='agent' && state='delivered'), the control
-- plane records a dispatch targeting that agent. A Machine hosting an ONLINE instance
-- of the agent may poll/claim/complete it. The server NEVER executes a runtime (D-021):
-- it only schedules delivery + records the dispatch lifecycle. On `complete` with a
-- replyBody, the runtime's output is posted back as an agent-authored message (via the
-- same postMessage critical path), re-entering the decider — so delivery diagnostics
-- compose across the human -> agent -> reply chain, all auditable.
--
-- A dispatch targets an AGENT, not a specific machine: any machine with an online
-- instance of that agent is eligible. Pending dispatches for an offline agent simply
-- wait (queued) until an instance comes online — direct @mention of an offline agent
-- is delivered+queued, not dropped.
CREATE TABLE IF NOT EXISTS dispatch (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES message(id) ON DELETE CASCADE,
  delivery_id TEXT NOT NULL REFERENCES message_delivery(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  channel_id TEXT NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
  thread_id TEXT REFERENCES thread(id) ON DELETE SET NULL,
  agent_id TEXT NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
  runtime TEXT,                          -- expected runtime (informational; instance runtime at creation)
  state TEXT NOT NULL DEFAULT 'pending'
       CHECK (state IN ('pending','claimed','done','failed','dead')),
  payload TEXT NOT NULL,                 -- JSON snapshot consumed by the runtime
  result TEXT,                           -- JSON {ok, replyBody?, error?} filled on done/failed
  claimed_by_machine TEXT REFERENCES machine(id) ON DELETE SET NULL,
  claimed_at INTEGER,
  completed_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispatch_pending ON dispatch(state, created_at);
CREATE INDEX IF NOT EXISTS idx_dispatch_agent ON dispatch(agent_id, state);
CREATE INDEX IF NOT EXISTS idx_dispatch_delivery ON dispatch(delivery_id);
