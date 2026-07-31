# Messaging gateway guidance

Applies to `gateway/`, including session routing, config, platform adapters,
relays, hooks, and delivery. For a new adapter, read
[`platforms/ADDING_A_PLATFORM.md`](platforms/ADDING_A_PLATFORM.md) completely.

## Platform extension boundary

- Community/third-party platforms use a plugin under
  `plugins/platforms/<name>/` or the user's plugin directory. Prefer the generic
  plugin hooks for YAML/env enablement, cron delivery, standalone sending, and
  setup metadata over adding platform-specific core branches.
- A built-in adapter is exceptional. It must cover adapter lifecycle, enum and
  config, factory, both authorization maps, session identity, prompt hint,
  toolset/preset, cron delivery, setup metadata, docs, dependencies, and tests.
- Shared behavior between two transports for one platform belongs in a mixin;
  transport-specific I/O stays in each adapter. Put the mixin before
  `BasePlatformAdapter` when it overrides base behavior.

## Session, authorization, and control

- Construct `SessionSource` through `build_source()`. Preserve chat/thread/user
  identity and round-trip serialization; do not merge conversations across
  platform or profile boundaries.
- Filter self/echo events, redact tokens and personal identifiers in logs, and
  use bounded retry with backoff/jitter for streaming connections.
- Credential-backed adapters acquire a scoped token lock when connecting and
  release it when disconnecting so two profiles cannot use the same account.
- Authorization and approval controls must use structured IDs/state. Never
  infer approval or a control command from arbitrary body keywords.

The busy-session path has two guards:

1. `gateway/platforms/base.py` may queue messages while a session is active.
2. `gateway/run.py` intercepts control/approval commands before ordinary agent
   interruption and background dispatch.

Any command that must unblock an active agent must bypass **both** guards and be
handled inline. Routing it through `_process_message_background()` races the
session lifecycle.

## Configuration and delivery

- Gateway runtime reads user YAML through `gateway/run.py` and
  `gateway/config.py`, not only `hermes_cli/config.py:DEFAULT_CONFIG`. Verify
  YAML, environment-compatibility bridges, and setup UI together.
- `terminal.cwd` is the canonical messaging workdir; it may be bridged to
  `TERMINAL_CWD` for child tools. Do not restore `MESSAGING_CWD`.
- Background-process notifications are configured by
  `display.background_process_notifications` (`all`, `result`, `error`,
  `off`). Keep the legacy env bridge internal and preserve watcher/error
  semantics.
- Cron deliveries stay in their own cron session and must not be appended into
  the destination conversation, which would break message-role alternation.
- Always-registered hooks belong in `gateway/builtin_hooks/` only when they are
  genuinely generic; no speculative hook surface.

## Verification

```bash
scripts/run_tests.sh tests/gateway/
scripts/run_tests.sh tests/tui_gateway/   # shared command/protocol changes
```

For adapter work, prove config enablement, authorization, inbound dispatch,
outbound send, reconnect/cleanup, and cron delivery using the nearest fake or
real protocol boundary. Unit-testing only the formatter is insufficient.
