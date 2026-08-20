# Provider registry guidance

`providers/` is the generic model-provider registry and base contract. Bundled
profiles live under `plugins/model-providers/`; read that nested guide too.

- Keep discovery lazy and idempotent. Scan bundled profiles, active-user
  profiles, then legacy modules in the documented precedence; user registration
  may override bundled registration.
- Keep provider-specific conditionals in `ProviderProfile` hooks, not in registry
  lookup. Do not infer behavior from provider-name or URL substrings.
- Return explicit unsupported/auth/transport errors. Fallback may try another
  configured provider, but must preserve the failure evidence and never invent a
  successful response.
- Model catalogs and metadata tests assert relationships/invariants, not current
  names or counts.

Run targeted `tests/providers/` and `tests/plugins/` files with the canonical
test wrapper.
