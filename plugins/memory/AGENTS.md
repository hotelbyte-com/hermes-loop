# Memory-provider plugin guidance

The in-tree provider set is closed. New memory backends ship as standalone
plugin repositories installed under the user's active plugin directory or via
Python entry points. Bug fixes to existing bundled providers are welcome.

- Implement `agent/memory_provider.py:MemoryProvider`; orchestration stays in
  `agent/memory_manager.py`. Provider-specific SDK/auth/retry behavior remains in
  the provider directory.
- Preserve lifecycle contracts such as `sync_turn`, `prefetch`, `shutdown`, and
  optional setup integration. Make shutdown/retry idempotent and bounded.
- CLI commands in a provider's `cli.py` are exposed only for the active provider.
  Do not clutter global help or import every provider eagerly.
- Retrieval must retain provenance and explicit degraded/unavailable state.
  Never return demo, cache, or fixture values as live memories.
- Never persist credentials in provider state or expose secret values to the
  model. Use profile-safe `HERMES_HOME` paths.

Verify through targeted `tests/plugins/` and memory tests using the canonical
wrapper. Use fakes at the remote SDK boundary, but exercise real provider
discovery, config selection, lifecycle, and isolated state paths.
