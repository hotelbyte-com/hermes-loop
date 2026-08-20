# ACP adapter guidance

Applies to the Agent Client Protocol server, sessions, auth, permissions, tool
bridging, edit approval, events, and provenance.

- Keep ACP messages structured and version-compatible. Validate requests at the
  protocol boundary and return typed protocol errors; stdout/transport channels
  must not contain ad hoc diagnostics.
- Preserve session/workspace identity and provenance through tool calls and
  edits. Never infer authorization or approval from natural-language content.
- File edits and dangerous tools use the existing permission/edit-approval
  flow. A missing/expired approval fails closed and stays auditable.
- Auth tokens and secrets never enter normal events, logs, or model-visible
  payloads. Compare credentials using the existing secure boundary.
- Clean up active sessions, tasks, and subprocesses on disconnect/cancellation.
  Do not let one ACP client mutate another client's state.

Verify through the canonical wrapper:

```bash
scripts/run_tests.sh tests/acp_adapter/ tests/acp/
```

Exercise the real adapter entrypoint for transport/session lifecycle changes.
