# Plugin guidance

Applies to repo-shipped plugins. Plugins extend generic contracts; they do not
own core runtime branches.

## General contract

- A general plugin has a manifest (`plugin.yaml`/`plugin.yml`) and a
  `register(ctx)` entrypoint. Use `ctx` registration methods for hooks, tools,
  CLI commands, platforms, or other declared surfaces.
- Plugin-specific behavior stays in its directory. If a real plugin exposes a
  missing generic need, extend the reusable plugin contract and make the plugin
  its first consumer; do not add `if plugin_name == ...` in `run_agent.py`,
  `cli.py`, `gateway/run.py`, or `hermes_cli/main.py`.
- Hooks are lifecycle boundaries, not an excuse for hidden global mutation.
  Keep inputs/outputs structured, failure visible, and cleanup idempotent.
- Manifests are package data. Verify a built wheel/archive still includes the
  manifest; source-tree discovery alone is not packaging proof.
- User plugins may override bundled implementations through documented
  last-writer or search precedence. Preserve that behavior and active-profile
  `HERMES_HOME` discovery.

## Security and configuration

- Secrets are declared as credential metadata and stored through existing
  secure setup paths. Manifests, logs, fixtures, and dashboard assets must not
  contain live keys.
- Behavioral settings belong in plugin/config YAML, not a new public env var.
- Optional dependency absence should disable or degrade the plugin explicitly,
  never fabricate successful output or crash unrelated plugins.
- Plugin dashboards must use their registered API/auth boundary and must not
  infer authorization client-side.

Read the closer guide for specialized plugin families:

- [`memory/AGENTS.md`](memory/AGENTS.md)
- [`model-providers/AGENTS.md`](model-providers/AGENTS.md)
- [`platforms/AGENTS.md`](platforms/AGENTS.md)
- [`kanban/AGENTS.md`](kanban/AGENTS.md)

Run the closest `tests/plugins/` target with `scripts/run_tests.sh` and add the
owning surface's tests for gateway, provider, memory, or dashboard behavior.
