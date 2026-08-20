# Bundled skill authoring guidance

Applies to built-in skills shipped and active by default. Optional/niche/heavy
skills belong in `optional-skills/`; community-specific skills generally belong
in a registry rather than this repository.

## Structure and metadata

- Each skill has `SKILL.md` with `name`, a one-sentence `description` of at most
  60 characters ending in a period, version/author/license as applicable, and
  correct platform/config metadata.
- No marketing adjectives or repeated skill name in the description. Credit
  the human contributor first; the agent/tool may be a secondary collaborator.
- Use the modern body order: title, short scope intro, `When to Use`,
  `Prerequisites`, `How to Run`, `Quick Reference`, `Procedure`, `Pitfalls`,
  `Verification`. Keep simple skills near 100 lines and complex ones near 200;
  remove duplicated setup prose.
- Put executable helpers in `scripts/`, durable details in `references/`, and
  reusable artifacts in `templates/`. Reference them relative to the skill
  directory; do not ask the model to recreate a parser or walker every run.

## Tool and platform contracts

- SKILL.md interaction prose names native Hermes tools (for example `terminal`,
  `read_file`, `patch`, `search_files`, `web_extract`, `browser_navigate`,
  `delegate_task`) or a specifically required MCP server. Frame third-party
  CLIs as commands invoked through `terminal`, not as substitute model tools.
- Do not instruct the model to use shell utilities where a native tool is the
  interaction surface. Complex shell pipelines may live inside a tested helper
  script.
- Audit `platforms:` against real implementation primitives. Prefer portable
  `pathlib`, `tempfile`, and Python APIs; gate macOS/Linux/Windows only when a
  real dependency requires it.
- Conditional activation (`fallback_for_*`, `requires_*`) describes actual tool
  availability. Do not hide a skill because a credential is missing when the
  load-time setup flow is designed to collect or skip it.
- Required secrets use secure setup metadata. The model receives setup status,
  never the secret value.

## Safety and quality

- Instructional content must be read completely; do not design lazy pagination
  or page-one-only flows.
- Do not add hardcoded demo results that can enter normal execution. A missing
  source/tool becomes an explicit limitation or failed verification.
- `.env.example` changes stay in a clearly delimited skill-owned block and
  contain commented placeholders only.
- Prefer existing dependencies and native tools. New dependencies follow the
  repository bounds and must be justified by the skill's real procedure.

## Tests and verification

Tests live at `tests/skills/test_<skill>_skill.py`, use stdlib + pytest +
`unittest.mock`, and make no live network calls. Use `tmp_path`/`monkeypatch` for
filesystem and environment behavior.

```bash
scripts/run_tests.sh tests/skills/test_<skill>_skill.py
```

Also run the skill's documented verification through the actual Hermes tool
surface when feasible. A test that only parses frontmatter is not proof that the
procedure works.
