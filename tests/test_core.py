from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.database.models import Article, Draft, Source
from app.database.session import make_session_factory
from app.timeutil import LOCAL_ZONE, dump_datetime, load_datetime
from app.workflow import WorkflowError, approve_draft


def test_utc_round_trip_and_addis_conversion():
    original = datetime(2025, 1, 1, 12, 30, tzinfo=UTC)
    assert load_datetime(dump_datetime(original)) == original
    assert original.astimezone(LOCAL_ZONE).hour == 15
    with pytest.raises(ValueError):
        dump_datetime(datetime(2025, 1, 1))


def test_settings_bounds_and_credentials():
    settings = Settings(_env_file=None)
    assert settings.article_text_retention_days == 7
    with pytest.raises(ValueError, match="missing or invalid"):
        settings.require_runtime_credentials()
    with pytest.raises(ValueError):
        Settings(_env_file=None, article_text_retention_days=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, publication_window_start="bad")
    with pytest.raises(ValueError):
        Settings(_env_file=None, database_url="sqlite:///:memory:")


def sample_rows(session):
    now = dump_datetime(datetime.now(UTC))
    source = Source(name="Sample", source_type="webpage", canonical_url="https://example.com/",
                    lifecycle_state="quarantined", enabled=False, created_at=now, updated_at=now)
    session.add(source)
    session.flush()
    article = Article(source_id=source.id, original_url="https://example.com/a",
                      canonical_url="https://example.com/a", content_hash="a" * 64,
                      ingest_kind="admin_url", status="extracted", discovered_at=now,
                      created_at=now, updated_at=now, title="Sample")
    session.add(article)
    session.flush()
    draft = Draft(article_id=article.id, generated_text="Good post", category="Science",
                  generation_model="configured", content_hash="b" * 64,
                  draft_state="pending_approval", created_at=now, updated_at=now)
    session.add(draft)
    session.flush()
    return source, article, draft


def test_foreign_keys_unique_urls_and_state_constraints(sessions):
    with sessions() as session:
        source, article, draft = sample_rows(session)
        session.commit()
        assert session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        with pytest.raises(IntegrityError):
            session.add(Article(source_id=source.id, original_url="https://elsewhere/",
                                canonical_url=article.canonical_url, content_hash="c" * 64,
                                ingest_kind="rss", status="discovered", discovered_at=dump_datetime(datetime.now(UTC)),
                                created_at=dump_datetime(datetime.now(UTC)), updated_at=dump_datetime(datetime.now(UTC))))
            session.commit()
        session.rollback()
        approve_draft(session, draft, actor_id=42)
        with pytest.raises(WorkflowError):
            approve_draft(session, draft, actor_id=42)
        assert session.scalar(select(Draft).where(Draft.id == draft.id)).draft_state == "approved"


def test_approval_requires_pending_state(sessions):
    with sessions() as session:
        _, _, draft = sample_rows(session)
        approve_draft(session, draft, 123)
        assert draft.approved_by == 123
        with pytest.raises(WorkflowError):
            approve_draft(session, draft, 123)


def test_failed_article_retry_is_admin_audited(sessions):
    from app.workflow import retry_article
    with sessions() as session:
        _, article, _ = sample_rows(session)
        article.status = "failed"
        article.rejection_reason = "network error"
        retry_article(session, article, 77)
        assert article.status == "discovered"
        assert article.rejection_reason == "network error"
        with pytest.raises(WorkflowError):
            retry_article(session, article, 77)
