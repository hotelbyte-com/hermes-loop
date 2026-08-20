# Documentation guidance

Applies to repository design, security, middleware, observability, plan, and
handoff documents under `docs/`.

- Keep durable architecture decisions separate from temporary implementation
  notes. State status, scope, evidence, and superseding decision when applicable.
- Link every long guidance document from the root or closest nested `AGENTS.md`;
  an unlinked rename is not progressive disclosure because agents will not know
  to load it.
- Commands, paths, defaults, and invariants must be verified against the current
  checkout. Mark roadmap work as roadmap, not supported behavior.
- Never include credentials, private user data, or machine-specific absolute
  paths. Use Mermaid/tables/lists instead of brittle ASCII diagrams.
- When code changes invalidate a design/security/operations document, update the
  document in the same change or record the explicit gap.

Run existing doc/link/diagram checks for the affected site or package.
