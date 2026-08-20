# Contribution and review guidance

Load this document for new capabilities, contribution review, issue/PR triage,
or changes that alter an extension boundary.

## What Hermes accepts

- Fix reproducible bugs on the current target branch. Point to the exact live
  call path and cover sibling paths that share the defect.
- Expand product reach at the edges: platform adapters, providers, models,
  desktop/TUI/dashboard features, and integrations are welcome when they use
  existing setup and configuration UX.
- Extract god-files such as `cli.py`, `run_agent.py`, and `gateway/run.py` into
  coherent modules when the extraction itself is the declared goal.
- Extend an existing interface instead of adding a parallel manager, registry,
  or hook. When three or more integrations in one category appear, prefer one
  generic ABC/orchestrator with the existing implementation as its first
  provider.
- Preserve prompt caching, message alternation, profile isolation, and behavior
  contracts. Preserve external authorship when salvaging contributor work.

## What Hermes rejects

- Speculative extension points without a real consumer.
- Non-secret behavior configured through a new `HERMES_*` environment variable.
- A core model tool when an existing tool, a CLI command plus skill, a plugin,
  or an MCP server can solve the problem.
- Pagination on instructional content that the agent must read completely
  (skills, prompts, playbooks).
- Security fixes that remove the feature's intended behavior.
- Outbound telemetry, usage attribution, or third-party identifiers without a
  generic opt-in gate and user-facing setup/config controls.
- Mid-conversation toolset or system-prompt mutation; dead code wired into a
  live path without real-path evidence; plugin-specific edits in core files.
- Change-detector tests that freeze catalogs, enumeration counts, or current
  config-version literals.

## Verify the premise before fixing

An apparent gap may be an intentional isolation or compatibility boundary.
Trace the current runtime and inspect the introducing change with
`git log -p -S '<symbol>'`. A valid defect report needs both:

1. a reproduction on the current target branch; and
2. a line-level explanation of why the changed path affects that reproduction.

Do not restore an apparently missing file/import/branch until you understand
what its absence protects. Do not revive an abandoned approach or enlarge a
task beyond the agreed slice without explicit scope.

## Capability footprint ladder

Choose the first rung that fully solves the requirement:

1. Extend existing code.
2. Add a CLI command plus a skill for shell/config/state workflows.
3. Add a prerequisite-gated structured tool with `check_fn`.
4. Add a plugin for third-party, niche, or user-specific behavior.
5. Build a cataloged MCP server for non-core structured tool use.
6. Add a core model tool only when it is broadly fundamental and cannot be
   reached through terminal/file/MCP paths.

Document the rejected higher rungs when choosing a core tool or a new generic
extension interface.

## Automated review and triage

The triage sweeper may close a contribution only with evidence for one of its
supported reasons:

- `implemented_on_main`: current main already implements the requested behavior;
- `cannot_reproduce`: the reported behavior is absent on a faithful current-main
  reproduction; or
- `incoherent`: the proposal cannot form a testable claim.

Design taste, product scope, or "we do not want this" decisions belong to a
human maintainer. Uncertainty is a reason to leave a contribution open, not to
invent a close reason.

Review changes for the repository-level blockers in `AGENTS.md`, then load the
closest subtree guidance. A green unit mock is not sufficient when the claim
depends on discovery, config propagation, security, a remote backend, or real
file/network I/O.

## Dependencies and supply chain

- PyPI: `>=floor,<next-major`; for pre-1.0 packages use a narrow compatible
  upper bound.
- Git dependencies and GitHub Actions: immutable commit SHA (with the action
  version documented in a comment).
- CI-only pip installs: exact version.
- Regenerate and review `uv.lock` after Python dependency edits.

Before squash-merging, bring the branch up to the current target using a safe
merge/rebase workflow and inspect the final diff for unrelated deletions. Do
not use a stale source snapshot to overwrite newer target-branch fixes.
