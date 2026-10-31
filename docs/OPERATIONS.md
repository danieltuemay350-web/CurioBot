# Operations Guide

This guide covers local MVP operation. VPS/Docker deployment remains future work.

## Environment setup

The proposed pinned dependencies are declared in `pyproject.toml`. Use Python 3.12+ and the local virtual environment described in the README. Version compatibility has not been verified in every OS/Python combination. Do not install into Kali's externally managed system Python.

1. Create a virtual environment and install with `python -m pip install -e '.[dev]'`.
2. Create the `data/` directory and copy `.env.example` to an ignored `.env`.
3. Configure required credentials and limits. Never put real tokens in shell history, logs, or source control.
4. Run `alembic upgrade head` explicitly, then `alembic current`.
5. Start the app with `python -m app.main`. It checks that migrations are current and refuses startup if required credentials, model, or target are absent.
6. From an authorized administrator account, validate/approve/activate configured feeds through `/sources`; review URL submissions and drafts through `/queue`.

## Configuration reference

| Variable | Required | Default / rule |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | yes | Secret; never log it |
| `TELEGRAM_ADMIN_IDS` | yes | Comma-separated positive Telegram user IDs |
| `TELEGRAM_TARGET_CHAT_ID` | yes | Explicit destination; no default |
| `GEMINI_API_KEY` | yes | Secret; never log it |
| `GEMINI_MODEL` | yes | Explicit configured model; no fallback or availability claim |
| `DATABASE_URL` | no | `sqlite:///./data/bot.sqlite3`; SQLite file only for current backup helper |
| `LOG_LEVEL` | no | `INFO` |
| `TIMEZONE` | no | `Africa/Addis_Ababa` |
| `GEMINI_DAILY_REQUEST_BUDGET` | no | 10 requests; integer 1–10000 |
| `GEMINI_DAILY_TOKEN_BUDGET` | no | 0 disables; if enabled and usage is unavailable, generation pauses |
| `GEMINI_RESET_TIMEZONE` | no | `UTC` |
| `ARTICLE_TEXT_RETENTION_DAYS` | no | 7; integer 1–365 |
| `RSS_POLL_INTERVAL_MINUTES` | no | 30; integer 5–1440 |
| `PROCESS_INTERVAL_MINUTES` | no | 5; integer 1–1440 |
| `PUBLICATION_DAILY_LIMIT` | no | 5; integer 1–20 |
| `PUBLICATION_WINDOW_START` / `PUBLICATION_WINDOW_END` | no | `08:00` / `21:00`, 24-hour `HH:MM`, must differ |
| `MAX_OVERDUE_AGE_HOURS` | no | 24; integer 1–168 |
| `CATCHUP_MIN_INTERVAL_MINUTES` | no | 180; integer 30–1440 |
| `PUBLISH_RETRY_LIMIT` | no | 3 definite attempts; integer 1–10 |
| `RETRY_BASE_SECONDS` | no | 60; exponential backoff base |
| `HTTP_CONNECT_TIMEOUT_SECONDS` | no | 5; >0 and ≤30 |
| `HTTP_READ_TIMEOUT_SECONDS` | no | 10; >0 and ≤60 |
| `HTTP_MAX_RESPONSE_BYTES` | no | 2,000,000; 1 KiB–20 MiB |
| `HTTP_MAX_REDIRECTS` | no | 3; 0–10 |
| `HTTP_ALLOWED_PORTS` | no | `80,443`; comma-separated ports |
| `RSS_FEED_URLS` | no | Comma-separated; candidates remain quarantined until review |

The Gemini request budget reserves a request durably before the SDK call. Failed and interrupted calls consume the reservation conservatively. If a token cap is configured, the app reserves a conservative input/output token estimate before a request, sets a bounded output limit, and records provider-reported usage; generation pauses if usage reaches the cap or cannot be measured. When token usage is uncertain, that budget period is marked `usage_uncertain`, so `/quota_resume` cannot authorize another token-budgeted request until the next period. Usage rows are keyed by provider/model and the configured budget-period date, so restart does not reset the current period. A request-cap exhaustion blocks future requests until the next period. No paid fallback exists.

## Runtime behavior and logs

Run exactly one scheduler/publisher process. The app takes a non-blocking OS lock at `<DATABASE_URL file>.lock` and refuses to start a second instance using that database. APScheduler triggers periodic checks; SQLite stores source, article, draft, queue, attempt, publication, audit, and quota state. Persisted timestamps are ISO-8601 UTC strings. The schedule and user-facing queue times use the configured timezone (default `Africa/Addis_Ababa`).

