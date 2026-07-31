# Tooling guidance

Applies to built-in tools, tool discovery/dispatch, toolsets, terminal
environments, skill tools, and delegation. Read the footprint ladder in
[`../docs/agent-guidance/contribution-and-review.md`](../docs/agent-guidance/contribution-and-review.md)
before adding a tool.

## Tool registration and exposure

- Prefer a plugin for custom/local capability. Add a built-in tool only when it
  is fundamental to most users and terminal/file/skill/MCP paths cannot cover it.
- A built-in tool requires both:
  1. a `tools/<name>.py` module with top-level `registry.register(...)`; and
  2. explicit exposure through the appropriate list/toolset in root
     `toolsets.py`.
  Discovery registers a schema but does not expose it to an agent.
- Keep `tools/registry.py` dependency-light. Tool modules depend on it; it must
  not import the high-level runtime or individual tool modules.
- Co-locate schema, availability check, handler, and registration. Handlers
  return a JSON string and expose structured errors; do not return fabricated
  success when an executor or dependency is absent.
- Prerequisite-specific tools use `check_fn`/`requires_env` so their schema is
  absent when unavailable. Do not dynamically swap the active toolset during a
  conversation.

## Schema and state safety

- Schemas must describe only the tool itself. Do not hardcode references to a
  tool from another optional toolset; if contextual guidance is essential,
  inject it only after the available-tool set is known in `model_tools.py`.
- Runtime state uses `get_hermes_home()` and schema/display paths use
  `display_hermes_home()`. Tests use isolated `HERMES_HOME`; no tool test may
  touch the developer's real `~/.hermes`.
- Validate typed/structured arguments at the boundary. Dangerous mutations go
  through the existing approval policy; never infer approval from prose.
- Do not use error-string or user-text substring checks as product routing.
  Parse protocol fields or use typed/sentinel error categories.

## Agent-level tools and globals

Todo and memory tools are intercepted by `run_agent.py` before ordinary
`handle_function_call()` dispatch. Preserve that boundary when changing their
lifecycles.

`model_tools.py:_last_resolved_tool_names` is process-global. Delegated child
execution saves/restores it around `_run_single_child()`. Any new reader must
account for the child window and must not treat the value as durable session
state.

## Delegation

`tools/delegate_tool.py` supports a single task or a bounded parallel batch.
Leaf workers cannot recursively delegate or use privileged interaction tools;
orchestrators retain delegation only when enabled and within configured depth.
Preserve:

- `delegation.max_concurrent_children`, `max_spawn_depth`, child timeout, and
  iteration budgets;
- isolated child context/terminal state and explicit result provenance;
- visible partial failure instead of silently dropping a child result; and
- the fact that background delegation is process-local. Restart-durable work
  belongs in cron or a durable background process, not in-memory delegation.

## Skills tooling

Tools that load instructional content must return the complete required file;
do not add pagination that encourages reading only the first page. Skill source,
provenance, platform gates, and setup metadata must survive search/load/manage
operations. Do not expose secret values to the model.

## Verification

```bash
scripts/run_tests.sh tests/tools/
scripts/run_tests.sh tests/computer_use/    # terminal/computer backends when relevant
scripts/run_tests.sh tests/skills/          # skill discovery/manage changes
```

For discovery or platform-preset changes, exercise real imports with a temporary
`HERMES_HOME`; a mocked registration call is not end-to-end proof.
