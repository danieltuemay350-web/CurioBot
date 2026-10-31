# Telegram Reading Bot MVP: Architecture

**Status:** MVP implementation architecture. Dependency pins are declared but were not install-verified in the restricted development environment.

## Architecture decisions

- Use a Python modular monolith and SQLite for the MVP. Do not introduce microservices.
- Keep Telegram handlers thin; business workflow, persistence, source fetching, AI integration, quality checks, scheduling, and publishing have clear responsibilities.
- SQLite is the durable source of truth. APScheduler triggers periodic checks and does not own queue state.
- Run one scheduler/publisher process initially. A non-blocking OS lock beside the SQLite file prevents a second process from starting against the same database.
- Configuration starts with environment variables. No Telegram-editable settings persistence is required for the MVP.

## Proposed stack (not installed or compatibility-verified)

| Responsibility | Proposed technology |
|---|---|
| Runtime | Python 3.12+ |
| Telegram | `python-telegram-bot` |
| Persistence and migrations | SQLite, SQLAlchemy 2.x, Alembic |
| RSS parsing | `feedparser` |
| Article extraction | `trafilatura` |
| Safe article HTTP | Standard-library HTTP(S) connections pinned to validated DNS addresses; `httpx` is pinned for the Telegram SDK transport |
| LLM | Gemini through the official `google-genai` SDK |
| Scheduling | APScheduler |
| Configuration and schemas | Pydantic, `pydantic-settings` |
| Tests and development checks | `pytest`, `ruff`, `mypy` (development extras) |
| Later deployment | Docker on a VPS |

Versions are pinned in `pyproject.toml`. Compatibility must be checked on the target Python/OS at installation; dependency installation and live integrations were not verified in the restricted environment.

## Logical components and flow

Introduce modules incrementally; do not create empty modules just to match a diagram.

1. **Bot interface:** authenticates administrator commands and submissions by Telegram user ID and delegates to services.
2. **Source service:** polls active RSS sources, records submissions and AI-proposed candidates, and enforces source lifecycle/approval.
3. **Fetcher/extractor:** applies SSRF controls, retrieves permitted pages, extracts metadata and text, and enforces response limits and text retention.
4. **Workflow service:** deduplicates articles, selects eligible content, coordinates generation, review, scheduling, and audit events.
5. **Gemini adapter:** uses the explicit configured Gemini model and structured output; keeps provider-specific behavior isolated.
6. **Quality validation:** validates schemas, source-grounding, attribution, similarity, formatting, and Telegram limits before review.
7. **Scheduler/publisher:** atomically claims eligible queue entries, records attempts before network sends, and stores confirmed outcomes.
8. **Database layer:** SQLAlchemy models and repositories/migrations persist durable state; transactions protect related state transitions.

Model input contains source text as untrusted data separate from system instructions. The model receives no tools, credentials, or publishing authority. Human review is the publication gate.

## Relational data model

Tables use integer primary keys. Persisted timestamps use ISO-8601 UTC strings with microseconds and `+00:00`, produced/parsed by `app.timeutil`; naive datetime values are rejected. Required fields are non-null. Foreign keys are enabled on application and migration connections. Indexes and uniqueness constraints below are present in the initial Alembic migration.

### `sources`

- `id` primary key; `name`, `source_type`, `canonical_url`, `lifecycle_state`, `created_at`, `updated_at` required.
- `source_type` is restricted to `rss` or `webpage`; `canonical_url` is unique.
- `category`, `trust_level`, `enabled_at`, `approved_by`, `rejection_reason`, and `last_checked_at` are nullable where not yet applicable. `rejection_reason` is required for rejected candidates.
- Lifecycle: `discovered -> quarantined -> validated -> approved -> active`; rejection is a terminal/review outcome with reason. AI may create candidates but cannot transition them past quarantine or perform approval/activation.
- Index lifecycle state and enabled status for discovery operations.

### `articles`

