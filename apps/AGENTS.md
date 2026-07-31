# Application workspace guidance

Applies to TypeScript applications under `apps/`. Install npm workspace
dependencies from the repository root; do not create per-app lockfiles unless
the package is intentionally standalone.

## TypeScript and React

- Prefer small feature-owned nanostores when state is shared, reused, persisted,
  or consumed at a distance. Rendering components use `useStore`; non-rendering
  actions may read `$atom.get()`.
- Keep route roots thin and compositional. Avoid prop drilling through several
  layers and avoid monolithic hooks; colocate narrow actions with the state they
  mutate.
- Prefer interfaces for public props and shared object shapes. Extend React
  primitives with `React.ComponentProps`, `Omit`, or `Pick` instead of copying
  their props.
- Use table-driven mappings for ids/routes/views. Make fire-and-forget async UI
  intent explicit with `void` and surface actionable failure state.
- Do not calculate backend-owned security, billing, model, or session truth in
  the renderer. Consume typed gateway/backend responses.

Load the nested app guide before editing its files:

- [`desktop/AGENTS.md`](desktop/AGENTS.md)
- [`bootstrap-installer/AGENTS.md`](bootstrap-installer/AGENTS.md)

Run the exact scripts declared in that app's `package.json`.
