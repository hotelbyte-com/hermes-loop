# Optional MCP catalog guidance

`optional-mcps/` contains catalog manifests for MCP servers that are not active
by default.

- Keep manifests declarative and schema-valid. A catalog entry must not imply
  that the server is bundled, running, authenticated, or verified when it is not.
- Declare required commands/config/secrets as metadata; never commit live values
  or route a secret through model-visible prose.
- Prefer a cataloged MCP server over a new core model tool for non-fundamental
  structured capability.
- Installation and disable/uninstall paths must be reversible and scoped to the
  active profile.

Run the MCP/catalog parser tests under `tests/skills/`, `tests/tools/`, or the
specific existing target found with `rg`, using `scripts/run_tests.sh`.
