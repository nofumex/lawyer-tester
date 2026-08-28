# Lawyer tester bot

Python 3.11+ Telegram/MAX bot with SQLite persistence and amoCRM notes/stage actions.

1. Copy `.env.example` to `.env` and set the transport token, `ADMIN_IDS`, and amoCRM credentials.
2. Run `python -m unittest discover -v`, then `python main.py`.
3. Send `/start`; send `/admin` from an ID in `ADMIN_IDS`.

Questions, choices, attempts, answers and the durable `platform + user_id -> attempt -> amo_lead_id` binding are stored in SQLite. The bundled seed contains only the questions present in the supplied brief; add the remaining approved questionnaire through the admin command API before production.
