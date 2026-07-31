# Bootstrap installer guidance

This app is a Tauri installer: React/Vite in `src/` and Rust/native packaging in
`src-tauri/`.

- Preserve the same managed-install layout and profile-safe `HERMES_HOME`
  semantics as the CLI/desktop app. Never embed credentials or machine-specific
  absolute paths in the bundle.
- Keep renderer state presentation-only; privileged filesystem/process/network
  operations belong behind the Tauri command boundary with validated inputs.
- Treat download, extraction, executable selection, and update paths as security
  boundaries. Verify hashes/signatures where the existing flow does, prevent
  traversal, and clean partial installs without deleting user state.
- Maintain Linux/macOS/Windows behavior and manifests. Do not assume a POSIX
  shell or Unix path syntax in shared code.

Run from `apps/bootstrap-installer/`:

```bash
npm run typecheck
npm run build
npm run tauri:build:debug   # when native packaging changed
```

Use a temporary install root for manual verification.
