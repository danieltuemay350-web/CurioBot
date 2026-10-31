# Telegram Reading Bot MVP: Product Specification

**Status:** Confirmed product requirements for the implemented MVP. Live Telegram/Gemini behavior has not been verified in this environment.

## Problem and goals

The product is a Telegram knowledge-sharing bot that helps administrators turn useful reading into concise, original, attributed channel posts. It should reduce routine work while preserving editorial approval, factual care, variety, and safe handling of external content.

The channel identity is curiosity-driven learning across technology, software, AI, cybersecurity, science, engineering, psychology, philosophy, history, culture, books, and interesting discoveries.

Success means relevant sources can be reviewed, useful drafts can be produced with attribution, approved posts can be scheduled reliably, and failures or uncertain outcomes are visible to an administrator. The target is three to five posts per day, but quality takes precedence: fewer posts or no post is acceptable when the queue lacks suitable material.

## MVP scope and actors

The MVP has Telegram administrators, an existing target channel, configured RSS sources, administrator-submitted article URLs, and AI-assisted source discovery. Initially supported source types are RSS feeds and article webpages.

Every publication requires administrator approval. AI may assist with discovery and drafting but cannot activate a source, approve content, or publish. Discovered sources remain quarantined until validation and administrator approval.

### User stories

- As an administrator, I can submit an article URL, see its extraction/draft outcome, and review its attribution before approval.
- As an administrator, I can inspect, validate, approve, reject, enable, or disable a source candidate and retain the reason for rejection.
- As an administrator, I can preview and edit a draft, approve or reject it, and request regeneration when appropriate.
- As an administrator, I can inspect and manage the publication queue, pause/resume processing, and see service status, statistics, and errors.
- As an administrator, I receive an alert when a send is uncertain and can reconcile it manually before any further attempt.
- As a channel reader, I receive concise, original posts with source attribution and canonical URL, distributed through the day without repetitive consecutive topics.

## Content workflow

1. Collect RSS entries from configured sources and accept article URLs from authorized administrator submissions.
2. AI-assisted discovery may propose additional sources. Every candidate starts quarantined and must pass independent validation and administrator approval before activation.
3. Validate source and URL safety, normalize the canonical URL, detect duplicates, and extract permitted article metadata and text.
4. Generate structured draft content using the configured Gemini model. Validate the structure and run deterministic content checks.
5. Present drafts for administrator review, editing, approval, or rejection. Quality checks or an LLM score alone never authorize a post.
6. Schedule approved drafts across the day subject to daily caps, publication windows, and topic variety. If quality is insufficient, leave slots empty.
7. Record every publication attempt and result. Uncertain delivery becomes `unknown`, is alerted, and requires manual reconciliation.

Both RSS and submitted URLs use the same downstream extraction, deduplication, drafting, review, and publication workflow.

## Telegram administration

Management commands are restricted to configured administrator Telegram user IDs:

| Command | Intended purpose |
|---|---|
| `/start` | Explain the bot and administrator access |
| `/help` | Show command and workflow guidance |
| `/status` | Show pause state and operational health |
| `/pause` | Pause collection/processing/publication according to defined controls |
| `/resume` | Resume eligible work |
| `/stats` | Show source, draft, and publication counts |
| `/sources` | Review source status and source candidates |
| `/queue` | Inspect and manage drafts and scheduled items |
| `/settings` | Show effective settings; initial configuration is through environment variables |

Interactions include administrator URL submission, source review, draft preview/edit/regeneration, approval/rejection, queue management, and operational error notifications. Exact command payloads and edit flows are implementation details and must be documented when implemented.

## MVP acceptance criteria

The MVP is acceptable when implementation and tests demonstrate that:

1. RSS and administrator-submitted webpage inputs enter the same pipeline and duplicate canonical URLs are not ingested twice.
2. AI-discovered candidates remain quarantined until validation and explicit administrator approval; model output cannot approve or activate them.
3. Unsafe URLs and destinations are rejected, including unsafe redirect targets; request limits are enforced.
4. Extracted article text expires after seven days by default, with the duration configurable through environment variables. Source metadata, drafts, publication history, and audit records are retained indefinitely by default.
5. Gemini uses a single explicitly configured model and the official `google-genai` SDK. Generation pauses if the configured daily budget is exhausted or quota availability is uncertain; there is no silent provider/model or paid fallback.
6. Structured model output is validated; drafts include source attribution and canonical URL and pass deterministic quality, duplicate/similarity, formatting, factual-support review, and Telegram-length checks before being offered for approval.
7. No post is published without administrator approval. Scheduling respects `Africa/Addis_Ababa`, daily limits, publication windows, and topic variety; insufficient quality may yield fewer or zero posts.
8. A publication attempt is durable before sending. Confirmed success stores Telegram identifiers and timestamp; definite failures follow bounded retry rules; ambiguous sends become `unknown` and are never automatically retried.
9. Recovery after downtime gradually schedules eligible overdue approved items subject to daily limits and a configurable maximum age, without a catch-up burst.
10. Management commands reject unauthorized users, operational failures are visible to administrators, and tests use mocks rather than the real channel or production quotas.

## Explicitly out of scope

- Unrestricted crawling or automatic activation of discovered sources.
- Multi-channel publishing.
- Complex user management and role hierarchies.
- Analytics dashboards.
- Newsletter ingestion, arbitrary document formats, image generation, and advanced recommendation systems.
- Autonomous publishing without administrator approval.

## Retention and time

Extracted article text has a seven-day default retention, configurable through environment variables. Source metadata, drafts, publication history, and audit records are retained indefinitely by default, subject to future explicit retention policies. Retention cleanup must not erase publication history. Persist timestamps in UTC; schedule and display local times using `Africa/Addis_Ababa`.
