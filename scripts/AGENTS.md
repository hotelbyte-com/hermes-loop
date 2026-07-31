# Script and CI helper guidance

Applies to installer, test, release, CI, and operational scripts.

- `scripts/run_tests.sh` plus `run_tests_parallel.py` is the canonical Python
  runner. Preserve clean-env execution, venv resolution, per-file process
  isolation, explicit skipped-suite behavior, timeouts, process-tree cleanup,
  and exit-code aggregation.
- Install/update/release scripts mutate user or published state. Resolve exact
  targets, preserve existing config/data, make partial failure recoverable, and
  never print credentials. Use dry-run/check modes where present.
- Keep shell scripts `set -euo pipefail` when compatible, quote paths, use
  script-relative/repository-relative roots, and avoid assuming GNU-only tools
  on macOS or POSIX utilities on native Windows.
- Version/dependency edits must remain synchronized with manifests, locks,
  packaging metadata, release assets, and CI. Do not update generated files by
  hand when the repository has a generator.
- CI helpers should produce deterministic, actionable output and preserve the
  failing command/status. Do not turn a failed check into warning-only success.

Run shell syntax/static checks already defined by CI, the script's targeted
tests under `tests/scripts/` or `tests/ci/`, and an isolated dry run for any
installer/release path.