- `id` primary key; nullable `source_id` references `sources(id)` with `ON DELETE RESTRICT` (submitted items may not have a configured source); required `original_url`, `canonical_url`, `content_hash`, `ingest_kind`, `status`, and timestamps.
- `canonical_url` and extracted `content_hash` are unique after conservative normalization; do not strip query parameters unless known tracking parameters are removed safely. `ingest_kind` identifies RSS or administrator submission.
- `title`, `author`, `published_at`, and `rejection_reason` are nullable. Index `source_id`, status, and discovery time.
- Do not store expiring full text in this permanent metadata row.

### `article_text`

- Separate retention-bound table: `article_id` primary key and foreign key to `articles(id)` with `ON DELETE CASCADE`; required extracted text, `stored_at`, and `expires_at`.
- Index `expires_at` for cleanup. The default expiry is seven days after storage and is configurable through environment variables.
- Deleting expired text must not delete article metadata, drafts, audit records, or publication history. If text is already expired, future generation requiring it must be skipped or require re-extraction under the same safety policy.

### `drafts`

- `id` primary key; required `article_id` references `articles(id)` with `ON DELETE RESTRICT`; required generated text, category, explicit generation model, content hash, `draft_state`, and timestamps.
- Nullable `edited_text`, `approved_by`, `approved_at`, `rejected_by`, and `rejected_at`; rejection requires a reason. Index state and creation time; unique `(article_id, content_hash)` prevents identical repeated drafts for one article.
- State transitions: `generated -> pending_approval -> approved -> scheduled -> publishing -> published`; from review, an administrator may transition to `rejected`; approved/scheduled drafts may be `cancelled`. Regeneration creates a new row and retains the old draft history. The internal `publishing` claim is atomic; an interrupted attempt remains for manual reconciliation. Publishing eligibility requires approval actor and timestamp.

### `publication_attempts`

- `id` primary key; required `draft_id` FK to `drafts(id)` with `ON DELETE RESTRICT`, `destination_id` (configured target channel identifier), `attempt_number`, `state`, `started_at`, and `created_at`.
- `state` is `sending`, `succeeded`, `failed`, or `unknown`; nullable `completed_at`, `telegram_message_id`, `retry_after_seconds`, and sanitized `error_code`/`error_summary`. A succeeded attempt requires a message ID and completion time. A Telegram `RetryAfter` delay is persisted and honored alongside exponential backoff. Never store credentials or full sensitive response bodies. Index `(state, started_at)` and `(draft_id, destination_id)`.
- Unique `(draft_id, destination_id, attempt_number)`. A send must have a persisted attempt before Telegram I/O. A `sending` attempt found after process death is conservatively reconciled as `unknown`, not retried automatically.

### `publications`

- One confirmed publication per draft and destination. Required `draft_id` FK to `drafts(id)` with `ON DELETE RESTRICT`, `destination_id`, `telegram_message_id`, and `published_at`.
- Unique `(draft_id, destination_id)` and unique `(destination_id, telegram_message_id)`. Created only after confirmed Telegram success in the same database transaction that marks the attempt succeeded and draft published. The attempt stores the returned message ID too for audit linkage.
- `publication_attempts` preserve failures and unknowns; `publications` represent confirmed success only.

### `app_settings`

- Small persistent operational flags such as a publishing pause and quota-uncertainty pause. Required key, value, and update timestamp; key is the primary key. This is not a general administrator-editable configuration store.

### `audit_events`

- Append-only administrative/workflow history: required event type, occurrence timestamp, actor type/id where known, target type/id, and minimal structured details. Index timestamp and target. No cascade deletion from source or draft; retain indefinitely by default. Do not include credentials, unnecessary article text, or sensitive payloads.

### `quota_usage`

