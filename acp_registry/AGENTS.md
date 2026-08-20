# ACP registry guidance

`acp_registry/` is declarative discovery metadata for ACP clients.

- Keep identifiers, entrypoints, capabilities, and version constraints aligned
  with the actual `acp_adapter` implementation and packaging metadata.
- Do not advertise a capability that lacks a registered handler and an adapter
  test. Roadmap behavior must remain clearly marked as unsupported.
- Registry entries contain no credentials, local absolute paths, or
  machine-specific state.
- Preserve compatibility for existing client discovery; treat renamed IDs or
  removed fields as a migration, not a cosmetic edit.

Run ACP adapter/registry tests through `scripts/run_tests.sh` and inspect the
packaged artifact when registry inclusion is part of the claim.
