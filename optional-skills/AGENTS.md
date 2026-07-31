# Optional skill guidance

Optional skills are official but inactive by default because they are niche,
heavyweight, platform-specific, or depend on paid/external services.

Before editing, load the complete bundled-skill standards in
[`../skills/AGENTS.md`](../skills/AGENTS.md); they apply here unchanged.

- Keep optional install/discovery paths compatible with
  `hermes skills install official/<category>/<skill>`.
- Put only capability-specific dependencies and setup in the skill directory;
  do not activate the skill or add its tools to the default core surface.
- Platform and tool availability gates must reflect real imports/scripts.
- Tests still live under `tests/skills/` and run through
  `scripts/run_tests.sh`; no live network or developer credentials.
