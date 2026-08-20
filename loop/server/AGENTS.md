# Loop server guidance

Applies to the Hono API, node:sqlite store/schema, delivery decision engine,
dispatch/task services, seed path, and machine client.

## Delivery and tenancy

- `api/delivery-service.ts` plus `delivery/decider.ts` is the only delivery
  critical path. Routes transport typed requests; views map rows; neither may
  reimplement decision policy.
- Create a `message_delivery` verdict for every candidate, including excluded
  and deferred recipients. A wake-agent delivered verdict has at most one
  linked dispatch.
- Scope every workspace-owned read/write with a structured workspace equality.
  Cross-workspace references and foreign tokens return 404 where the contract
  hides existence; do not fetch globally and filter in application code.
- Mention/broadcast/assignment routing uses parsed typed facts. The
  hard-ban test in `src/test/dispatch.test.ts` must continue rejecting new
  `.contains`/`.indexOf` keyword routing outside `delivery/parser.ts`.

## Dispatch, tasks, and machines

- Dispatch transitions are CAS-protected: pending → claimed → completed/failed,
  with explicit abandon/requeue/dead behavior. Only the claiming machine may
  renew or complete.
- `LOOP_INSTANCE_TTL_MS` must remain greater than claim TTL. Poll refreshes the
  calling machine's online instances before global stale-instance reap. Lost
  lease or late completion returns conflict and posts no duplicate reply.
- Runtime output with `replyBody` goes through `postMessage` using the captured
  channel/thread scope. Never insert an agent reply directly.
- Task assignment is one explicit wake/dispatch. Double assignment is a
  conflict; cancellation dead-letters non-terminal dispatches; completed tasks
  remain terminal.
- Machine tokens are opaque, stored hashed, and compared/authenticated through
  the existing boundary. Never log or return the full stored credential.

## Seed and network safety

The demo seed HTTP endpoint trusts the configured bind host, never the client
`Host` header. Loopback is the default. A non-loopback bind requires
`LOOP_SEED_TOKEN`; absent authorization hides the endpoint, and token comparison
remains constant-time. The local seed CLI may call the seed service directly.

## Verification

From `loop/`:

```bash
pnpm --filter ./server typecheck
pnpm --filter ./server test
```

Add invariant tests for quiet-default zero dispatch, complete delivery evidence,
workspace isolation, single-winner claims, TTL/renewal/takeover, task coupling,
and duplicate-reply count. Do not weaken these tests to accommodate a change.
