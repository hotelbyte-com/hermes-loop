# Loop product guidance

`loop/` is the HotelByte controllable-delivery control plane, not a generic
feature folder in the upstream Python agent. It is a standalone pnpm workspace:
central Hono/node:sqlite server plus React/Vite collaboration UI. Read
[`README.md`](README.md) before changes.

## Product invariants

- Identity has three layers: portable **Soul**, workspace-bound **Agent**, and
  machine-hosted **Instance**. `Machine` is an instance host, never the source
  of agent identity.
- The central service owns metadata, dispatch decisions, queues, and audit. It
  never executes an agent runtime; machines execute runtimes locally.
- Every recipient decision produces auditable delivery evidence: delivered,
  excluded, or deferred with a structured reason. Ordinary messages do not fan
  out; only explicit structured wake rules (`@agent`, allowed `@all`, allowed
  `@online`, task assignment) may dispatch.
- Private/thread/workspace context never broadens on delivery or reply. Runtime
  replies re-enter the same `postMessage` decision path rather than bypassing
  audit.
- No body-substring routing outside the dedicated mention parser. Decisions use
  parsed mentions, membership, policy, online-instance state, and typed fields.
- Duplicate agent replies are unacceptable. Claims, renewal, completion,
  cancellation, takeover, and task coupling use CAS/state transitions and keep
  lost-lease/late-complete behavior visible.

## Workspace commands

From `loop/`:

```bash
pnpm install
pnpm typecheck
pnpm test
pnpm build:web
```

For local end-to-end work, use `pnpm seed`, `pnpm dev:server`, `pnpm dev:web`,
and `pnpm machine` in separate terminals with disposable `.data` state.

Load the closer guide for [`server/`](server/AGENTS.md) or
[`web/`](web/AGENTS.md).
