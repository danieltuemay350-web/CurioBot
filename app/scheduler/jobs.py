"""Periodic durable workflow jobs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from app.ai.gemini import GenerationPaused
from app.config import Settings
from app.database.models import AppSetting, Article, ArticleText, Draft, Publication, PublicationAttempt, Source
from app.ingestion import add_source_candidate, extract_article, poll_rss
from app.publisher import (AmbiguousPublishError, DefinitePublishError, PublishBlocked,
                           publish_due, recover_stale_attempts)
from app.retention import cleanup_expired_text
from app.timeutil import dump_datetime, utc_now
from app.workflow import (article_retry_count, audit, record_article_failure,
                          schedule_approved_drafts, within_publication_window)

logger = logging.getLogger(__name__)


def _bool_setting(session, key: str) -> bool:
    value = session.get(AppSetting, key)
    return bool(value and value.value == "true")


async def notify_admins(application, settings: Settings, message: str) -> None:
    for admin_id in settings.admin_ids:
        try:
            await application.bot.send_message(chat_id=admin_id, text=message[:4000])
        except Exception:
            logger.error("Could not notify configured administrator")


def initialize_sources(sessions, settings: Settings) -> None:
    if not settings.feed_urls:
        return
    with sessions() as session:
        for url in settings.feed_urls:
            try:
                add_source_candidate(session, url, "rss", url)
            except Exception:
                logger.error("Configured RSS source is invalid", extra={"error_type": "invalid_source_url"})
        session.commit()


async def poll_sources(application, settings: Settings, sessions) -> None:
    with sessions() as session:
        if _bool_setting(session, "publishing_paused"):
            return
        source_ids = session.scalars(select(Source.id).where(Source.lifecycle_state == "active",
                                                              Source.enabled.is_(True),
                                                              Source.source_type == "rss")).all()
    for source_id in source_ids:
        def poll_one() -> int:
            with sessions() as worker_session:
                source = worker_session.get(Source, source_id)
                if not source or source.lifecycle_state != "active" or not source.enabled:
                    return 0
                count = poll_rss(worker_session, source, settings)
                worker_session.commit()
                return count
        try:
            count = await asyncio.to_thread(poll_one)
            if count:
                logger.info("RSS items ingested", extra={"source_id": source_id, "count": count})
        except Exception as exc:
            logger.warning("RSS source poll failed", extra={"source_id": source_id,
                                                              "error_type": type(exc).__name__})
            await notify_admins(application, settings,
                                f"RSS source #{source_id} failed. Check /sources and operational logs.")


async def process_articles(application, settings: Settings, sessions) -> None:
    with sessions() as session:
        if _bool_setting(session, "publishing_paused") or _bool_setting(session, "generation_paused"):
            return
        article_ids = session.scalars(select(Article.id).where(Article.status.in_(["discovered", "extracted"]))
                                      .order_by(Article.discovered_at).limit(3)).all()
    for article_id in article_ids:
        phase = {"name": "extraction"}
        def process_one() -> int | None:
            with sessions() as worker_session:
                article = worker_session.get(Article, article_id)
                if not article or article.status not in {"discovered", "extracted"}:
                    return None
                text_row = worker_session.get(ArticleText, article.id)
                if not text_row or text_row.expires_at <= dump_datetime(utc_now()):
                    try:
                        extract_article(worker_session, article, settings)
                        worker_session.commit()
                    except Exception:
                        worker_session.commit()
                        raise
                phase["name"] = "generation"
                from app.workflow import create_draft
                draft = create_draft(worker_session, article, settings)
                worker_session.commit()
                return draft.id
        try:
            draft_id = await asyncio.to_thread(process_one)
            if draft_id:
                await notify_admins(application, settings,
                                    f"Draft #{draft_id} is ready for review. Use /queue to preview and approve.")
        except GenerationPaused as exc:
            await notify_admins(application, settings, str(exc))
            return
        except Exception as exc:
            with sessions() as failed_session:
                failed = failed_session.get(Article, article_id)
                if failed:
                    record_article_failure(failed_session, failed, exc, phase=phase["name"])
                retry_count = article_retry_count(failed_session, article_id) if failed else 0
                failed_session.commit()
            logger.error("Article processing failed", extra={"article_id": article_id,
                                                               "error_type": type(exc).__name__})
            state = failed.status if failed else "missing"
            reason = failed.rejection_reason if failed else type(exc).__name__
            if state == "failed":
                message = (f"Article #{article_id} is retryable after {type(exc).__name__}. "
                           f"Reason: {reason}. Retry {retry_count}/3 used; use /retry {article_id} if appropriate.")
            else:
                message = f"Article #{article_id} is {state}: {reason}. Inspect /queue for details."
            await notify_admins(application, settings, message)


async def cleanup_text(sessions) -> int:
    with sessions() as session:
        count = cleanup_expired_text(session)
        if count:
            logger.info("Expired article text removed", extra={"count": count})
        return count


async def schedule_jobs(application, settings: Settings, sessions) -> None:
    with sessions() as session:
        if _bool_setting(session, "publishing_paused"):
            return
        schedule_approved_drafts(session, settings)
        session.commit()


async def publish_jobs(application, settings: Settings, sessions) -> None:
    if not within_publication_window(settings):
        return
    with sessions() as session:
        if _bool_setting(session, "publishing_paused"):
            return
        now = dump_datetime(utc_now())
        due_ids = session.scalars(select(Draft.id).where(Draft.draft_state == "scheduled",
                                                        Draft.scheduled_at <= now)
                                  .order_by(Draft.scheduled_at).limit(1)).all()
        for draft_id in due_ids:
            draft = session.get(Draft, draft_id)
            if not draft:
                continue
            scheduled = datetime.fromisoformat(draft.scheduled_at)
            if utc_now() - scheduled > timedelta(hours=settings.max_overdue_age_hours):
                draft.draft_state = "cancelled"
                draft.updated_at = dump_datetime(utc_now())
                session.commit()
                await notify_admins(application, settings,
                                    f"Draft #{draft_id} expired beyond the configured overdue age and was skipped.")
                continue
            last_published = session.scalar(select(func.max(Publication.published_at)))
            if scheduled < utc_now() and last_published:
                previous = datetime.fromisoformat(last_published)
                if utc_now() - previous < timedelta(minutes=settings.catchup_min_interval_minutes):
                    continue
            try:
                async def send(text: str) -> str:
                    try:
                        message = await application.bot.send_message(
                            chat_id=settings.telegram_target_chat_id, text=text,
                            disable_web_page_preview=True)
                    except (RetryAfter, BadRequest, Forbidden) as exc:
                        raise DefinitePublishError(str(exc)) from exc
                    return str(message.message_id)

                await publish_due(session, draft, settings, send,
                                  destination_id=settings.telegram_target_chat_id)
            except DefinitePublishError as exc:
                _mark_definite(session, draft_id, exc)
                await notify_admins(application, settings,
                                    f"Telegram rejected draft #{draft_id}; any retry is bounded and delayed.")
            except AmbiguousPublishError:
                attempt = session.scalar(select(PublicationAttempt).where(
                    PublicationAttempt.draft_id == draft_id,
                    PublicationAttempt.destination_id == settings.telegram_target_chat_id,
                    PublicationAttempt.state == "unknown").order_by(PublicationAttempt.attempt_number.desc()))
                await notify_admins(application, settings,
                    f"Delivery for draft #{draft_id} is UNKNOWN (attempt #{attempt.id if attempt else 'n/a'}). "
                    "Inspect the channel and use /reconcile; do not retry automatically.")
            except PublishBlocked:
                continue
            except TelegramError as exc:
                # Unclassified Telegram transport errors may occur after acceptance.
                attempt = session.scalar(select(PublicationAttempt).where(
                    PublicationAttempt.draft_id == draft_id,
                    PublicationAttempt.state == "unknown").order_by(PublicationAttempt.attempt_number.desc()))
                if attempt and attempt.state == "sending":
                    attempt.state = "unknown"
                    attempt.completed_at = dump_datetime(utc_now())
                    session.commit()
                await notify_admins(application, settings,
                                    f"Telegram result for draft #{draft_id} requires manual review.")
            except Exception:
                logger.error("Unexpected publisher error", extra={"error_type": "publish_error"})


def _mark_definite(session, draft_id: int, error: Exception) -> None:
    attempt = session.scalar(select(PublicationAttempt).where(PublicationAttempt.draft_id == draft_id,
                                                              PublicationAttempt.state == "sending")
                             .order_by(PublicationAttempt.attempt_number.desc()))
    if attempt:
        attempt.state, attempt.completed_at = "failed", dump_datetime(utc_now())
        attempt.error_code, attempt.error_summary = type(error).__name__, type(error).__name__
        session.commit()


def start_scheduler(application, settings: Settings, sessions) -> AsyncIOScheduler:
    initialize_sources(sessions, settings)
    with sessions() as session:
        stale = len(recover_stale_attempts(session))
    if stale:
        asyncio.get_running_loop().create_task(notify_admins(
            application, settings,
            f"Startup reconciliation marked {stale} interrupted Telegram attempt(s) UNKNOWN. "
            "Inspect the channel and use /reconcile before any retry."
        ))
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))
    scheduler.add_job(poll_sources, IntervalTrigger(minutes=settings.rss_poll_interval_minutes),
                      args=[application, settings, sessions], id="poll_sources", max_instances=1,
                      coalesce=True, misfire_grace_time=300)
    scheduler.add_job(process_articles, IntervalTrigger(minutes=settings.process_interval_minutes),
                      args=[application, settings, sessions], id="process_articles", max_instances=1,
                      coalesce=True, misfire_grace_time=300)
    scheduler.add_job(schedule_jobs, IntervalTrigger(minutes=1),
                      args=[application, settings, sessions], id="schedule", max_instances=1, coalesce=True)
    scheduler.add_job(publish_jobs, IntervalTrigger(minutes=1),
                      args=[application, settings, sessions], id="publish", max_instances=1, coalesce=True)
    scheduler.add_job(cleanup_text, IntervalTrigger(hours=1), args=[sessions],
                      id="text_retention", max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler
