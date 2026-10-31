"""Application business workflows and state transitions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.gemini import generate_draft
from app.config import Settings
from app.database.models import Article, ArticleText, AuditEvent, Draft, Publication, Source
from app.ingestion import (DuplicateArticleError, PermanentArticleRejection,
                           extract_article)
from app.timeutil import dump_datetime, local_day_bounds, utc_now


class WorkflowError(ValueError):
    """Invalid workflow operation."""


MAX_ARTICLE_RETRIES = 3


def within_publication_window(settings: Settings, instant: datetime | None = None) -> bool:
    local = (instant or utc_now()).astimezone(ZoneInfo(settings.timezone)).time().replace(tzinfo=None)
    start = time.fromisoformat(settings.publication_window_start)
    end = time.fromisoformat(settings.publication_window_end)
    if end > start:
        return start <= local < end
    return local >= start or local < end


def audit(session: Session, event: str, target_type: str, target_id: int | str,
          actor_id: int | None = None, details: dict[str, object] | None = None) -> None:
    session.add(AuditEvent(event_type=event, occurred_at=dump_datetime(utc_now()),
                           actor_type="admin" if actor_id is not None else "system",
                           actor_id=str(actor_id) if actor_id is not None else None,
                           target_type=target_type, target_id=str(target_id),
                           details=json.dumps(details or {}, ensure_ascii=True)[:4000]))


def _similarity_hash(text: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    return hashlib.sha256(" ".join(tokens).encode()).hexdigest()


def create_draft(session: Session, article: Article, settings: Settings, *, client=None,
                 ignore_draft_ids: tuple[int, ...] = ()) -> Draft:
    if article.status not in {"extracted", "processed"}:
        raise WorkflowError("Article has not passed extraction")
    text = session.get(ArticleText, article.id)
    if not text or text.expires_at <= dump_datetime(utc_now()):
        raise WorkflowError("Article text is missing or expired")
    if not article.title:
        raise WorkflowError("Article title is required for drafting")
    source = session.get(Source, article.source_id) if article.source_id else None
    source_name = source.name if source else "Submitted source"
    result = generate_draft(session, settings, text.extracted_text, article.title,
                            source_name, article.canonical_url, client=client)
    content = result.body.strip()
    if len(content) > 4096:
        raise WorkflowError("Generated post exceeds Telegram's message limit")
    latest_query = select(Draft).where(Draft.draft_state.in_(
        ["pending_approval", "published", "scheduled", "approved"]))
    if ignore_draft_ids:
        latest_query = latest_query.where(Draft.id.not_in(ignore_draft_ids))
    latest = session.scalar(latest_query.order_by(Draft.created_at.desc()))
    if latest and latest.category.casefold() == result.category.casefold():
        raise WorkflowError("Consecutive posts would repeat the same category")
    digest = _similarity_hash(content)
    words = set(re.findall(r"[a-z0-9]+", content.casefold()))
    candidates_query = select(Draft).where(Draft.draft_state != "cancelled")
    if ignore_draft_ids:
        candidates_query = candidates_query.where(Draft.id.not_in(ignore_draft_ids))
    for candidate in session.scalars(candidates_query):
        if digest == candidate.content_hash:
            raise WorkflowError("Duplicate post content detected")
        other = set(re.findall(r"[a-z0-9]+", (candidate.edited_text or candidate.generated_text).casefold()))
        union = words | other
        if union and len(words & other) / len(union) >= 0.82:
            raise WorkflowError("Post is too similar to an existing draft")
    now = dump_datetime(utc_now())
    draft = Draft(article_id=article.id, generated_text=content, category=result.category,
                  generation_model=settings.gemini_model, content_hash=digest,
                  draft_state="pending_approval", created_at=now, updated_at=now)
    session.add(draft)
    article.status = "processed"
    article.updated_at = now
    session.flush()
    audit(session, "draft_created", "draft", draft.id, details={"model": settings.gemini_model})
    return draft


def process_article(session: Session, article: Article, settings: Settings, *, client=None) -> Draft:
    if not session.get(ArticleText, article.id):
        extract_article(session, article, settings)
    return create_draft(session, article, settings, client=client)


def approve_draft(session: Session, draft: Draft, actor_id: int) -> None:
    if draft.draft_state != "pending_approval":
        raise WorkflowError("Only pending drafts can be approved")
    now = dump_datetime(utc_now())
    draft.draft_state, draft.approved_by, draft.approved_at = "approved", actor_id, now
    draft.updated_at = now
    audit(session, "draft_approved", "draft", draft.id, actor_id)


def reject_draft(session: Session, draft: Draft, actor_id: int, reason: str) -> None:
    if draft.draft_state not in {"pending_approval", "approved", "scheduled"}:
        raise WorkflowError("Draft cannot be rejected in its current state")
    if not reason.strip():
        raise WorkflowError("A rejection reason is required")
    now = dump_datetime(utc_now())
    draft.draft_state, draft.rejected_by, draft.rejected_at = "rejected", actor_id, now
    draft.rejection_reason, draft.updated_at = reason.strip()[:1000], now
    audit(session, "draft_rejected", "draft", draft.id, actor_id)


def article_retry_count(session: Session, article_id: int) -> int:
    return session.scalar(select(func.count(AuditEvent.id)).where(
        AuditEvent.event_type == "article_retry_requested",
        AuditEvent.target_type == "article",
        AuditEvent.target_id == str(article_id),
    )) or 0


def mark_article_generation_failure(session: Session, article: Article, error: Exception) -> None:
    """Record a retryable generation/quality failure without erasing prior context."""
    now = dump_datetime(utc_now())
    article.status = "failed"
    if not article.rejection_reason:
        article.rejection_reason = f"generation:{type(error).__name__}"
    article.updated_at = now
    audit(session, "article_generation_failed", "article", article.id,
          details={"error_type": type(error).__name__,
                   "retry_count": article_retry_count(session, article.id),
                   "retry_limit": MAX_ARTICLE_RETRIES})


def record_article_failure(session: Session, article: Article, error: Exception,
                           *, phase: str) -> None:
    """Persist a terminal ingestion outcome or a retryable processing failure."""
    if article.status in {"processed", "rejected", "duplicate", "completed"}:
        return
    if phase == "generation":
        mark_article_generation_failure(session, article, error)
        return
    if isinstance(error, DuplicateArticleError):
        article.status = "duplicate"
        article.rejection_reason = article.rejection_reason or "duplicate_article_content"
        audit(session, "article_ingestion_duplicate", "article", article.id)
    else:
        terminal = isinstance(error, PermanentArticleRejection)
        article.status = "rejected" if terminal else "failed"
        if not article.rejection_reason:
            article.rejection_reason = type(error).__name__
        audit(session, "article_ingestion_failed", "article", article.id,
              details={"error_type": type(error).__name__, "terminal": terminal})
    article.updated_at = dump_datetime(utc_now())


def retry_article(session: Session, article: Article, actor_id: int) -> None:
    if article.status != "failed":
        raise WorkflowError("Only retryable failed articles can be retried")
    published = session.scalar(select(Publication.id).join(
        Draft, Draft.id == Publication.draft_id).where(Draft.article_id == article.id).limit(1))
    if published is not None:
        raise WorkflowError("Articles with a confirmed publication cannot be retried")
    retries = article_retry_count(session, article.id)
    if retries >= MAX_ARTICLE_RETRIES:
        raise WorkflowError(f"Article retry limit ({MAX_ARTICLE_RETRIES}) reached")
    extracted_text = session.get(ArticleText, article.id)
    text_is_available = bool(extracted_text and
                             extracted_text.expires_at > dump_datetime(utc_now()))
    article.status = "extracted" if text_is_available else "discovered"
    article.updated_at = dump_datetime(utc_now())
    audit(session, "article_retry_requested", "article", article.id, actor_id,
          {"retry_number": retries + 1, "retry_limit": MAX_ARTICLE_RETRIES,
           "previous_reason": article.rejection_reason})


def edit_draft(session: Session, draft: Draft, actor_id: int, text: str) -> None:
    if draft.draft_state != "pending_approval":
        raise WorkflowError("Only pending drafts can be edited")
    clean = text.strip()
    if not clean or len(clean) > 4096:
        raise WorkflowError("Edited message must be between 1 and 4096 characters")
    if draft.article.canonical_url not in clean:
        clean += f"\n\nSource: {draft.article.canonical_url}"
    words = set(re.findall(r"[a-z0-9]+", clean.casefold()))
    for candidate in session.scalars(select(Draft).where(
            Draft.id != draft.id, Draft.draft_state != "cancelled")):
        other = set(re.findall(r"[a-z0-9]+", (candidate.edited_text or candidate.generated_text).casefold()))
        union = words | other
        if union and len(words & other) / len(union) >= 0.82:
            raise WorkflowError("Edited post is too similar to an existing draft")
    draft.edited_text = clean
    draft.content_hash = _similarity_hash(clean)
    draft.updated_at = dump_datetime(utc_now())
    audit(session, "draft_edited", "draft", draft.id, actor_id)


def schedule_approved_drafts(session: Session, settings: Settings,
                             now: datetime | None = None) -> int:
    """Assign deterministic spaced UTC slots while enforcing each local day's cap."""
    now = now or utc_now()
    if now.tzinfo is None:
        raise ValueError("Scheduling clock must be timezone-aware")
    now = now.astimezone(UTC)
    local_zone = ZoneInfo(settings.timezone)
    start_h, start_m = map(int, settings.publication_window_start.split(":"))
    end_h, end_m = map(int, settings.publication_window_end.split(":"))

    def window_for(day):
        beginning = datetime.combine(day, time(start_h, start_m), local_zone)
        ending = datetime.combine(day, time(end_h, end_m), local_zone)
        if ending <= beginning:
            ending += timedelta(days=1)
        return beginning.astimezone(UTC), ending.astimezone(UTC)

    due = session.scalars(select(Draft).where(Draft.draft_state == "approved")
                          .order_by(Draft.approved_at, Draft.id)).all()
    assigned = 0
    existing_events = [datetime.fromisoformat(value).astimezone(UTC)
                       for value in session.scalars(select(Draft.scheduled_at).where(
                           Draft.draft_state == "scheduled", Draft.scheduled_at.is_not(None))).all()]
    existing_events.extend(datetime.fromisoformat(value).astimezone(UTC)
                            for value in session.scalars(select(Publication.published_at)).all())
    interval = timedelta(minutes=settings.catchup_min_interval_minutes)
    last_event = max(existing_events) if existing_events else None
    next_time = max(now, last_event + interval) if last_event else now

    for draft in due:
        approved = datetime.fromisoformat(draft.approved_at or draft.created_at).astimezone(UTC)
        if now - approved > timedelta(hours=settings.max_overdue_age_hours):
            draft.draft_state, draft.updated_at = "cancelled", dump_datetime(now)
            audit(session, "draft_expired_overdue", "draft", draft.id)
            continue
        while True:
            day = next_time.astimezone(local_zone).date()
            window_start, window_end = window_for(day)
            if next_time < window_start:
                next_time = window_start
            if next_time >= window_end:
                next_time = window_for(day + timedelta(days=1))[0]
                continue
            day_start, day_end = local_day_bounds(day, settings.timezone)
            published_count = session.scalar(select(func.count(Publication.id)).where(
                Publication.published_at >= dump_datetime(day_start),
                Publication.published_at < dump_datetime(day_end))) or 0
            scheduled_count = session.scalar(select(func.count(Draft.id)).where(
                Draft.draft_state == "scheduled",
                Draft.scheduled_at >= dump_datetime(day_start),
                Draft.scheduled_at < dump_datetime(day_end))) or 0
            if published_count + scheduled_count < settings.publication_daily_limit:
                break
            next_time = window_for(day + timedelta(days=1))[0]
        draft.scheduled_at = dump_datetime(next_time)
        draft.draft_state, draft.updated_at = "scheduled", dump_datetime(now)
        assigned += 1
        last_event = next_time
        next_time = last_event + interval
    return assigned


def stats(session: Session) -> dict[str, int]:
    return {"sources": session.scalar(select(func.count(Source.id))) or 0,
            "articles": session.scalar(select(func.count(Article.id))) or 0,
            "pending_drafts": session.scalar(select(func.count(Draft.id)).where(Draft.draft_state == "pending_approval")) or 0,
            "scheduled_drafts": session.scalar(select(func.count(Draft.id)).where(Draft.draft_state == "scheduled")) or 0,
            "publications": session.scalar(select(func.count(Publication.id))) or 0}
