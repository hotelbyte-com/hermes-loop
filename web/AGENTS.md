# Browser dashboard guidance

Applies to the React/Vite SPA under `web/`. The Python API/static server lives in
`hermes_cli/web_server.py` and serves the build from `hermes_cli/web_dist/`.

## Chat boundary

`ChatPage.tsx` embeds the real `hermes --tui` through xterm.js and `/api/pty`.
Do not rebuild the primary transcript, composer, slash-command behavior, or
terminal in React. Extend the Ink TUI so CLI and dashboard receive the same chat
behavior.

Structured React UI around the terminal is allowed: sidebars, model pickers,
inspectors, summaries, and status panels. Keep that state independent from the
PTY child session and make failures non-destructive so the terminal keeps
working.

Preserve:

- `/api/pty` session-ticket/token authentication, including stronger gated/OAuth
  rules;
- raw PTY byte forwarding and `\x1b[RESIZE:<cols>;<rows>]` framing;
- xterm fit/unicode/WebGL lifecycle and cleanup; and
- browser base-path/proxy behavior.

## React and visual rules

- Use typed clients in `src/lib/api.ts`; do not duplicate backend authorization,
  config defaults, or session truth.
- Text intended to be read uses at least `text-xs` and effective opacity at
  least 0.7. Do not stack alpha on semantic text tokens.
- Prefer semantic design-system tokens. Use `text-display` for brand chrome,
  not new raw `uppercase` classes. Do not apply themed body/font classes to
  global layout shells.
- Follow the shared state/component rules in [`../apps/AGENTS.md`](../apps/AGENTS.md)
  for nanostores, thin route roots, and narrow hooks.

## Development and verification

Run the backend from the repository root and Vite from `web/` for live changes:

```bash
python -m hermes_cli.main web --no-open
npm run dev
```

Before completion:

```bash
npm run typecheck
npm run lint
npm test
npm run build
```

`npm run build` writes the bundle consumed by the Python server. Restart the
dashboard and exercise the built route for PTY/auth/static-serving changes.
