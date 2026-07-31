# Container guidance

Applies to Dockerfiles, Compose files, entrypoint/shim scripts, and s6 services.

- Preserve user/group permissions, active-profile reconciliation, signal
  forwarding, process supervision, and writable-volume ownership. Do not solve
  a permission problem by running the whole agent permanently as root.
- Container state uses the configured `HERMES_HOME` volume. Image rebuilds and
  startup must not overwrite user config, profiles, sessions, skills, or keys.
- Keep secrets in runtime injection/secret stores, never image layers, build
  args, committed compose values, or logs.
- Entry/shim scripts validate and quote commands, preserve exit codes, and avoid
  shell injection. Cleanup must target exact container-owned paths.
- Pin base/dependency/action versions per repository supply-chain policy and
  preserve multi-architecture behavior.

The normal wrapper skips Docker tests. Opt in explicitly:

```bash
scripts/run_tests.sh tests/docker/
```

When required, build the exact image/compose service in an isolated temporary
volume and verify startup, shutdown, permissions, profiles, and persistence.
