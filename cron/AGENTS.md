# Cron scheduler guidance

Applies to scheduled job storage, parsing, execution, and delivery.

- Preserve supported schedule forms (duration/every phrase, five-field cron,
  and one-shot ISO timestamp) through the existing parser; do not introduce
  body-keyword routing.
- Keep the scheduler interrupt, catch-up, grace-window, file-lock, and duplicate
  prevention guarantees bounded and testable.
- Cron sessions default to `skip_memory=True` and remain separate from gateway
  conversations. Delivery must not append messages into the target session and
  break role alternation.
- A pre-run script's stdout is untrusted input. Preserve injection scanning,
  timeouts, workdir/profile isolation, and explicit `no_agent` behavior.
- Delivery adapters use the generic platform/plugin contract. Do not grow a
  hardcoded platform map when registration metadata can carry delivery support.
- Jobs, results, and locks use the active `HERMES_HOME`; tests use a temporary
  home. Do not silently discard a missed/failed delivery.

Verify with targeted scheduler/job tests:

```bash
scripts/run_tests.sh tests/cron/
```

For platform delivery, also run the owning gateway/plugin tests and prove the
job result lands in its separate cron session.
