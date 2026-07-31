# TUI gateway guidance

Applies to the Python JSON-RPC backend shared by the Ink TUI and desktop app.
Read the client guide in [`../ui-tui/AGENTS.md`](../ui-tui/AGENTS.md) or
[`../apps/desktop/AGENTS.md`](../apps/desktop/AGENTS.md) when changing a
client-visible contract.

## Protocol contracts

- Transport is newline-delimited JSON-RPC over stdio. Stdout is protocol-only;
  diagnostics go to stderr/logging. A stray print can corrupt both clients.
- Requests, responses, and events need stable method names and structured
  payloads. Keep request IDs, cancellation, timeout, and late-response behavior
  intact.
- Python owns sessions, model/tool calls, approvals, and command execution. UI
  clients own rendering and a small curated set of local commands.
- `commands.catalog` and `complete.slash` already combine built-ins, quick
  commands, and skill commands. Do not add a parallel catalog RPC for one
  client.
- `slash.exec` falls through to `command.dispatch` for commands it does not own.
  Preserve that extension path and its error/progress semantics.
- Blocking approval, clarify, sudo, and secret prompts must remain correlated
  to their structured request IDs. Never serialize secret values into logs or
  normal transcript messages.

## Runtime behavior

- The gateway is long-lived. Clean up worker subprocesses, background tasks,
  session resources, and pending requests on shutdown or restart.
- Keep prompt/tool/session state in the Python runtime; do not duplicate it in
  the client or infer it from rendered text.
- Do not let malformed client input crash the request loop. Return a structured
  protocol error without converting backend failures into successful empty data.

## Verification

```bash
scripts/run_tests.sh tests/tui_gateway/
scripts/run_tests.sh tests/cli/            # slash/command bridge when relevant
```

Then run the affected client tests (`npm test` in `ui-tui/` or
`npm run test:ui` in `apps/desktop/`). Protocol changes are incomplete until a
real client request/response or event path is exercised.
