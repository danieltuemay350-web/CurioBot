from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.database.models import Article, ArticleText, Source
from app.ingestion import DuplicateArticleError, extract_article, poll_rss, submit_article_url
from app.security.urls import FetchedResponse
from app.timeutil import dump_datetime, utc_now


def test_submitted_url_is_deduplicated_and_source_quarantined(sessions):
    with sessions() as session:
        config = Settings(_env_file=None)
        article, created = submit_article_url(session, "https://Example.com/story#part", config, 100)
        session.commit()
        assert created is True
        source = session.get(Source, article.source_id)
        assert source.lifecycle_state == "quarantined" and not source.enabled
        duplicate, created_again = submit_article_url(session, "https://example.com/story", config, 100)
        assert created_again is False
        assert duplicate.id == article.id


def test_rss_parser_uses_same_article_persistence(sessions, monkeypatch):
    import app.ingestion as ingestion
    now = dump_datetime(utc_now())
    with sessions() as session:
        source = Source(name="Feed", source_type="rss", canonical_url="https://feed.example/rss",
                        lifecycle_state="active", enabled=True, created_at=now, updated_at=now)
        session.add(source)
        session.flush()
        xml = b"""<?xml version='1.0'?><rss version='2.0'><channel><title>Feed</title>
        <item><title>Story</title><link>https://example.com/story</link></item></channel></rss>"""
        monkeypatch.setattr(ingestion, "safe_fetch", lambda *_a, **_k: FetchedResponse(source.canonical_url, 200, "application/rss+xml", xml))
        assert poll_rss(session, source, Settings(_env_file=None)) == 1
        session.commit()
        assert poll_rss(session, source, Settings(_env_file=None)) == 0
        assert session.scalar(select(Article).where(Article.canonical_url == "https://example.com/story"))


def test_extracted_text_retention_and_content_dedup(sessions, monkeypatch):
    import app.ingestion as ingestion
    config = Settings(_env_file=None, article_text_retention_days=3)
    body_text = "This is the same article content with enough words to make a readable extraction. " * 10
    html = f"<html><title>Title</title><body><article>{body_text}</article></body></html>".encode()
    monkeypatch.setattr(ingestion, "safe_fetch", lambda url, *_a, **_k: FetchedResponse(url, 200, "text/html", html))
    with sessions() as session:
        now = dump_datetime(utc_now())
        first = Article(original_url="https://example.com/one", canonical_url="https://example.com/one",
                        content_hash=hashlib.sha256(b"one").hexdigest(), ingest_kind="admin_url",
                        status="discovered", discovered_at=now, created_at=now, updated_at=now)
        second = Article(original_url="https://example.org/two", canonical_url="https://example.org/two",
                         content_hash=hashlib.sha256(b"two").hexdigest(), ingest_kind="admin_url",
                         status="discovered", discovered_at=now, created_at=now, updated_at=now)
        session.add_all([first, second])
        session.flush()
        row = extract_article(session, first, config)
        assert datetime.fromisoformat(row.expires_at) - datetime.fromisoformat(row.stored_at) == timedelta(days=3)
        with pytest.raises(DuplicateArticleError):
            extract_article(session, second, config)
        assert session.get(ArticleText, second.id) is None


def test_retention_cleanup_deletes_only_expired_extracted_text(sessions):
    from app.retention import cleanup_expired_text
    now = utc_now()
    with sessions() as session:
        for index, expires in enumerate((now - timedelta(seconds=1), now + timedelta(days=1)), start=1):
            article = Article(original_url=f"https://example.com/{index}",
                              canonical_url=f"https://example.com/{index}",
                              content_hash=hashlib.sha256(str(index).encode()).hexdigest(),
                              ingest_kind="admin_url", status="extracted",
                              discovered_at=dump_datetime(now), created_at=dump_datetime(now),
                              updated_at=dump_datetime(now))
            session.add(article)
            session.flush()
            session.add(ArticleText(article_id=article.id, extracted_text="private source text",
                                    stored_at=dump_datetime(now - timedelta(days=10)),
                                    expires_at=dump_datetime(expires)))
        session.commit()
        assert cleanup_expired_text(session) == 1
        assert len(session.scalars(select(Article)).all()) == 2
        assert len(session.scalars(select(ArticleText)).all()) == 1


def test_source_candidate_requires_validate_approve_activate(sessions):
    from app.ingestion import add_source_candidate, transition_source
    with sessions() as session:
        source = add_source_candidate(session, "https://sources.example/feed.xml", "rss", "Candidate")
        assert source.lifecycle_state == "quarantined" and not source.enabled
        with pytest.raises(ValueError, match="Invalid source"):
            transition_source(session, source, "approve", 55)
        transition_source(session, source, "validate", 55)
        transition_source(session, source, "approve", 55)
        assert source.lifecycle_state == "approved" and source.approved_by == 55
        transition_source(session, source, "activate", 55)
        assert source.lifecycle_state == "active" and source.enabled
        transition_source(session, source, "disable", 55)
        assert source.lifecycle_state == "disabled" and not source.enabled


def test_permanent_extraction_rejection_is_terminal_and_preserves_reason(sessions, monkeypatch):
    from app.ingestion import PermanentArticleRejection
    from app.workflow import retry_article, WorkflowError
    import app.ingestion as ingestion
    monkeypatch.setattr(ingestion, "safe_fetch", lambda url, *_args, **_kwargs:
                        FetchedResponse(url, 200, "text/html", b"<html><title>Short</title><p>Too short.</p></html>"))
    with sessions() as session:
        now = dump_datetime(utc_now())
        article = Article(original_url="https://example.test/short", canonical_url="https://example.test/short",
                          content_hash=hashlib.sha256(b"short").hexdigest(), ingest_kind="admin_url",
                          status="discovered", discovered_at=now, created_at=now, updated_at=now)
        session.add(article)
        session.flush()
        with pytest.raises(PermanentArticleRejection):
            extract_article(session, article, Settings(_env_file=None))
        assert article.status == "rejected"
        assert article.rejection_reason == "insufficient_extracted_text"
        with pytest.raises(WorkflowError, match="retryable"):
            retry_article(session, article, actor_id=55)
