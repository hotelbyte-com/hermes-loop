# Documentation website guidance

Applies to the standalone Docusaurus site under `website/`.

- User documentation must match current commands, config keys, platform support,
  defaults, and security behavior. Verify claims against source/CLI help rather
  than copying stale README prose.
- Keep internal design notes distinct from user-facing guarantees. Never publish
  tokens, internal endpoints, local absolute paths, or unverified capability
  claims.
- Preserve links, sidebar registration, localization structure, and versioned
  redirects when moving pages.
- Use Mermaid, lists, or tables instead of ASCII box diagrams; CI runs
  `ascii-guard` over documentation.

`website/` has its own `package-lock.json`. Run from that directory:

```bash
npm ci
npm run typecheck
npm run lint:diagrams
npm run build
```

Use `npm start` for visual/link checks when layout or navigation changes.
