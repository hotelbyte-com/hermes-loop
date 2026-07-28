# Hermes Issue Control Plane — Phase 1A

Phase 1A is a durable, read-only observer for authorized GitHub issues. It is
not an execution orchestrator: it does not dispatch Kanban work, create
sandboxes, push branches, open or merge pull requests, deploy code, or call a
Policy Gateway. The existing Hermes/Kanban execution path is neither replaced
nor started by this module.

## Safety and authority

- PostgreSQL is the only fact source for issue events, active sessions,
  snapshots, reconciliation state, and the current fencing epoch.
- `issue_events` and `issue_session_snapshots` are append-only at the database
  boundary. Update and delete attempts fail in PostgreSQL.
- A partial unique index permits one active session per `issue_key`.
- Every event and session snapshot records issue, session, run, and lease
  epoch correlation.
- Redis contains only replaceable queue hints, rate counters, and a cached
  leader view. Redis loss may reduce readiness detail or delay a queue hint;
  it cannot erase or authorize a durable mutation.
- Sanitized, bounded payloads are content-addressed in S3-compatible storage.
  PostgreSQL stores only their bounded `s3://` references.
- The GitHub client exposes GET operations only. Startup audits the GitHub App
  installation permissions and refuses any permission other than `read` or
  `none`; `issues=read` is mandatory.
- `mode: shadow` is the only accepted mode. Unknown configuration, write-token
  fields, App private keys, and non-shadow modes fail startup validation.

The public contracts live in `issue_control/contracts.py`; the lifecycle graph
and fail-closed transition errors live in `issue_control/state_machine.py`.

## PostgreSQL and pgvector

`issue_control/migrations/0001_issue_control.sql` installs the `vector`
extension in `public`, creates the durable ledger/session tables, and creates
`issue_memory_references`. The latter already carries session/run/source
correlation plus an unconstrained `vector` column; a later memory phase can
choose an embedding model and dimensions without moving authority to Hermes
SQLite state.

The service identity needs:

- permission to create the configured `hermes_issue_control` schema;
- permission to create the `vector` extension during the first migration, or
  an administrator must pre-install it in `public`;
- normal DML rights inside that schema after migration.

Production PostgreSQL must be external to the Hermes nodes, backed up, and
reachable from both `s3` and `s5`. Local SQLite files and container volumes are
not supported fact sources.

## Configuration

Install the opt-in runtime dependencies:

```bash
pip install 'hermes-agent[issue-control]'
```

Add the following explicit section to the profile-aware
`~/.hermes/config.yaml`. Credentials remain secret references; the built-in
resolver supports `secret://env/NAME`, so the corresponding process
environment values are credentials only.

```yaml
issue_control:
  mode: shadow
  node_id: s3                       # use s5 on the standby
  primary_node: s3
  standby_node: s5

  # These may be literal credential-free service URLs or secret://env refs.
  postgres_dsn: secret://env/HERMES_ISSUE_CONTROL_POSTGRES_DSN
  redis_url: secret://env/HERMES_ISSUE_CONTROL_REDIS_URL

  renewal_interval_seconds: 10
  takeover_after_seconds: 60
  reconciliation_interval_seconds: 300

  authorized_repositories:
    - hotelbyte-com/hotel-be
    - hotelbyte-com/hotel-fe

  github:
    api_base_url: https://api.github.com
    read_token_secret_ref: secret://env/HERMES_ISSUE_CONTROL_GITHUB_READ_TOKEN
    webhook_secret_ref: secret://env/HERMES_ISSUE_CONTROL_WEBHOOK_SECRET

  payload_store:
    bucket: hermes-issue-events
    prefix: phase-1a
    endpoint_url: https://minio.internal

  # Keep these loopback-only unless an authenticated internal proxy fronts them.
  internal_host: 127.0.0.1
  internal_port: 8787
```

The GitHub installation token must come from an App installation whose current
permissions are read-only. Hermes does not accept an App private key or a
GitHub write-token configuration surface. Installation-token minting and
rotation therefore remain external deployment responsibilities in this phase.

Start one instance on each node:

```bash
hermes-issue-control --config ~/.hermes/config.yaml
```

## Leader and failover semantics

`s3` acquires the initial PostgreSQL lease. The leader renews every 10 seconds.
At 60 seconds without renewal, the other configured node may take over in a
row-locked PostgreSQL transaction that increments `lease_epoch`. Every durable
event, session, transition, and reconciliation mutation checks node, epoch,
and lease freshness in the same PostgreSQL transaction as its write.

An expired leader cannot renew itself after the takeover window. After `s5`
takes over, recovered `s3` observes the newer epoch and rejoins as standby; its
old-epoch writes are rejected before they reach the ledger. Status reports
expose both each node's fixed `configured_role` and its current
`leader`/`standby` role, so standby readiness remains accurate after takeover.

Schema migration is serialized with a PostgreSQL transaction-scoped advisory
lock. Concurrent `s3`/`s5` startup therefore cannot race the migration ledger;
the lock controls migration DDL only and does not replace row-locked leadership
or fencing.

## HTTP surfaces

- `POST /github/events` — signed GitHub `issues` and `issue_comment` ingress.
  Bodies are limited to 1 MiB, allowlisted, redacted, then stored in S3.
- `GET /internal/health` — process liveness.
- `GET /internal/ready` — PostgreSQL and verified read-only GitHub readiness;
  reports Redis independently because Redis is not authoritative.
- `GET /internal/status` — current leader/epoch, node roles and freshness,
  standby readiness, Redis degradation, classification coverage, and maximum
  reconciliation lag.
- `GET /internal/reconciliation` — per-repository run, count, error, epoch, and
  lag projection.

Expose `/github/events` through the authenticated/TLS GitHub App ingress. Keep
`/internal/*` on a private network or behind internal authentication.

## Deterministic replay

Webhook deliveries retain the GitHub delivery ID. Full reconciliation uses a
deterministic identity derived from issue key, GitHub `updated_at` version, and
event type. PostgreSQL deduplicates immutable event IDs. When distinct events
share the same GitHub timestamp, a stable hash of event type, sanitized payload
reference, and event ID breaks the tie, so opposite arrival orders produce the
same projection while both events remain in the ledger. Replays return an
explicit `duplicate` disposition; older observations append audit evidence with
a `stale` disposition but cannot regress the session projection.

## Verification

Run focused behavior tests with the repository wrapper:

```bash
scripts/run_tests.sh tests/issue_control/
```

Real PostgreSQL/pgvector contracts require:

```bash
HERMES_ISSUE_CONTROL_TEST_POSTGRES_DSN='postgresql://...' \
  scripts/run_tests.sh --include-integration \
  tests/issue_control/test_postgres_repository.py
```

CI provides a dedicated `pgvector/pgvector:pg16` PostgreSQL service for these
tests. They cover migration behavior, append-only enforcement, concurrent
session claims, duplicate/reordered replay, process restart, Redis loss,
`s3 -> s5` failover, stale-epoch rejection, reconciliation lag, and mutation
correlation. S3/MinIO durability, PostgreSQL backup/restore, network policy,
and secret-manager injection are deployment assumptions owned by Phase 2 and
must be proven there; local emulators are never treated as production truth.