Logs use standard Python logging. External exception messages and credentials should not be logged. Alerts go to configured administrator IDs for source failures, article-processing failures, Gemini pauses, unknown Telegram outcomes, startup reconciliation, and overdue skips. `/status`, `/stats`, `/sources`, `/queue`, and `/settings` expose operational state.

## Source and processing procedures

- Configured RSS URLs are inserted as quarantined candidates at startup. In `/sources`, validate each candidate (safe bounded fetch and parser/content-type check), approve it, then activate it. Disabling stops future polls.
- `/discover topic` asks Gemini for suggestions, charges the same request budget, and stores suggestions only as quarantined candidates. AI suggestions are not verified; inspect them before the safe validation and approval flow.
- Send an article URL to the bot as an administrator to submit it. Unsafe schemes, credentials, ports, non-public IPs, unsafe redirects, oversized responses, unsupported types, or fetch failures are rejected.
- Article content extraction and draft generation run on the bounded scheduler. A draft appears in `/queue` for preview, edit, regeneration, approve, or reject.
- Retryable ingestion or generation failures appear in `/queue` and `/status` with their preserved reason and retry count. An administrator may request up to three retries with `/retry ARTICLE_ID`; each retry is explicit and audited. Permanent extraction rejections and already-published articles cannot be retried. This does not bypass source/URL validation or draft approval.
- Content text is removed after its configured expiry (seven days by default); source metadata, drafts, publication history, and audit records remain by default.

## Pausing and quotas

`/pause` pauses polling, processing, and publication without erasing queued work. `/resume` resumes those workflows, still subject to normal gates. If Gemini budget is reached, generation pauses naturally for the current period. If the SDK fails or usage is uncertain, inspect quota/configuration and only then run `/quota_resume`; the budget cap still applies.

## Telegram delivery failure handling

A publication attempt is committed before calling Telegram. Confirmed success persists the message ID and timestamp. Definite Telegram rejections become `failed` and may retry within the bounded attempt limit and exponential backoff. Transport errors and other ambiguous outcomes become `unknown`, trigger an administrator notification, and are never automatically retried.

For unknown attempts:

1. Find the attempt ID in `/queue` and inspect the target channel around the attempt time.
2. If present, run `/reconcile ATTEMPT_ID found MESSAGE_ID` to record confirmed delivery.
3. If absent, run `/reconcile ATTEMPT_ID absent`. This preserves the original unknown attempt and allows one explicit future attempt; if inspection was incomplete, a duplicate remains possible.
4. Keep the audit record and investigate repeated network ambiguity. Exactly-once delivery cannot be guaranteed.

After restart, any `sending` attempt is conservatively changed to `unknown`; startup sends an alert. It must be reconciled manually.

## Downtime recovery and pacing

Approved drafts are assigned UTC due times within the publication window. Scheduling respects the local-day publication cap, 180-minute default spacing, and 24-hour default maximum age. Once overdue, only one due item is considered per scheduler cycle; catch-up sends must also respect the configured minimum interval since the prior publication. Items beyond maximum age are cancelled and reported. No burst is emitted after downtime. Quality may leave days below the 3–5 target or empty.

## Retention

The hourly cleanup removes only expired `article_text` rows. It does not delete articles, drafts, publications, or audit events. Verify retention with isolated tests before changing the period. Invalid retention values prevent configuration startup.

## Migrations

Stop the application before applying migrations. Do not auto-migrate at runtime. Back up first, inspect the Alembic revision, apply `alembic upgrade head`, then verify with `alembic current`. Never delete/recreate the database as a recovery shortcut. Fresh database migration and migration against retained data must both be tested for future schema changes.

## Backup and restore verification

Use SQLite's online backup API via the included helper:

```sh
python -m app.backup backup /secure/backups/bot-$(date -u +%Y%m%dT%H%M%SZ).sqlite3
python -m app.backup verify-restore /secure/backups/bot-file.sqlite3 /tmp/bot-restore-check.sqlite3
```

The backup destination must be new and separate from the live file. The restore command also requires a new path; it checks SQLite integrity and presence of the migration version table and never overwrites the live database. Protect files with restrictive permissions, store backups separately, define retention/encryption and RPO/RTO before production, and periodically record successful restore checks. The helper does not perform automatic backups or verify application-level row counts.

## Tests and deployment

Run `pytest -q`; optional available checks are `ruff check .` and `mypy app`. Tests mock Telegram/Gemini and use a controlled local HTTP server when sandbox permissions allow it; current restricted environments may require mocked transport behavior. No test should publish to a real channel, consume production Gemini quota, or fetch arbitrary internet URLs.

VPS deployment and Docker are later work. Before production, document a pinned deployment image, single-process supervisor, durable SQLite permissions, secret injection, network egress controls, monitoring, log rotation, and tested separate backups. SSRF controls in the application are not a replacement for network egress restrictions.
