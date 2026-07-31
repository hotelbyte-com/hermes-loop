# Loop web guidance

Applies to the React/Vite collaboration and delivery-diagnostics UI.

- Render server-owned Soul/Agent/Instance, message, delivery, dispatch, and task
  contracts from typed API responses. Do not recompute delivery reason, wake,
  online eligibility, or task state in the browser.
- Delivery diagnostics are a product contract: show delivered/excluded/deferred
  state, structured reason, wake decision, and dispatch lifecycle without
  masking failures as empty success.
- Keep private/thread scope visible and do not merge data across workspace IDs.
  Treat API 404/403/409 distinctly rather than falling back to stale unrelated
  data.
- UI actions that claim, cancel, assign, or complete work must reflect the
  server response and concurrent conflict; optimistic state cannot invent a
  successful transition.
- Reuse shared typed helpers and keep page roots compositional; do not introduce
  ad hoc keyword routing from message body text.

From `loop/` run:

```bash
pnpm --filter ./web typecheck
pnpm --filter ./web build
```

For delivery/task UI changes, run server tests too and exercise the seeded
`#pm-delivery` flow against disposable local state.
