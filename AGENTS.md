# Hermes Agent — repository guidance

This is the always-on entrypoint for work in this repository. Keep it short:
Codex concatenates guidance from the repository root down to the working
directory and stops at its configured byte limit (32 KiB by default). Read the
closest nested `AGENTS.md` for the subtree you edit; closer guidance supplements
or overrides this file.

## Product and architecture

Hermes runs one agent core through the classic CLI, messaging gateway, Ink TUI,
browser dashboard, Electron desktop app, ACP adapter, scheduled jobs, and
delegated workers. It learns through memory and skills. Product breadth belongs
at these edges; the core agent and model-tool schema remain a narrow waist.

Two invariants are non-negotiable:

- **Prompt caching:** keep the system-prompt prefix byte-stable for a
  conversation. Do not mutate past context, reload memory, rebuild the system
  prompt, or change toolsets mid-conversation. Context compression is the only
  normal exception. Cache-affecting commands default to next-session activation;
  an explicit `--now` path may invalidate immediately.
- **Conversation protocol:** preserve strict role alternation. Never append two
  consecutive messages with the same role or inject a synthetic user message
  in the middle of the agent loop.

## Working method

1. Search the repository, current tests, and `git log -p -S '<symbol>'` before
   changing behavior. Verify both the reported symptom and the original design
   intent; an omission or isolation boundary may be deliberate.
2. Reproduce on the current checkout and identify the exact live call path.
   Fix the bug class and sibling paths, not only the reported site.
3. Prefer the smallest existing extension point. Do not add speculative hooks,
   managers, registries, or dependencies without a concrete consumer.
4. Keep diffs scoped and reversible. Large mechanical extraction from a known
   god-file is acceptable when extraction is the declared task; feature work
   must not smuggle unrelated refactors.
5. Validate behavior through the real resolution/config/I/O path when that
   boundary matters. Mocks are not sufficient evidence for config propagation,
   security boundaries, remote backends, plugin discovery, or file/network I/O.
6. Report the changed behavior, proving commands and output, and remaining
   gaps. Do not claim runtime behavior from source inspection alone.

## Repository-wide guardrails

- New capability follows this footprint order: extend existing code → CLI
  command plus skill → prerequisite-gated tool (`check_fn`) → plugin → cataloged
  MCP server → new core model tool. A core tool is the last resort because its
  schema is sent on every model call.
- `.env` is for secrets only: API keys, tokens, and passwords. Timeouts, feature
  flags, paths, thresholds, and display behavior belong in `config.yaml`.
- All state paths use `get_hermes_home()`; user-facing paths use
  `display_hermes_home()`. Never hardcode `~/.hermes` or
  `Path.home() / '.hermes'` for runtime state. Profiles are intentionally
  isolated; do not add live inheritance between them.
- Never swallow failures into `{}`, `[]`, `None`, or a fake successful result.
  Return or surface a typed/structured gap or error. Demo and fixture data must
  never masquerade as live tool output.
- Do not add substring/keyword branching as a substitute for agent reasoning,
  intent routing, tool selection, or policy. Use typed state, parsed protocol
  fields, registries, schemas, or injected configuration.
- Plugins must not add plugin-specific branches to core files. Widen a generic
  plugin interface only when a real plugin requires it.
- New dependencies require a ceiling. PyPI packages use a bounded range, git
  dependencies and GitHub Actions use commit SHAs, and CI-only pip installs use
  exact versions. Regenerate `uv.lock` with `uv lock` after Python dependency
  changes.
- Preserve cross-platform behavior (Linux, macOS, Windows, WSL, and supported
  Termux paths). Use `pathlib`, explicit UTF-8 text I/O, and platform-aware
  process handling; do not assume POSIX commands exist on native Windows.
- Never add outbound analytics, attribution tags, or third-party identifiers
  without a user-facing opt-in gate and setup/config controls.
- Preserve contributor authorship when integrating external work. Before a
  squash merge, update from the current target branch and inspect the final
  diff for unrelated reversions.

## Setup and verification

Python requires 3.11–3.13. Prefer `.venv`, with `venv` as a local fallback.

```bash
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[all,dev]"
```

Use the canonical Python test wrapper; do not call `pytest` directly for a
claimed verification run:

```bash
scripts/run_tests.sh tests/path/test_file.py
scripts/run_tests.sh tests/path/ -- -v --tb=long
scripts/run_tests.sh                 # full non-integration suite
```

