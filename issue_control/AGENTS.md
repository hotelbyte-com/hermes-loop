# Issue Control Plane guidance

Read `../docs/issue-control-plane-phase1a.md` before changing this package.
Phase 1A is a shadow-only, read-only GitHub issue observer; it is not an agent
or Kanban execution path and must not create worktrees, branches, PRs, merges,
deployments, or Policy Gateway decisions.

- PostgreSQL is the durable authority for events, sessions, leases, fencing,
  replay, and reconciliation. Redis is replaceable acceleration only.
- Preserve append-only event/snapshot storage, one active session per issue,
  transactional epoch checks, deterministic replay, and stale-leader rejection.
- GitHub permissions must remain read-only. Configuration accepts secret
  references, not raw credentials, App private keys, or write tokens.
- Keep payloads bounded, redacted, content-addressed, and correlated by issue,
  session, run, and lease epoch. Do not turn missing state into empty success.
- Keep `/internal/*` private and require signed, bounded ingress for
  `/github/events`; standby nodes must reject event ingestion.

Use `scripts/run_tests.sh tests/issue_control/` for focused behavior. PostgreSQL
contracts require the explicit integration DSN and `--include-integration`;
local SQLite or mocked Redis/S3 is not production durability proof.

## Code review rules

- Reject write-capable GitHub paths, non-shadow modes, Redis authority,
  unfenced mutations, mutable audit rows, unbounded payloads, leaked secrets,
  unsafe failover, and tests that skip the real PostgreSQL contract they claim.
