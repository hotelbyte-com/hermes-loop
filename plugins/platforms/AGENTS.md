# Platform plugin guidance

Platform plugins follow the general plugin contract plus the gateway adapter
contract. Read [`../../gateway/platforms/ADDING_A_PLATFORM.md`](../../gateway/platforms/ADDING_A_PLATFORM.md)
and [`../../gateway/AGENTS.md`](../../gateway/AGENTS.md) before changes.

- Register adapters through `ctx.register_platform()`; use generic YAML/env,
  cron-delivery, standalone-sender, and setup-metadata hooks.
- Preserve inbound authorization, self/echo filtering, `SessionSource`, scoped
  credential locks, redacted logs, reconnect cleanup, message limits, and media
  handling.
- Approval/control callbacks use the existing structured resolver IDs and must
  bypass both busy-session guards when they unblock an active agent.
- Platform-specific time-window UX belongs in an adapter override with cleanup
  in `finally`; keep the base typing heartbeat alive.

Test adapter registration, config, auth, inbound/outbound behavior, cleanup, and
cron delivery with targeted gateway/plugin tests.
