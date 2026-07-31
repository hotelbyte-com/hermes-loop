# Hermes CLI and dashboard-backend guidance

Applies to `hermes_cli/`: subcommands, setup/config, skins, plugin loading,
profile selection, and the Python dashboard backend. Root `cli.py` guidance is
in [`../docs/agent-guidance/core-runtime.md`](../docs/agent-guidance/core-runtime.md).

## Commands

- `hermes_cli/commands.py:COMMAND_REGISTRY` is canonical for command names,
  aliases, help, gateway availability, Telegram/Slack menus, and autocomplete.
  Add metadata once; do not maintain parallel lists.
- A new command needs its classic CLI handler in `cli.py` and a gateway handler
  only when gateway-visible. Respect `cli_only`, `gateway_only`, and
  `gateway_config_gate`.
- Persist behavior with existing config helpers. Do not write configuration by
  string surgery or silently mutate an active conversation's prompt/tool state.

## Configuration

- User behavior belongs in `~/.hermes/config.yaml`; credentials belong in
  `~/.hermes/.env`. Add secret metadata to `OPTIONAL_ENV_VARS` only for actual
  keys/tokens/passwords. Do not expose a new non-secret `HERMES_*` setting.
- `hermes_cli/config.py:DEFAULT_CONFIG` is canonical for setup/subcommands.
  Adding a normal key is deep-merged and does **not** require a
  `_config_version` bump. Bump only for an active migration such as a rename or
  structural transform.
- Know the three load paths: `cli.py:load_cli_config()` for the classic CLI,
  `hermes_cli/config.py:load_config()` for setup/subcommands, and gateway raw
  YAML plus `gateway/config.py` for messaging. Verify all consumers that need a
  new key.
- CLI working directory is the process cwd. Messaging working directory is
  `terminal.cwd`; `TERMINAL_CWD` is an internal bridge, not the user-facing
  configuration path. `MESSAGING_CWD` is removed.

## Profiles and state

- `hermes_cli/main.py:_apply_profile_override()` must run before imports that
  cache state paths. Runtime state uses `get_hermes_home()`; displayed paths use
  `display_hermes_home()`.
- Profile enumeration is intentionally anchored at the real HOME so an active
  profile can see sibling profiles. Preserve that documented exception.
- Do not add live config inheritance across profiles; `--clone` is the explicit
  copy-at-creation mechanism.

## CLI UI and skins

- New interactive menus use `hermes_cli/curses_ui.py`; do not add
  `simple_term_menu` call sites. Existing use is legacy fallback only.
- Skins are data through `SkinConfig`/`_BUILTIN_SKINS` in
  `hermes_cli/skin_engine.py`. Missing values inherit from `default`; user YAML
  under the active `HERMES_HOME/skins` overrides built-ins.
- Use `display_hermes_home()` in schema descriptions and messages. Never print
  a hardcoded default-home path for profile-specific state.

## Dashboard backend

- `hermes_cli/web_server.py` serves REST/WebSocket APIs and the built bundle in
  `hermes_cli/web_dist/`. Source UI changes live in `web/`; read
  [`../web/AGENTS.md`](../web/AGENTS.md).
- `/api/pty` is a platform-aware PTY bridge. POSIX uses `ptyprocess`; native
  Windows uses its dedicated bridge. Preserve session-ticket/token auth rules,
  resize framing, process-tree cleanup, and platform guards.
- In gated/OAuth mode, the legacy ephemeral session token must not bypass the
  stronger WebSocket authorization path.

## Plugins

General plugin discovery is idempotent but historically triggered by importing
`model_tools.py`. A path that reads plugin state before that import must call the
existing discovery entrypoint explicitly. Model-provider plugins have a
separate lazy registry; do not double-import them through the general manager.

## Verification

```bash
scripts/run_tests.sh tests/cli/ tests/hermes_cli/
scripts/run_tests.sh tests/website/   # dashboard/backend contract tests when relevant
```

Use the paths that exist for the changed surface, plus `npm run build` in
`web/` when the served bundle or browser contract changes.
