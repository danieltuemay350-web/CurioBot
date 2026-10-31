"""Telegram handlers and administrator workflow."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from app.config import Settings
from app.ai.gemini import discover_sources
from app.database.models import AppSetting, Article, Draft, PublicationAttempt, QuotaUsage, Source
from app.ingestion import add_source_candidate, submit_article_url, transition_source
from app.security.urls import FetchLimits, safe_fetch
from app.publisher import reconcile_unknown
from app.timeutil import dump_datetime, utc_now
from app.workflow import (MAX_ARTICLE_RETRIES, WorkflowError, article_retry_count,
                          approve_draft, edit_draft, reject_draft, stats)
from app.bot.authorization import require_admin

logger = logging.getLogger(__name__)


def _sessions(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["sessions"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_admin(update, context, _settings(context)):
        await update.effective_message.reply_text("Reading Bot admin console. Use /help for commands. All posts require approval.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_admin(update, context, _settings(context)):
        await update.effective_message.reply_text(
            "/status /pause /resume /quota_resume /stats /sources /queue /settings\n"
            "Submit a webpage URL as a message.\n"
            "/addsource rss|webpage URL [name]\n/approve ID /reject ID reason /edit ID text /regenerate ID\n"
            "/retry ARTICLE_ID /discover topic /reconcile ATTEMPT_ID found MESSAGE_ID|absent"
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    with _sessions(context)() as session:
        paused = session.get(AppSetting, "publishing_paused")
        gen_paused = session.get(AppSetting, "generation_paused")
        config = _settings(context)
        quota_day = utc_now().astimezone(ZoneInfo(config.gemini_reset_timezone)).date().isoformat()
        quota = session.scalar(select(QuotaUsage).where(
            QuotaUsage.provider == "gemini", QuotaUsage.model == _settings(context).gemini_model
        , QuotaUsage.usage_date == quota_day))
        failed_articles = session.scalar(select(func.count(Article.id)).where(Article.status == "failed")) or 0
        rejected_articles = session.scalar(select(func.count(Article.id)).where(
            Article.status.in_(["rejected", "duplicate"]))) or 0
        await update.effective_message.reply_text(
            f"Publishing: {'paused' if paused and paused.value == 'true' else 'active'}\n"
            f"Generation: {'paused' if gen_paused and gen_paused.value == 'true' else 'active'}\n"
            f"Gemini requests this budget period: {quota.request_count if quota else 0}/"
            f"{config.gemini_daily_request_budget}; tokens: {quota.token_count if quota and quota.token_count is not None else 'unknown'}"
            f"/{config.gemini_daily_token_budget or 'disabled'}\n"
            f"Gemini token usage certainty: {'uncertain' if quota and quota.usage_uncertain else 'known'}\n"
            f"Articles: {failed_articles} retryable failures, {rejected_articles} rejected/duplicate\n"
            f"Database: available\nScheduler: {'running' if context.application.bot_data.get('scheduler') else 'not running'}"
        )


async def _set_pause(update: Update, context: ContextTypes.DEFAULT_TYPE, paused: bool) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    user_id = update.effective_user.id
    with _sessions(context)() as session:
        row = session.get(AppSetting, "publishing_paused")
        now = dump_datetime(utc_now())
        if row:
            row.value, row.updated_at = str(paused).lower(), now
        else:
            session.add(AppSetting(key="publishing_paused", value=str(paused).lower(), updated_at=now))
        from app.workflow import audit
        audit(session, "publishing_paused" if paused else "publishing_resumed", "settings", "publishing", user_id)
        session.commit()
    await update.effective_message.reply_text("Publishing paused." if paused else "Publishing resumed.")


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_pause(update, context, True)


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_pause(update, context, False)


async def quota_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    from app.ai.gemini import resume_generation
    with _sessions(context)() as session:
        resume_generation(session, update.effective_user.id)
    await update.effective_message.reply_text(
        "Generation pause cleared. The configured daily budgets still apply; "
        "confirm quota availability before resuming."
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    with _sessions(context)() as session:
        values = stats(session)
    await update.effective_message.reply_text("\n".join(f"{key}: {value}" for key, value in values.items()))


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    config = _settings(context)
    await update.effective_message.reply_text(
        f"Timezone: {config.timezone}\nDaily publication limit: {config.publication_daily_limit}\n"
        f"Window: {config.publication_window_start}–{config.publication_window_end}\n"
        f"Gemini model: {config.gemini_model}\nDaily request budget: {config.gemini_daily_request_budget}\n"
        f"Daily token budget: {config.gemini_daily_token_budget or 'disabled'} (reset {config.gemini_reset_timezone})\n"
        f"Text retention: {config.article_text_retention_days} days\n"
        "Settings are controlled through environment variables. Secrets are hidden."
    )


async def sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    with _sessions(context)() as session:
        sources = session.scalars(select(Source).order_by(Source.created_at.desc()).limit(30)).all()
        buttons = []
        lines = []
        for source in sources:
            lines.append(f"#{source.id} {source.name}: {source.lifecycle_state} ({source.source_type})")
            if source.lifecycle_state == "quarantined":
                buttons.append([InlineKeyboardButton(f"Validate source #{source.id}", callback_data=f"source:validate:{source.id}")])
                buttons.append([InlineKeyboardButton(f"Reject source #{source.id}", callback_data=f"source:reject:{source.id}")])
            if source.lifecycle_state == "validated":
                buttons.append([InlineKeyboardButton(f"Approve source #{source.id}", callback_data=f"source:approve:{source.id}"),
                                InlineKeyboardButton(f"Reject source #{source.id}", callback_data=f"source:reject:{source.id}")])
            if source.lifecycle_state == "approved":
                buttons.append([InlineKeyboardButton(f"Activate source #{source.id}", callback_data=f"source:activate:{source.id}")])
            if source.lifecycle_state in {"active", "approved"}:
                buttons.append([InlineKeyboardButton(f"Disable source #{source.id}", callback_data=f"source:disable:{source.id}")])
    await update.effective_message.reply_text("\n".join(lines) or "No sources.", reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    with _sessions(context)() as session:
        drafts = session.scalars(select(Draft).where(Draft.draft_state.in_(["pending_approval", "approved", "scheduled", "publishing"])).order_by(Draft.created_at.desc()).limit(20)).all()
        buttons = []
        lines = []
        for draft in drafts:
            preview = (draft.edited_text or draft.generated_text)[:250]
            scheduled_local = ""
            if draft.scheduled_at:
                scheduled_local = datetime.fromisoformat(draft.scheduled_at).astimezone(
                    ZoneInfo(_settings(context).timezone)).strftime("%Y-%m-%d %H:%M %Z")
            unknown = session.scalar(select(PublicationAttempt).where(
                PublicationAttempt.draft_id == draft.id,
                PublicationAttempt.state == "unknown").order_by(PublicationAttempt.attempt_number.desc()))
            attempt_label = f" unknown attempt=#{unknown.id}" if unknown else ""
            lines.append(f"#{draft.id} {draft.category} [{draft.draft_state}]{attempt_label} {scheduled_local}\n{preview}")
            buttons.append([InlineKeyboardButton(f"Preview #{draft.id}", callback_data=f"draft:preview:{draft.id}")])
            if draft.draft_state == "pending_approval":
                buttons.append([InlineKeyboardButton(f"Approve #{draft.id}", callback_data=f"draft:approve:{draft.id}"),
                                InlineKeyboardButton(f"Reject #{draft.id}", callback_data=f"draft:reject:{draft.id}")])
        articles = session.scalars(select(Article).where(
            Article.status.in_(["failed", "rejected", "duplicate"])
        ).order_by(Article.updated_at.desc()).limit(8)).all()
        for article in articles:
            retries = article_retry_count(session, article.id)
            reason = (article.rejection_reason or "unspecified")[:120]
            lines.append(f"Article #{article.id} [{article.status}; retries {retries}/{MAX_ARTICLE_RETRIES}] {reason}")
    await update.effective_message.reply_text(("\n\n".join(lines) or "Queue is empty.")[:4000],
                                              reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def submit_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    text = (update.effective_message.text or "").strip()
    if not text.startswith(("http://", "https://")):
        return
    try:
        with _sessions(context)() as session:
            article, created = submit_article_url(session, text, _settings(context), update.effective_user.id)
            session.commit()
            await update.effective_message.reply_text(
                f"URL {'queued' if created else 'already recorded'} as article #{article.id}. "
                "Extraction and draft generation will run in the background."
            )
    except Exception:
        logger.error("URL submission failed", extra={"error_type": "submission_rejected"})
        await update.effective_message.reply_text("URL could not be accepted. Check the URL and try again.")


async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    if len(context.args) < 2 or context.args[0] not in {"rss", "webpage"}:
        await update.effective_message.reply_text("Usage: /addsource rss|webpage URL [name]")
        return
    kind, url = context.args[0], context.args[1]
    name = " ".join(context.args[2:]) or url
    try:
        with _sessions(context)() as session:
            source = add_source_candidate(session, url, kind, name, actor_id=update.effective_user.id)
            session.commit()
        await update.effective_message.reply_text(f"Candidate source #{source.id} is quarantined for review.")
    except Exception:
        logger.error("Source candidate submission failed", extra={"error_type": "candidate_rejected"})
        await update.effective_message.reply_text("Source candidate rejected. Check its URL and type.")


async def source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await require_admin(update, context, _settings(context)):
        return
    try:
        _, action, raw_id = query.data.split(":")
        with _sessions(context)() as session:
            source = session.get(Source, int(raw_id))
            if not source:
                raise ValueError("Source not found")
            source_url, source_type = source.canonical_url, source.source_type
        if action == "validate":
            config = _settings(context)
            limits = FetchLimits(config.http_connect_timeout_seconds,
                                 config.http_read_timeout_seconds,
                                 config.http_max_response_bytes,
                                 config.http_max_redirects, config.allowed_ports)
            response = await asyncio.to_thread(safe_fetch, source_url, limits)
            if source_type == "rss":
                import feedparser
                parsed = feedparser.parse(response.body)
                if parsed.bozo and not parsed.entries:
                    raise ValueError("RSS feed did not parse")
            elif response.content_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError("Web source is not an HTML page")
        with _sessions(context)() as session:
            source = session.get(Source, int(raw_id))
            if not source:
                raise ValueError("Source not found")
            if action == "reject":
                transition_source(session, source, action, query.from_user.id, "Rejected by administrator")
            else:
                transition_source(session, source, action, query.from_user.id)
            session.commit()
        await query.answer("Source updated")
        await query.edit_message_text(f"Source #{raw_id} is now {source.lifecycle_state}.")
    except Exception:
        logger.error("Source callback failed", extra={"error_type": "source_action_failed"})
        await query.answer("Source action failed", show_alert=True)


async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await require_admin(update, context, _settings(context)):
        return
    try:
        _, action, raw_id = query.data.split(":")
        with _sessions(context)() as session:
            draft = session.get(Draft, int(raw_id))
            if not draft:
                raise WorkflowError("Draft not found")
            if action == "preview":
                text = (draft.edited_text or draft.generated_text)[:3500]
                keyboard = None
                if draft.draft_state == "pending_approval":
                    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data=f"draft:approve:{draft.id}"),
                                                      InlineKeyboardButton("Reject", callback_data=f"draft:reject:{draft.id}")]])
                await query.answer()
                await query.edit_message_text(f"Draft #{draft.id} [{draft.draft_state}]\n\n{text}", reply_markup=keyboard)
                return
            if action == "approve":
                approve_draft(session, draft, query.from_user.id)
                result = "approved"
            else:
                reject_draft(session, draft, query.from_user.id, "Rejected by administrator")
                result = "rejected"
            session.commit()
        await query.answer(f"Draft {result}")
        await query.edit_message_text(f"Draft #{raw_id} {result}.")
    except Exception:
        logger.error("Draft callback failed", extra={"error_type": "draft_action_failed"})
        await query.answer("Draft action failed", show_alert=True)


async def draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    if not context.args:
        await update.effective_message.reply_text("Provide a draft ID.")
        return
    command = update.effective_message.text.split(maxsplit=1)[0].split("@")[0].lstrip("/")
    raw = update.effective_message.text.split(maxsplit=1)
    if command == "regenerate":
        config = _settings(context)
        sessions = _sessions(context)
        draft_id = int(context.args[0])
        actor_id = update.effective_user.id
        def regenerate() -> int:
            with sessions() as worker_session:
                draft = worker_session.get(Draft, draft_id)
                if not draft or draft.draft_state != "pending_approval":
                    raise WorkflowError("Only pending drafts can be regenerated")
                article = worker_session.get(Article, draft.article_id)
                if not article:
                    raise WorkflowError("Source article not found")
                from app.workflow import audit, create_draft
                replacement = create_draft(worker_session, article, config,
                                           ignore_draft_ids=(draft.id,))
                draft.draft_state = "cancelled"
                draft.updated_at = dump_datetime(utc_now())
                audit(worker_session, "draft_regeneration_requested", "draft", draft.id, actor_id)
                worker_session.commit()
                return replacement.id
        try:
            replacement_id = await asyncio.to_thread(regenerate)
        except Exception:
            logger.error("Draft regeneration failed", extra={"error_type": "regeneration_failed"})
            await update.effective_message.reply_text("Regeneration failed safely; check budget and article-text retention.")
            return
        await update.effective_message.reply_text(f"Replacement draft #{replacement_id} created for review.")
        return
    with _sessions(context)() as session:
        draft = session.get(Draft, int(context.args[0]))
        if not draft:
            await update.effective_message.reply_text("Draft not found.")
            return
        if command == "approve":
            approve_draft(session, draft, update.effective_user.id)
        elif command == "reject":
            reject_draft(session, draft, update.effective_user.id, " ".join(context.args[1:]) or "Rejected by administrator")
        elif command == "edit":
            if len(raw) < 2 or len(context.args) < 2:
                await update.effective_message.reply_text("Usage: /edit ID replacement text")
                return
            remainder = raw[1].split(maxsplit=1)
            edit_draft(session, draft, update.effective_user.id, remainder[1])
        else:
            raise WorkflowError("Unsupported draft action")
        session.commit()
    await update.effective_message.reply_text(f"Draft #{context.args[0]} updated.")


async def reconcile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    args = context.args
    if (len(args) == 2 and args[1] == "absent"):
        exists = False
        message_id = None
    elif (len(args) == 3 and args[1] == "found"
          and args[2].isascii() and args[2].isdecimal() and len(args[2]) <= 20
          and 0 < int(args[2]) <= 2**63 - 1):
        exists = True
        message_id = args[2]
    else:
        await update.effective_message.reply_text("Usage: /reconcile ATTEMPT_ID found MESSAGE_ID | absent")
        return
    try:
        if (not args[0].isascii() or not args[0].isdecimal() or len(args[0]) > 20
                or not 0 < int(args[0]) <= 2**63 - 1):
            raise ValueError("Attempt ID must be a positive integer")
        attempt_id = int(args[0])
        with _sessions(context)() as session:
            attempt = session.get(PublicationAttempt, attempt_id)
            if not attempt:
                raise ValueError("Attempt not found")
            reconcile_unknown(session, attempt, update.effective_user.id,
                              exists_in_channel=exists, message_id=message_id)
        await update.effective_message.reply_text("Reconciliation recorded. An absent result allows only a new explicit operator-authorized attempt.")
    except Exception:
        await update.effective_message.reply_text("Could not reconcile that attempt. Check its ID and current state.")


async def retry_article_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /retry ARTICLE_ID")
        return
    try:
        with _sessions(context)() as session:
            article = session.get(Article, int(context.args[0]))
            if not article:
                raise WorkflowError("Article not found")
            from app.workflow import retry_article
            retry_article(session, article, update.effective_user.id)
            retry_count = article_retry_count(session, article.id) + 1
            session.commit()
        await update.effective_message.reply_text(
            f"Article #{context.args[0]} returned to the processing queue (retry {retry_count}/{MAX_ARTICLE_RETRIES}).")
    except Exception:
        await update.effective_message.reply_text("Only a failed article can be returned to the queue.")


async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context, _settings(context)):
        return
    topic = " ".join(context.args).strip()
    if not topic:
        await update.effective_message.reply_text("Usage: /discover topic")
        return
    try:
        config = _settings(context)
        sessions = _sessions(context)
        actor_id = update.effective_user.id
        def perform_discovery() -> int:
            with sessions() as session:
                suggestions = discover_sources(session, config, topic)
                ids: list[int] = []
                for item in suggestions.candidates:
                    try:
                        source = add_source_candidate(session, item.url, item.source_type,
                                                      item.name, actor_id=actor_id)
                        ids.append(source.id)
                    except ValueError:
                        continue
                session.commit()
                return len(ids)
        count = await asyncio.to_thread(perform_discovery)
        await update.effective_message.reply_text(
            f"Added {count} quarantined candidates. Review and validate them with /sources."
        )
    except Exception:
        logger.error("AI-assisted source discovery failed", extra={"error_type": "discovery_failed"})
        await update.effective_message.reply_text(
            "Discovery failed or quota is uncertain. Generation may be paused; check /status."
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled Telegram handler failure", extra={
        "error_type": type(context.error).__name__ if context.error else "UnknownError"
    })
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("The request failed safely. An administrator can inspect operational logs.")
        except Exception:
            logger.error("Could not send handler error reply")


def build_application(settings: Settings, sessions, *, post_init=None, post_shutdown=None) -> Application:
    builder = Application.builder().token(settings.telegram_bot_token)
    if post_init:
        builder = builder.post_init(post_init)
    if post_shutdown:
        builder = builder.post_shutdown(post_shutdown)
    app = builder.build()
    app.bot_data["settings"] = settings
    app.bot_data["sessions"] = sessions
    commands = [
        ("start", start), ("help", help_command), ("status", status), ("pause", pause),
        ("resume", resume), ("quota_resume", quota_resume),
        ("stats", stats_command), ("sources", sources_command),
        ("queue", queue_command), ("settings", settings_command), ("addsource", add_source),
        ("approve", draft_command), ("reject", draft_command), ("edit", draft_command),
        ("regenerate", draft_command), ("reconcile", reconcile_command),
        ("discover", discover_command),
        ("retry", retry_article_command),
    ]
    for command, callback in commands:
        app.add_handler(CommandHandler(command, callback))
    app.add_handler(CallbackQueryHandler(draft_callback, pattern=r"^draft:"))
    app.add_handler(CallbackQueryHandler(source_callback, pattern=r"^source:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, submit_url))
    app.add_error_handler(error_handler)
    return app

