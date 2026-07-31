# Model-provider plugin guidance

Each provider directory registers one `ProviderProfile` and includes a
`plugin.yaml` with `kind: model-provider`.

- Discovery is lazy and separate from the general PluginManager:
  `providers/__init__.py` scans bundled providers, then the active
  `$HERMES_HOME/plugins/model-providers`, then legacy modules. User registration
  is last-writer-wins and may override a bundled provider.
- A normal provider requires only its profile registration and manifest. The
  provider registry auto-wires auth/config/model metadata/runtime consumers; do
  not add parallel provider-name branches elsewhere.
- Put protocol quirks in `ProviderProfile` hooks/subclasses with structured
  request transformations. Keep transport, reasoning, token, and temperature
  behavior explicit; do not identify providers from URL/name substrings.
- Credentials remain secret metadata. Provider absence/auth failure must be
  visible and must not silently fall through to fake output.
- Keep dependency bounds and model-catalog tests invariant-based; never snapshot
  the current list of models or provider count.

Read [`README.md`](README.md), then run targeted tests under
`tests/providers/` and `tests/plugins/` through `scripts/run_tests.sh`.
