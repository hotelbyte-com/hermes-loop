# Localization guidance

Applies to locale catalogs under `locales/`.

- Keep message keys aligned across supported locales and with the source lookup.
  Add a fallback intentionally; do not silently drop a key from one language.
- Preserve interpolation placeholders, plural/select forms, Markdown/code spans,
  slash commands, config keys, and product names exactly where they are
  functional tokens.
- Translate user-facing meaning, not logs/protocol fields that machines parse.
  Never localize a structured error code or routing identifier.
- Use UTF-8 and retain contributor-language review for substantive copy. Do not
  claim translation quality from machine generation alone.

Run the existing locale/catalog consistency tests found under `tests/` and
exercise the affected UI/CLI surface when layout or interpolation can change.
