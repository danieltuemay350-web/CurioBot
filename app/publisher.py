"""Durable Telegram publishing and manual reconciliation."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.models import AuditEvent, Draft, Publication, PublicationAttempt
from app.timeutil import dump_datetime, local_day_bounds, utc_now
from app.workflow import audit


class DefinitePublishError(RuntimeError):
    """Telegram explicitly rejected the send; retry may be safe within limits."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AmbiguousPublishError(RuntimeError):
    """The request may have reached Telegram but no reliable result arrived."""


class PublishBlocked(RuntimeError):
    """Publishing is blocked by a safety or scheduling condition."""


def normalize_retry_after(value: object) -> float | None:
    if isinstance(value, timedelta):
        value = value.total_seconds()
    if isinstance(value, bool):
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def _valid_message_id(value: object) -> bool:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return False
    raw = str(value)
    return (raw.isascii() and raw.isdecimal() and len(raw) <= 20
            and 0 < int(raw) <= 2**63 - 1)


async def publish_due(session: Session, draft: Draft, settings: Settings,
                      send: Callable[[str], Awaitable[str]], *, destination_id: str) -> PublicationAttempt:
    if draft.draft_state == "publishing":
        raise PublishBlocked("Previous attempt requires manual reconciliation")
    if draft.draft_state != "scheduled" or not draft.approved_by or not draft.scheduled_at:
        raise PublishBlocked("Only approved, scheduled drafts can be published")
    now = utc_now()
    scheduled = datetime.fromisoformat(draft.scheduled_at)
    if scheduled > now:
        raise PublishBlocked("Draft is not due yet")
    day_start, day_end = local_day_bounds(timezone_name=settings.timezone)
    sent_today = session.scalar(select(func.count(Publication.id)).where(
        Publication.published_at >= dump_datetime(day_start), Publication.published_at < dump_datetime(day_end))) or 0
    if sent_today >= settings.publication_daily_limit:
        raise PublishBlocked("Daily publication limit reached")
    last = session.scalar(select(PublicationAttempt).where(
        PublicationAttempt.draft_id == draft.id,
        PublicationAttempt.destination_id == destination_id
    ).order_by(PublicationAttempt.attempt_number.desc()))
    if last and last.state == "sending":
        raise PublishBlocked("Previous attempt requires manual reconciliation")
    if last and last.state == "unknown":
        acknowledged_absence = session.scalar(select(AuditEvent.id).where(
            AuditEvent.event_type == "unknown_send_absence_confirmed",
            AuditEvent.target_type == "publication_attempt",
            AuditEvent.target_id == str(last.id)))
        if not acknowledged_absence:
            raise PublishBlocked("Previous attempt requires manual reconciliation")
    if last and last.state == "failed" and last.attempt_number >= settings.publish_retry_limit:
        now = dump_datetime(utc_now())
        draft.draft_state, draft.updated_at = "cancelled", now
        audit(session, "publication_retry_limit_reached", "draft", draft.id,
              details={"attempt_id": last.id, "attempt_number": last.attempt_number,
                       "retry_limit": settings.publish_retry_limit})
        session.commit()
        raise PublishBlocked("Definite publication retry limit reached")
    if last and last.state == "failed" and last.completed_at:
        retry_after = datetime.fromisoformat(last.completed_at) + timedelta(
            seconds=max(settings.retry_base_seconds * (2 ** max(0, last.attempt_number - 1)),
                        last.retry_after_seconds or 0))
        if now < retry_after:
            raise PublishBlocked("Definite failure retry backoff is still active")
    claim = session.execute(update(Draft).where(
        Draft.id == draft.id, Draft.draft_state == "scheduled",
        Draft.approved_by.is_not(None), Draft.scheduled_at <= dump_datetime(now)
    ).values(draft_state="publishing", updated_at=dump_datetime(now)))
    if claim.rowcount != 1:
        session.rollback()
        raise PublishBlocked("Draft was already claimed or is no longer eligible")
    attempt_no = (last.attempt_number if last else 0) + 1
    attempt = PublicationAttempt(draft_id=draft.id, destination_id=destination_id,
                                 attempt_number=attempt_no, state="sending",
                                 started_at=dump_datetime(now))
    session.add(attempt)
    session.commit()  # Required durable write before any Telegram network operation.
    content = draft.edited_text or draft.generated_text
    try:
        message_id = await send(content)
        if not _valid_message_id(message_id):
            raise ValueError("Telegram response did not include a valid message ID")
    except DefinitePublishError as exc:
        attempt.state, attempt.completed_at = "failed", dump_datetime(utc_now())
        attempt.error_code, attempt.error_summary = type(exc).__name__, type(exc).__name__
        attempt.retry_after_seconds = exc.retry_after_seconds
        draft.draft_state = "scheduled"
        session.commit()
        audit(session, "publication_failed_definite", "draft", draft.id,
              details={"attempt_id": attempt.id, "attempt": attempt_no})
        session.commit()
        raise
    except Exception as exc:
        attempt.state, attempt.completed_at = "unknown", dump_datetime(utc_now())
        attempt.error_code, attempt.error_summary = type(exc).__name__, "Delivery result is ambiguous; manual reconciliation required"
        session.commit()
        audit(session, "publication_unknown", "draft", draft.id,
              details={"attempt_id": attempt.id, "attempt": attempt_no})
        session.commit()
        raise AmbiguousPublishError("Telegram delivery is uncertain; do not retry automatically") from exc
    attempt.state = "succeeded"
    attempt.completed_at = dump_datetime(utc_now())
    attempt.telegram_message_id = str(message_id)
    publication = Publication(draft_id=draft.id, destination_id=destination_id,
                              telegram_message_id=str(message_id), published_at=attempt.completed_at)
    draft.draft_state, draft.updated_at = "published", attempt.completed_at
    session.add(publication)
    audit(session, "publication_confirmed", "draft", draft.id,
          details={"attempt_id": attempt.id, "message_id": str(message_id)})
    session.commit()
    return attempt


