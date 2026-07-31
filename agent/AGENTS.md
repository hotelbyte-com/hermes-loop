# Agent internals guidance

Applies to `agent/`: prompt construction, display/progress, memory orchestration,
compression/caching, auxiliary models, curator, provider state, and internal
runtime helpers. For the root `AIAgent` loop, also read
[`../docs/agent-guidance/core-runtime.md`](../docs/agent-guidance/core-runtime.md).

## Conversation and prompt contracts

- Treat the per-conversation prompt prefix as immutable. Prompt builders may
  assemble a prefix at session start; they must not silently rebuild it on later
  turns. Skill/tool/memory changes default to the next session unless an
  explicit immediate-invalidating flow is requested.
- Preserve system/user/assistant/tool role alternation and attach reasoning to
  the assistant message that produced it. Do not inject synthetic user messages
  to steer an active loop.
- New agent behavior needs a real registration/discovery path, typed or
  structured inputs/outputs, bounded iterations, and a visible failure/gap
  path. Never market a capability that exists only in prompt prose or fixtures.
- Do not route intent, policy, tools, or UI semantics with ad hoc substring
  matching. Use existing parsed state, registries, schemas, or classifiers.
- Tool, memory, or auxiliary-model failure must not produce invented live data
  or an empty-success response. Preserve source/provenance through the result.

## Memory and curator

- Memory-provider implementations belong behind `MemoryProvider` in
  `agent/memory_provider.py` and orchestration in `agent/memory_manager.py`.
  Provider-specific behavior belongs in its plugin, not in the manager.
- Memory retrieval is evidence, not current truth. Keep source/session anchors
  and degrade explicitly when a provider is unavailable.
- Curator logic lives in `agent/curator.py`; backups live in
  `agent/curator_backup.py`; usage state belongs to `tools/skill_usage.py`.
  Automatic curator transitions apply only to skills with
  `created_by: "agent"` provenance.
- Curator never deletes: its strongest automatic action is archive. Pinned
  skills are excluded from automatic transitions and LLM review. Editing a
  pinned skill remains allowed; deleting it remains refused.

## Display and platform behavior

- Do not write ANSI erase-to-end-of-line (`\033[K`) in spinner/display paths;
  it leaks through prompt-toolkit `patch_stdout`. Clear stale characters with
  carriage return plus space padding.
- Keep platform-independent formatting in shared code and explicit
  platform-specific behavior in adapters or registered hints. Never make one
  platform's optional tool a hardcoded schema recommendation for all sessions.
- Logs must redact credentials and personal platform identifiers. Logging alone
  is not a defect fix.

## Verification

Use targeted files through the canonical wrapper, then the owning integration
surface when prompt or protocol behavior changes:

```bash
scripts/run_tests.sh tests/agent/
scripts/run_tests.sh tests/run_agent/
```

If the change affects memory plugins, delegation, TUI, gateway, or desktop,
also run the closest subtree's tests. Prove cache stability and failure behavior
with invariants rather than snapshots of current prompt text or catalog counts.
