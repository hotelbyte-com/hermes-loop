# Kanban plugin guidance

Kanban is a durable SQLite-backed multi-agent queue. Its dashboard, dispatcher,
CLI, and worker toolset must preserve one lifecycle and isolation model.

- Board is the hard visibility boundary. Dispatcher workers are pinned through
  structured board/workspace/run identifiers; tenant is only a namespace inside
  a board. Never infer board or tenant routing from prompt text.
- Claims, completion, stale reclaim, retries, and auto-blocking must be atomic
  and auditable. A partial/failed worker result stays visible; do not mark it
  successful or spin indefinitely.
- The gateway-hosted dispatcher is canonical when enabled. Standalone systemd
  assets must use the same database, locks, limits, and state transitions.
- Worker-only `kanban_*` tools remain service-gated so their model-schema
  footprint is zero outside a dispatched task.
- Preserve bounded failure attempts and heartbeats. Process restart must not
  lose durable tasks, claims, comments, or dependency links.

Verify CLI/tool/plugin tests plus a real temporary-board dispatch lifecycle
through `scripts/run_tests.sh`.
