# Root runtime guidance

Load this document before changing root-level runtime files such as
`run_agent.py`, `cli.py`, `model_tools.py`, `toolsets.py`, `hermes_state.py`,
`hermes_constants.py`, `batch_runner.py`, or `trajectory_compressor.py`.

## Dependency direction

Keep the tool dependency chain one-way:

```text
tools/registry.py
  <- tools/*.py (self-register at import time)
  <- model_tools.py (discovers, selects, and dispatches tools)
  <- run_agent.py / cli.py / batch_runner.py / terminal environments
```

Do not make the registry import tool implementations or high-level agent/CLI
modules. A built-in tool is auto-discovered but is not exposed until its name is
wired into `toolsets.py`; follow `tools/AGENTS.md`.

## Agent loop

`run_agent.py:AIAgent.run_conversation()` is the synchronous conversation loop.
It owns interrupt handling, API/tool iteration budgets, the grace call, message
history, and final response assembly. Preserve these contracts:

- system prompt and past messages stay byte-stable except during compression;
- assistant tool calls are followed by their tool results without breaking role
  alternation;
- tool failures remain visible and never become fabricated success data;
- iteration and delegation budgets stay bounded and shared as designed;
- reasoning fields remain associated with the assistant message that produced
  them.

When changing prompt assembly, also inspect `agent/prompt_builder.py`. When
changing tool exposure or dispatch, inspect `model_tools.py`,
`tools/registry.py`, `toolsets.py`, and the owning platform preset together.

## Classic CLI and slash commands

`cli.py:HermesCLI` is the classic prompt-toolkit/Rich orchestrator. Slash-command
metadata is canonical in `hermes_cli/commands.py:COMMAND_REGISTRY`; CLI dispatch,
gateway help/dispatch, Telegram/Slack menus, autocomplete, and aliases derive
from it.

For a new command:

1. add one `CommandDef` to `COMMAND_REGISTRY`;
2. add the `HermesCLI.process_command()` handler in `cli.py`;
3. add gateway handling only if the command is available there; and
4. persist settings through the existing config helpers.

An alias belongs only in the existing `CommandDef`. Do not maintain parallel
alias/help/menu lists. Respect `cli_only`, `gateway_only`, and
`gateway_config_gate` semantics.

## State and profiles

`hermes_constants.py:get_hermes_home()` is the runtime state root and
`display_hermes_home()` is its presentation form. Profile selection is applied
in `hermes_cli/main.py` before runtime imports, so module-level constants may
cache the helper result after that point. Profile-management roots are
intentionally HOME-anchored so every active profile can enumerate siblings; do
not casually replace that exceptional path with the active `HERMES_HOME`.

Session persistence is SQLite-backed in `hermes_state.py`. Preserve migration,
search, and concurrency behavior with real temporary databases rather than
mocking the persistence boundary.

## Verification routes

- Agent/core Python: `scripts/run_tests.sh tests/agent/` plus the specific
  root-module tests selected by `rg`.
- CLI/commands/config: `scripts/run_tests.sh tests/cli/ tests/hermes_cli/`.
- Tools/toolsets: `scripts/run_tests.sh tests/tools/`.
- Session state: `scripts/run_tests.sh tests/hermes_state/`.

Name targeted files explicitly if a directory does not exist in the current
checkout. For changes spanning an actual surface, add its owning TUI, gateway,
desktop, or web verification rather than treating core unit tests as end-to-end
proof.
