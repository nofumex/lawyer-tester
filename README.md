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

## Automatic partner mailings

The mailing subsystem is safe-off. On the first start it records `mailings_implemented_at`, creates durable schedules, and stores `mailings_enabled=0`. Starting the process, applying the additive SQLite migration, or restarting it does not enable delivery. A deadline that is already past at first rollout—or passes while delivery is disabled—is moved to `overdue` and is never released by a normal enable.

Admin commands:

- `/mailings status` — enabled flag and queue counts.
- `/mailings enable CONFIRM` — quarantine deadlines that passed while disabled, then enable only natural future schedules.
- `/mailings disable` — stop real automatic delivery; deadlines continue to become `overdue`.
- `/mailings resolve JOB_ID delivered` or `/mailings resolve JOB_ID not-delivered` — explicitly reconcile an `uncertain` Bot API result; only the second form authorizes one retry.
- `/mailings backfill group1 100`, `/mailings backfill group2 100`, or `/mailings backfill all 100` — dry-run only; returns the candidate count, limit, and a one-use confirmation token.
- `/mailings backfill confirm TOKEN` — explicitly release only the previewed, limited backfill batch. This works independently of the global automatic-mailing flag.
- `/mailings test add` — allow only the current admin account to receive due test jobs while global delivery remains off. An explicit target is `/mailings test add PLATFORM USER_ID`.
- `/mailings test send 1 1` (or `2 1`, etc.) — enqueue an exact group/step message for the current test account.
- `/mailings test send 1 1 PLATFORM USER_ID` — enqueue it for one previously registered test ID.
- `/mailings test advance 30d` — advance that test account's pending/re-entry clocks without affecting anyone else.
- `/mailings test remove` — leave test mode.

Delivery jobs have unique `(platform, user_id, step)` identities. Successful stages schedule exactly one next stage from the actual send time. A Bot API result that cannot be proved is quarantined as `uncertain` and is not blindly resent after restart.

## A7 Consult partner program

The same callbacks and inline keyboards are used by Telegram and MAX. The agent menu includes new-client intake, referral links, program rules, offer, bonuses, profile, manager contact, and the main menu. Client submissions store the agent, client, source, platform and amoCRM deal relationship. Repeated updates/submissions are deduplicated by the durable incoming-update ledger and a unique submission key.

Configure `TELEGRAM_BOT_USERNAME`, `MAX_BOT_LINK`, `A7_OFFER_URL`, `A7_MANAGER_CONTACT_URL`, and the A7-only amoCRM variables shown in `.env.example`. No Sinai URLs, requisites, pipeline IDs, or credentials are embedded. An empty `AMOCRM_REFERRAL_STATUS_NAME` selects the first regular stage in `[A7] TG / Max - Боты`.

Bonus operations are intentionally explicit:

- `/bonus PLATFORM USER_ID AMOUNT LEAD_ID [comment]`
- `/bonus_paid BONUS_ID`

Manager messages are stored durably. An admin replies with `/reply MESSAGE_ID text`.
