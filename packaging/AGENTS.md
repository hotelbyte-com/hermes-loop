# Packaging guidance

Applies to Homebrew and other distribution metadata under `packaging/`.

- Package the same version, entrypoints, assets, manifests, and dependency bounds
  as `pyproject.toml`/release automation. Do not hand-edit a generated formula
  when a release script owns it.
- Downloads use immutable versions and verified checksums. Never publish a
  placeholder checksum or a URL that can change underneath a release.
- Preserve user config, profiles, sessions, skills, and credentials on upgrade
  and uninstall unless an explicit purge action is selected.
- Keep platform/architecture conditions exact and test installation in an
  isolated prefix without relying on the developer's existing Hermes install.

Run the owning release/package checks and a formula/package audit before
claiming distribution readiness.