def reconcile_unknown(session: Session, attempt: PublicationAttempt, actor_id: int,
                      *, exists_in_channel: bool, message_id: str | None = None) -> None:
    if attempt.state != "unknown":
        raise ValueError("Only unknown attempts can be reconciled")
    now = dump_datetime(utc_now())
    if exists_in_channel:
        if not _valid_message_id(message_id):
            raise ValueError("Telegram message ID is required to confirm delivery")
        message_id = str(message_id)
        attempt.state, attempt.telegram_message_id, attempt.completed_at = "succeeded", message_id, now
        publication = Publication(draft_id=attempt.draft_id, destination_id=attempt.destination_id,
                                  telegram_message_id=message_id, published_at=now)
        session.add(publication)
        draft = session.get(Draft, attempt.draft_id)
        assert draft is not None
        draft.draft_state, draft.updated_at = "published", now
        event = "unknown_send_confirmed"
    else:
        # Keep the ambiguous attempt intact. Explicit operator action acknowledges that a
        # later retry can duplicate a post if channel inspection was incomplete.
        event = "unknown_send_absence_confirmed"
        attempt.error_summary = "Administrator checked channel and authorized a possible new attempt"
        draft = session.get(Draft, attempt.draft_id)
        if draft:
            draft.draft_state, draft.updated_at = "scheduled", now
    audit(session, event, "publication_attempt", attempt.id, actor_id,
          {"exists_in_channel": exists_in_channel, "message_id": message_id})
    session.commit()


def recover_stale_attempts(session: Session) -> list[int]:
    """Conservatively convert interrupted sends to unknown after process restart."""
    stale = session.scalars(select(PublicationAttempt).where(PublicationAttempt.state == "sending")).all()
    ids: list[int] = []
    for attempt in stale:
        attempt.state = "unknown"
        attempt.completed_at = dump_datetime(utc_now())
        attempt.error_summary = "Process ended before delivery outcome was recorded"
        audit(session, "stale_attempt_marked_unknown", "publication_attempt", attempt.id)
        ids.append(attempt.id)
    session.commit()
    return ids

