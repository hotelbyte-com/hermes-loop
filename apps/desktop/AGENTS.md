# Desktop application guidance

Hermes Desktop is a separate Electron + React chat surface. It does **not**
embed `hermes --tui`; the renderer talks to the Python `tui_gateway`/dashboard
backend through structured requests and events.

## Runtime and state boundaries

- Keep agent/session/tool truth in the Python backend. The renderer owns
  presentation, local windows, onboarding, settings UX, and desktop-specific
  interaction state.
- Backend resolution and update/bootstrap logic live in `electron/main.cjs` and
  related modules. Preserve its explicit resolution order, platform-specific
  child-process cleanup, packaged-resource paths, and signing boundaries.
- Use `HERMES_HOME`/existing resolver helpers; never write test or preview data
  into a real user installation. Do not log credentials or OAuth tokens.
- Shared renderer state follows `../AGENTS.md`: feature-owned nanostores and
  narrow hooks, not controller components or duplicated backend state.

## Slash commands

The backend already exposes built-ins, quick commands, and skill commands via
`commands.catalog` and `complete.slash`.

- `src/lib/desktop-slash-commands.ts` curates noisy built-ins for desktop.
  `isDesktopSlashCommand` gates execution and `isDesktopSlashSuggestion` gates
  discovery.
- User quick commands and skill commands are extensions, not built-in noise.
  Preserve `isDesktopSlashExtensionCommand` in both catalog and typed-completion
  paths. Do not tighten the curated list in a way that hides extensions.
- `src/app/session/hooks/use-prompt-actions.ts` owns desktop-local dispatch;
  unowned commands flow to `slash.exec`, then `command.dispatch`.
- Do not add a client-only duplicate of the backend catalog or a new RPC solely
  to enumerate skills.

## Verification

Install from the repository root, then run from `apps/desktop/`:

```bash
npm run typecheck
npm run lint
npm run test:ui
npm run build
```

Use `npm run test:desktop:platforms` for Electron/platform process changes and
the appropriate `test:desktop:*` packaging smoke for installer/update work.
Run targeted `tests/tui_gateway/` Python tests when the JSON-RPC contract changes.
