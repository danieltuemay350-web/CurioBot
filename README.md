# Telegram Reading Bot MVP

A Python modular monolith for administrator-reviewed, attributed knowledge posts in a Telegram channel. It ingests configured RSS feeds and administrator-submitted article URLs, drafts concise posts with Gemini, and schedules approved drafts with durable delivery tracking.

**MVP controls:** every post requires administrator approval. AI-discovered sources stay quarantined until validation and administrator approval. Gemini uses one explicit configured model and a conservative daily request budget. Ambiguous Telegram sends are marked `unknown` and require manual reconciliation.

## Requirements

- Python 3.12 or later.
- Telegram bot token, administrator Telegram user IDs, and target channel identifier.
- Gemini API key and explicit model identifier. Model access/availability must be checked by the operator; the application does not select a fallback.
- A supported local SQLite filesystem.

The exact pinned dependency set is in `pyproject.toml`. It has not been installed or live-service verified in this environment. Versions and Python/platform compatibility should be checked when installing.

## Local setup

From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
mkdir -p data
cp .env.example .env
```

Edit `.env` and fill only the required values below. Keep `.env` private; it is ignored by Git. Do not configure a real channel until you are ready for live posting. The bot refuses to start when required credentials/target/model configuration is missing. It does not automatically run migrations.

Required values:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_IDS` (comma-separated positive numeric Telegram user IDs)
- `TELEGRAM_TARGET_CHAT_ID`
- `GEMINI_API_KEY`
- `GEMINI_MODEL` (explicit model; no fallback)

Then initialize the schema and start polling:

```sh
alembic upgrade head
python -m app.main
```

Configured RSS feeds start as quarantined source candidates. Use `/sources` to validate, approve, and activate each feed. Submit article webpages by sending an HTTP/HTTPS URL to the bot as an administrator.

## Environment variables

All supported variables, defaults, limits, and secret handling are listed in `.env.example`. Important controls include:

- `GEMINI_DAILY_REQUEST_BUDGET`: conservative daily request cap. Each attempted request is durably reserved/charged; a process crash may consume the reservation.
- `GEMINI_DAILY_TOKEN_BUDGET`: optional additional token ceiling. Set to `0` to disable token-based accounting and rely on the request cap. If enabled but provider usage is unavailable, generation pauses.
- `GEMINI_RESET_TIMEZONE`: budget-period timezone; defaults to UTC.
- `ARTICLE_TEXT_RETENTION_DAYS`: 7-day default, validated to 1–365 days.
- `PUBLICATION_DAILY_LIMIT`: hard local-day cap; default 5. Quality may yield fewer posts.
- `MAX_OVERDUE_AGE_HOURS`: default 24 hours; older approved posts are skipped.
- `CATCHUP_MIN_INTERVAL_MINUTES`: default 180 minutes between overdue catch-up posts.
- `HTTP_*`: fetch timeout, response-size, port, and redirect controls.
- `RSS_FEED_URLS`: comma-separated feed candidates, which still require administrator review and activation.

The schedule timezone defaults to `Africa/Addis_Ababa`; all persisted timestamps use ISO-8601 UTC.

## Telegram controls

Available commands include `/start`, `/help`, `/status`, `/pause`, `/resume`, `/stats`, `/sources`, `/queue`, `/settings`, `/addsource`, `/discover`, `/approve`, `/reject`, `/edit`, `/regenerate`, `/retry`, `/reconcile`, and `/quota_resume`. State-changing commands and callbacks check the administrator allowlist server-side.

`/approve ID` grants publication approval; after that the scheduler places the draft in the queue subject to time windows, topic variety, daily caps, and overdue rules. `/pause` stops polling/processing/publishing; `/resume` resumes them. `/quota_resume` clears an uncertainty pause only after an administrator verifies quota availability; the daily budget remains enforced.

For an `unknown` Telegram attempt, inspect the target channel and use `/reconcile ATTEMPT_ID found MESSAGE_ID` or `/reconcile ATTEMPT_ID absent`. An absent decision explicitly permits a later send that could still duplicate a message if the inspection was incomplete.

## Database operations

Apply schema changes explicitly:

```sh
alembic upgrade head
alembic current
```

The app refuses startup when the database is not at the current Alembic head. Use SQLite-safe backup and restore verification helpers:

```sh
python -m app.backup backup backups/bot-$(date -u +%Y%m%dT%H%M%SZ).sqlite3
python -m app.backup verify-restore backups/your-backup.sqlite3 /tmp/bot-restore-check.sqlite3
```

Restore verification writes only to a new path, checks SQLite integrity and the Alembic version table, and never overwrites the live database. Keep backups outside the live database directory with restricted permissions.

## Tests and checks

```sh
pytest -q
ruff check .
mypy app
alembic upgrade head
```

Tests use isolated SQLite databases, fake Gemini/Telegram clients, injected DNS, and mocked connection responses. Network-binding restrictions may prevent an actual local HTTP server in some sandboxes. Tests never send Telegram messages or use production Gemini credentials/quotas.

## Deployment

Initial use is local on Kali Linux. VPS and Docker deployment are later operational work. Before production, configure one supervised application instance, durable SQLite storage, restrictive permissions, secrets injection, outbound egress limits, log rotation, alerts, separate backups, and tested restoration. See [Operations](docs/OPERATIONS.md).

## Documentation

- [Product specification](docs/PRODUCT_SPEC.md)
- [Architecture and database](docs/ARCHITECTURE.md)
- [Security and threat model](docs/SECURITY.md)
- [Operations, setup, backup, and recovery](docs/OPERATIONS.md)
- [Coding-agent instructions](AGENTS.md)
