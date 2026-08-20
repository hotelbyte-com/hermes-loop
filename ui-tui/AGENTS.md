# Ink TUI guidance

Applies to `ui-tui/` and `ui-tui/packages/hermes-ink/`. TypeScript owns terminal
rendering; Python in `tui_gateway/` owns sessions, tools, model calls, and most
command behavior.

## Architecture and state

- `src/entry.tsx` starts `GatewayClient`, which spawns
  `python -m tui_gateway.entry`. Transport is newline-delimited JSON-RPC over
  stdio; treat unexpected stdout as protocol corruption and surface it as such.
- Keep route/application roots compositional. Shared or distant UI state belongs
  in small feature-owned nanostores; non-rendering actions read `$atom.get()`
  and rendering components use `useStore`.
- Do not pass shared state through long prop chains or build monolithic hooks.
  Keep pure helpers in `src/lib`, route/page composition in `src/app`, and
  feature actions beside their state.
- Preserve the bounded gateway crash-recovery budget, queued-message semantics,
  transcript immutability, prompt request IDs, and active-run interruption.

## Commands and protocol

- TUI-owned commands are registered in `src/app/slash/registry.ts`; all other
  commands use gateway `slash.exec` and its `command.dispatch` fallback.
- Do not duplicate the backend's command/skill catalog in a static client list.
  Local commands should be limited to behavior the TUI itself must own.
- Approval/clarify/sudo/secret flows are structured UI modes. Main input must
  stay suspended while a blocking prompt is active, and cancellation/secret
  values must not leak into the transcript.
- If an async callback is intentionally fire-and-forget, make that intent
  explicit with `void`; propagate actionable failures through visible state.

## Verification

Install workspace dependencies from the repository root once, then run from
`ui-tui/`:

```bash
npm run typecheck
npm run lint
npm test
npm run build
```

For protocol changes, also run the targeted `tests/tui_gateway/` file through
`scripts/run_tests.sh`. Exercise the TUI interactively for input, resize,
streaming, or terminal-rendering changes that unit tests cannot prove.
