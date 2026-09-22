# Lawyer tester bot

Python 3.11+ Telegram/MAX bot with SQLite persistence and amoCRM notes/stage actions.

1. Copy `.env.example` to `.env` and set the transport token, `ADMIN_IDS`, and amoCRM credentials.
2. Run `python -m unittest discover -v`, then `python main.py`.
3. Send `/start`; send `/admin` from an ID in `ADMIN_IDS`.

Questions, choices, attempts, answers and the durable `platform + user_id -> attempt -> amo_lead_id` binding are stored in SQLite. The bundled seed contains only the questions present in the supplied brief; add the remaining approved questionnaire through the admin command API before production.

Runtime delivery:

- Polling atomically stores each page in SQLite `update_queue` with the provider cursor. Pending updates and prepared responses resume after restart, even if the provider has acknowledged the page.
- Each platform has up to 16 concurrent user workers; a user has only one outstanding task. Later actions wait for successful delivery of the preceding response. Failures retry after 3 seconds without reapplying saved candidate state.
- Local state, deduplication and the response plan commit in one SQLite transaction. Transport and CRM network calls never hold that transaction. The existing `processing_lock` argument is retained for compatibility but is no longer used.
- Snapshots run every 60 seconds in a separate worker. Deletions and notification-only callback acknowledgements use a bounded best-effort queue (2 workers, 1000 operations). MAX callback responses that change the screen remain in ordered delivery.
- WARNING latency logs over 200 ms identify `poll.persist`, `update.queue`, `update.local`, `update.response`, `transport.<method>`, `snapshots` and `cleanup`. They contain identifiers, not candidate answers or API tokens.

Operate one bot process per database/token. Back up SQLite before upgrading; the queue table is created automatically. Drain pending `update_queue` rows before rolling back to older code, which cannot replay this queue. Shutdown waits for active UI/snapshot/CRM work; cleanup is disposable. Sixteen simultaneously slow users can exhaust a platform's delivery pool, so monitor queue latency. External message delivery is at-least-once: an API success followed by a lost response or process crash can repeat a screen, but will not apply the candidate answer twice. Existing CRM reconciliation/retry is retained; ambiguous lead creation still has the provider's existing idempotency limitations. Validate both bots on staging before production; automated tests use fake APIs.