- Required provider/model, UTC usage date, request count, request/token reservations, and update timestamp; nullable usage fields where the provider does not return them. Unique `(provider, model, usage_date)` and index date.
- Durably reserve/check a request before calling Gemini; charge attempted requests conservatively and record returned usage after. Request-count periods use `GEMINI_RESET_TIMEZONE` (UTC default), keyed to a period date. If token budgeting is enabled but usage is unavailable, set `usage_uncertain`, pause generation, and prevent further token-budgeted calls for that period even if an administrator clears the general pause. If quota status cannot be determined confidently, persist a pause until administrator review. Never invoke a different or paid model as fallback.

### Deletion and constraints

Foreign keys use `RESTRICT` for permanent business history and `CASCADE` only for dependent retention-bound text. Source and article records are not hard-deleted during routine workflows; disable or reject them. Use Alembic migrations for schema changes, preserve records, and test migration/restore behavior. Enforce valid enums/check constraints in SQLite where practical and validate transitions in transactional service logic.

## Source ingestion and discovery

RSS feeds and administrator-submitted article webpages are the supported initial inputs. AI-assisted discovery produces only `discovered`/`quarantined` candidates. Candidate URLs and metadata are untrusted. A human validates and approves a source before it can be activated. Every rejection retains a reason. See `SECURITY.md` for fetch protections.

Canonical URL normalization is deterministic and conservative. Uniqueness in the database is the final duplicate-ingestion guard; content hashes and similarity checks supplement URL deduplication.

## Draft quality and Gemini controls

- Use the official `google-genai` SDK and one explicit configured model. Do not silently switch provider/model or use paid fallback.
- Request a defined structured response and validate it before persistence. Invalid output is rejected; any retry is bounded and budget-accounted.
- Track daily request/token budget. Pause generation and notify an administrator when exhausted, rate/quota availability is uncertain, or usage cannot be accounted for safely.
- Drafts must include source attribution and canonical URL. Check factual claims against available source text; flag unsupported claims for rejection/edit/review, and do not treat a model score as approval.
- Run deterministic required-field, category, duplicate/similarity, safe formatting, attribution, and rendered Telegram-length checks. Do not silently truncate attribution or content.
- Do not send sensitive/private content to Gemini. Expired article text cannot be retained merely for convenience.

## Scheduling, claiming, and recovery

`Africa/Addis_Ababa` is the scheduling timezone; persist UTC instants. Store scheduled publication records in SQLite. APScheduler only wakes a worker to inspect durable state.

The single worker claims a due item in a transaction using a conditional update from `scheduled` to `publishing`, then commits a `sending` attempt before network I/O. The update prevents two workers from claiming the same draft; a file lock prevents a second application scheduler process. A stale `sending` attempt after crash is `unknown` and must be manually reconciled.

After restart, inspect approved/scheduled jobs and attempts, mark stale sends `unknown`, alert administrators, then dispatch eligible overdue jobs gradually. Skip items older than the 24-hour default maximum age; do not publish a burst. Catch-up dispatch observes the same local-day caps/windows and waits the configured 180-minute default interval between overdue publications. Maintain a configurable daily maximum; the 3–5 target is not a quota.

Definite failures may retry only within a bounded attempt count and backoff policy, respecting Telegram rate limits. Ambiguous delivery is never retried automatically. Pause and resume controls must not erase queue or attempt history.

## Proposed project structure

The following is a guide, not a requirement to create all modules in advance:

```text
app/
  main.py
  config.py
  bot/                 # Telegram application, handlers, authorization
  sources/             # RSS, candidate review, URL normalization
  extraction/          # Safe fetching and article extraction
  ai/                  # Gemini adapter, schemas, prompts
  quality/             # Grounding, attribution, similarity, formatting
  database/            # Models, sessions, migrations, persistence
  workflow/            # Business state transitions
  scheduler/           # Polling triggers and atomic queue claims
  publisher/           # Telegram delivery and reconciliation
tests/
docs/
```

## Local development and later deployment

Initial development is local on Kali Linux. VPS deployment and Docker come later and are not implemented by this design document. Before production, deployment must provide durable storage, one scheduler instance, separately stored backups, secret management, log rotation, and network egress controls. See `OPERATIONS.md`.
