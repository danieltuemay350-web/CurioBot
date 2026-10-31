"""RSS ingestion, URL submissions, and source lifecycle operations."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import timedelta

import feedparser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.models import Article, ArticleText, AuditEvent, Source
from app.security.urls import FetchLimits, normalize_url, safe_fetch
from app.timeutil import dump_datetime, utc_now


class DuplicateArticleError(ValueError):
    """Extracted article body has already been ingested."""


class PermanentArticleRejection(ValueError):
    """Fetched content cannot be accepted as an article."""


def _utc_source_datetime(value: str | None) -> str | None:
    if not value:
        return None
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return dump_datetime(parsed)


def _audit(session: Session, event_type: str, target_type: str, target_id: int | str,
           actor_id: int | None = None, details: str = "{}") -> None:
    session.add(AuditEvent(event_type=event_type, occurred_at=dump_datetime(utc_now()),
                           actor_type="admin" if actor_id else "system",
                           actor_id=str(actor_id) if actor_id else None,
                           target_type=target_type, target_id=str(target_id), details=details))


def source_domain(url: str) -> str:
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or "Unknown source"
    return f"[{host}]" if ":" in host else host


def submit_article_url(session: Session, url: str, settings: Settings, actor_id: int) -> tuple[Article, bool]:
    normalized = normalize_url(url)
    existing = session.scalar(select(Article).where(Article.canonical_url == normalized))
    if existing:
        return existing, False
    now = utc_now()
    # Submitted URLs do not automatically activate a source. A webpage source candidate
    # is quarantined and must be reviewed before it can become an RSS/web source.
    host = source_domain(normalized)
    source = session.scalar(select(Source).where(Source.canonical_url == f"https://{host}/"))
    if source is None:
        source = Source(name=host, source_type="webpage", canonical_url=f"https://{host}/",
                        lifecycle_state="quarantined", enabled=False,
                        created_at=dump_datetime(now), updated_at=dump_datetime(now))
        try:
            with session.begin_nested():
                session.add(source)
                session.flush()
        except IntegrityError:
            source = session.scalar(select(Source).where(Source.canonical_url == f"https://{host}/"))
    article = Article(source_id=source.id if source else None, original_url=url,
                      canonical_url=normalized, content_hash=hashlib.sha256(normalized.encode()).hexdigest(),
                      ingest_kind="admin_url", status="discovered", discovered_at=dump_datetime(now),
                      created_at=dump_datetime(now), updated_at=dump_datetime(now))
    try:
        with session.begin_nested():
            session.add(article)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(Article).where(Article.canonical_url == normalized))
        if existing:
            return existing, False
        raise
    _audit(session, "article_submitted", "article", article.id, actor_id)
    return article, True


def _extract(html: str) -> tuple[str | None, str | None, str | None, str]:
    try:
        import trafilatura
        metadata = trafilatura.extract_metadata(html)
        text = trafilatura.extract(html, include_comments=False, include_tables=False,
                                   favor_precision=True)
        if text:
            return (metadata.title if metadata else None,
                    metadata.author if metadata else None,
                    _utc_source_datetime(metadata.date if metadata else None),
                    text.strip())
    except (ImportError, ValueError, TypeError):
        # Extraction failures are expected on malformed/unsupported pages; caller gets a
        # safe rejection rather than exposing parser internals.
        pass
    from html.parser import HTMLParser

    class TextParser(HTMLParser):
        skip = False
        chunks: list[str]
        title_mode = False
        title: list[str]

        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.chunks = []
            self.title = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag in {"script", "style", "noscript", "svg"}:
                self.skip = True
            if tag == "title":
                self.title_mode = True

        def handle_endtag(self, tag: str) -> None:
            if tag in {"script", "style", "noscript", "svg"}:
                self.skip = False
            if tag == "title":
                self.title_mode = False

        def handle_data(self, data: str) -> None:
            if self.title_mode:
                self.title.append(data)
            if not self.skip and data.strip():
                self.chunks.append(data.strip())

    parser = TextParser()
    parser.feed(html)
    return (" ".join(parser.title).strip() or None, None, None,
            re.sub(r"\s+", " ", " ".join(parser.chunks)).strip())


def extract_article(session: Session, article: Article, settings: Settings, *, resolver=None) -> ArticleText:
    limits = FetchLimits(settings.http_connect_timeout_seconds, settings.http_read_timeout_seconds,
                         settings.http_max_response_bytes, settings.http_max_redirects,
                         settings.allowed_ports)
    fetch_kwargs = {"resolver": resolver} if resolver else {}
    try:
        fetched = safe_fetch(article.original_url, limits, **fetch_kwargs)
    except Exception as exc:
        article.status = "failed"
        article.rejection_reason = type(exc).__name__
        article.updated_at = dump_datetime(utc_now())
        raise
    title, author, published_at, text = _extract(fetched.body.decode("utf-8", errors="replace"))
    if len(text) < 200:
        article.status = "rejected"
        article.rejection_reason = "insufficient_extracted_text"
        raise PermanentArticleRejection("The page did not contain enough readable article text")
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    duplicate = session.scalar(select(Article.id).where(
        Article.content_hash == content_hash, Article.id != article.id))
    if duplicate is not None:
        article.status = "rejected"
        article.rejection_reason = "duplicate_article_content"
        article.updated_at = dump_datetime(utc_now())
        raise DuplicateArticleError("Article content duplicates an existing item")
    fetched_canonical = normalize_url(fetched.url)
    url_duplicate = session.scalar(select(Article.id).where(
        Article.canonical_url == fetched_canonical, Article.id != article.id))
    if url_duplicate is not None:
        article.status = "rejected"
        article.rejection_reason = "duplicate_redirect_target"
        article.updated_at = dump_datetime(utc_now())
        raise DuplicateArticleError("Redirect target has already been ingested")
    now = utc_now()
    article.canonical_url = fetched_canonical
    article.title = title or article.title
    article.author = author or article.author
    article.published_at = published_at or article.published_at
    article.status = "extracted"
    article.updated_at = dump_datetime(now)
    article.content_hash = content_hash
    row = ArticleText(article_id=article.id, extracted_text=text[:250_000], stored_at=dump_datetime(now),
                      expires_at=dump_datetime(now + timedelta(days=settings.article_text_retention_days)))
    old = session.get(ArticleText, article.id)
    if old:
        old.extracted_text = row.extracted_text
        old.stored_at = row.stored_at
        old.expires_at = row.expires_at
        return old
    session.add(row)
    return row


def add_source_candidate(session: Session, url: str, source_type: str, name: str,
                         *, actor_id: int | None = None) -> Source:
    if source_type not in {"rss", "webpage"}:
        raise ValueError("Source type must be rss or webpage")
    canonical = normalize_url(url)
    source = session.scalar(select(Source).where(Source.canonical_url == canonical))
    if source:
        return source
    now = dump_datetime(utc_now())
    source = Source(name=name[:300], source_type=source_type, canonical_url=canonical,
                    lifecycle_state="quarantined", enabled=False, created_at=now, updated_at=now)
    session.add(source)
    session.flush()
    _audit(session, "source_candidate_added", "source", source.id, actor_id)
    return source


def transition_source(session: Session, source: Source, action: str, actor_id: int,
                      reason: str | None = None) -> None:
    allowed = {"validate": {"quarantined"}, "approve": {"validated"},
               "reject": {"quarantined", "validated", "approved"},
               "activate": {"approved"}, "disable": {"active", "approved"}}
    if action not in allowed or source.lifecycle_state not in allowed[action]:
        raise ValueError("Invalid source state transition")
    if action == "reject" and not reason:
        raise ValueError("A rejection reason is required")
    now = dump_datetime(utc_now())
    if action == "validate":
        source.lifecycle_state = "validated"
    elif action == "approve":
        source.lifecycle_state, source.approved_by = "approved", actor_id
    elif action == "activate":
        source.lifecycle_state, source.enabled = "active", True
    elif action == "disable":
        source.lifecycle_state, source.enabled = "disabled", False
    else:
        source.lifecycle_state, source.enabled, source.rejection_reason = "rejected", False, reason
    source.updated_at = now
    _audit(session, f"source_{action}d", "source", source.id, actor_id)


def poll_rss(session: Session, source: Source, settings: Settings) -> int:
    limits = FetchLimits(settings.http_connect_timeout_seconds, settings.http_read_timeout_seconds,
                         settings.http_max_response_bytes, settings.http_max_redirects, settings.allowed_ports)
    fetched = safe_fetch(source.canonical_url, limits)
    parsed = feedparser.parse(fetched.body)
    if parsed.bozo and not parsed.entries:
        raise ValueError("RSS feed could not be parsed")
    created = 0
    for entry in parsed.entries[:100]:
        raw_url = entry.get("link")
        if not raw_url:
            continue
        normalized = normalize_url(raw_url)
        if session.scalar(select(Article.id).where(Article.canonical_url == normalized)):
            continue
        now = dump_datetime(utc_now())
        article = Article(source_id=source.id, original_url=raw_url, canonical_url=normalized,
                          content_hash=hashlib.sha256(normalized.encode()).hexdigest(), ingest_kind="rss",
                          title=(entry.get("title") or "")[:1000] or None,
                          author=(entry.get("author") or "")[:500] or None,
                          published_at=(time.strftime("%Y-%m-%dT%H:%M:%SZ", entry.published_parsed)
                                        if entry.get("published_parsed") else None),
                          discovered_at=now, status="discovered", created_at=now, updated_at=now)
        try:
            with session.begin_nested():
                session.add(article)
                session.flush()
            created += 1
        except IntegrityError:
            continue
    source.last_checked_at = dump_datetime(utc_now())
    return created