The wrapper cleans credentials and runtime variables, fixes timezone/locale,
isolates `HERMES_HOME`, and runs each test file in a fresh process. Name a
target under `tests/e2e`, `tests/integration`, or `tests/docker` explicitly when
you intend to run those normally skipped suites.

For TypeScript/React work, install the root npm workspace once (`npm install`)
and run the scripts declared by the nearest package. The standalone `loop/`
workspace uses pnpm instead.

Use the narrowest proving checks first, then the owning package's typecheck,
lint, tests, and build as appropriate. Full-suite verification is expected
before release/push when feasible; state any external-service or platform gap.

## Load the closest guidance

Read these files **before** editing the matching scope. The table is also the
maintenance index for this layered guidance; do not move a rule into an
undiscoverable document without linking it here or from a nested entrypoint.

| Scope | Required guidance |
|---|---|
| Contribution design, triage, review, new capability | [`docs/agent-guidance/contribution-and-review.md`](docs/agent-guidance/contribution-and-review.md) |
| Root runtime files (`run_agent.py`, `cli.py`, `model_tools.py`, `toolsets.py`, `hermes_state.py`, `hermes_constants.py`) | [`docs/agent-guidance/core-runtime.md`](docs/agent-guidance/core-runtime.md) |
| Agent internals, prompt building, memory, curator | [`agent/AGENTS.md`](agent/AGENTS.md) |
| CLI subcommands, setup, config, skins, dashboard backend | [`hermes_cli/AGENTS.md`](hermes_cli/AGENTS.md) |
| Model tools, registry, environments, delegation | [`tools/AGENTS.md`](tools/AGENTS.md) |
| Messaging gateway and platform adapters | [`gateway/AGENTS.md`](gateway/AGENTS.md) |
| Python JSON-RPC backend for TUI/desktop | [`tui_gateway/AGENTS.md`](tui_gateway/AGENTS.md) |
| Ink terminal UI | [`ui-tui/AGENTS.md`](ui-tui/AGENTS.md) |
| Browser dashboard SPA | [`web/AGENTS.md`](web/AGENTS.md) |
| Desktop/bootstrap/shared apps | [`apps/AGENTS.md`](apps/AGENTS.md), then the nested app guide |
| Plugins | [`plugins/AGENTS.md`](plugins/AGENTS.md), then any nested provider guide |
| Bundled and optional skills | [`skills/AGENTS.md`](skills/AGENTS.md) or [`optional-skills/AGENTS.md`](optional-skills/AGENTS.md) |
| Optional MCP catalog | [`optional-mcps/AGENTS.md`](optional-mcps/AGENTS.md) |
| Cron scheduler | [`cron/AGENTS.md`](cron/AGENTS.md) |
| Loop control-plane product | [`loop/AGENTS.md`](loop/AGENTS.md), then `server/` or `web/` guidance |
| Tests and fixtures | [`tests/AGENTS.md`](tests/AGENTS.md) |
| Install/release/CI scripts and containers | [`scripts/AGENTS.md`](scripts/AGENTS.md), [`docker/AGENTS.md`](docker/AGENTS.md) |
| ACP integration | [`acp_adapter/AGENTS.md`](acp_adapter/AGENTS.md) |
| ACP registry metadata | [`acp_registry/AGENTS.md`](acp_registry/AGENTS.md) |
| Docusaurus site | [`website/AGENTS.md`](website/AGENTS.md) |
| Packaging, Nix, and translations | [`packaging/AGENTS.md`](packaging/AGENTS.md), [`nix/AGENTS.md`](nix/AGENTS.md), [`locales/AGENTS.md`](locales/AGENTS.md) |

## Code review rules

- Reject cache-breaking prompt/toolset mutation, core-tool growth without a
  footprint analysis, plugin-specific core branches, profile-unsafe paths,
  secret/config confusion, fake-success fallbacks, and change-detector tests.
- Tests should assert relationships and invariants, not snapshots of model
  names, config versions, provider counts, or other intentionally changing
  catalogs.
- Automated triage may close only when evidence proves `implemented_on_main`,
  `cannot_reproduce`, or `incoherent`. Taste or scope decisions stay with a
  human maintainer; when uncertain, leave the contribution open.
- A mitigation must preserve the feature it secures. Read the original intent
  and prove the protected feature still works end to end.

## Commits

Follow the Conventional Commit style documented in `CONTRIBUTING.md` (for
example `fix(gateway): ...` or `docs(agent): ...`). Commit and push completed,
reversible work on a feature branch. Never rewrite or force-push shared history
unless the task explicitly authorizes it.
