# Nix guidance

Applies to flakes, packages, modules, dev shells, and desktop/TUI/web Nix
composition.

- Keep `flake.lock`, Python/Node inputs, package outputs, modules, and checks
  reproducible. Update the lock only through Nix tooling and review the input
  changes.
- Preserve separation between build-time derivations and runtime user state.
  Never bake credentials or a mutable real `HERMES_HOME` into a store path.
- Module options map to canonical Hermes config keys and profile-safe paths; do
  not create a second configuration vocabulary without migration.
- Keep Linux/macOS and supported architecture conditions explicit. A package
  that omits desktop/TUI/web assets must fail clearly or declare that variant.

Run the narrow `nix flake check`/package build that owns the change and report
any platform output not exercised locally.
