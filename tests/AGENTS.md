# Test guidance

Applies to all Python tests and fixtures under `tests/`.

## Canonical runner

Use `scripts/run_tests.sh`; a claimed verification run must not call `pytest`
directly. The wrapper activates `.venv`/`venv`, starts with a clean environment,
removes credential/runtime leakage, fixes UTC and C.UTF-8, and invokes
`scripts/run_tests_parallel.py`.

The current isolation unit is **one fresh Python process per test file**. It is
not xdist and not one process per individual test. Cross-file module state
cannot leak; ordering/state inside one file is the test author's responsibility.

```bash
scripts/run_tests.sh tests/agent/test_example.py
scripts/run_tests.sh tests/gateway/ -- -v --tb=long
scripts/run_tests.sh -j 4 tests/tools/
```

The default full run skips `tests/e2e`, `tests/integration`, and `tests/docker`;
explicitly naming one of those paths opts into it. Do not claim those suites ran
from an unqualified full-wrapper invocation.

## Hermetic state

- The autouse fixtures remove credential-shaped variables and point
  `HERMES_HOME` at a per-test temporary directory. Never weaken this isolation
  or write to the developer's real `~/.hermes`.
- Do not redirect HOME globally in general tests; that has broken subprocesses.
  Profile tests are the exception: mock `Path.home()` and set `HERMES_HOME`
  together so the profile root and active state both stay under `tmp_path`.
- Explicitly clean subprocesses, servers, ports, files, tasks, context variables,
  and global registries created within the same test file.
- No live network or developer API keys in unit tests. Dedicated integration/E2E
  tests must be marked/routed and fail clearly when their prerequisite is absent.

## What to assert

- Prefer behavior and relationships: registration leads to discovery; every
  catalog item has required metadata; authorization rejects a foreign scope;
  one claim wins; failure remains visible.
- Do not write change-detector snapshots of current model names, provider/skill
  counts, config-version literals, or full catalogs that intentionally evolve.
- For discovery/config/security/file/network paths, exercise real imports and
  temporary I/O. Mock only the external boundary, not the function whose wiring
  the test claims to prove.
- Regressions should fail before the fix and pass after it. Never loosen ordering,
  timing, observability, or a security invariant merely to make the suite green.
