from __future__ import annotations

from datetime import timedelta

import pytest

from app.ai.gemini import InvalidModelOutput
from app.database.models import ArticleText, Publication
from app.timeutil import dump_datetime, utc_now
from app.workflow import (MAX_ARTICLE_RETRIES, WorkflowError, record_article_failure,
                          retry_article)
from tests.test_core import sample_rows


@pytest.mark.parametrize("error", [
    InvalidModelOutput("bad JSON"),
    WorkflowError("Post is too similar to an existing draft"),
    RuntimeError("temporary generation failure"),
])
def test_generation_and_quality_failures_remain_retryable(sessions, error):
    with sessions() as session:
        _, article, _ = sample_rows(session)
        article.status = "extracted"
        article.rejection_reason = "original_generation_issue"
        record_article_failure(session, article, error, phase="generation")
        session.commit()
        assert article.status == "failed"
        assert article.rejection_reason == "original_generation_issue"


def test_retry_returns_failed_article_to_available_extracted_text(sessions):
    with sessions() as session:
        _, article, _ = sample_rows(session)
        now = utc_now()
        article.status = "failed"
        article.rejection_reason = "generation:InvalidModelOutput"
        session.add(ArticleText(article_id=article.id, extracted_text="retained text",
                                stored_at=dump_datetime(now),
                                expires_at=dump_datetime(now + timedelta(days=1))))
        session.flush()
        retry_article(session, article, actor_id=42)
        session.commit()
        assert article.status == "extracted"
        assert article.rejection_reason == "generation:InvalidModelOutput"


def test_retry_limit_stops_repeated_generation_failure(sessions):
    with sessions() as session:
        _, article, _ = sample_rows(session)
        now = utc_now()
        article.status = "failed"
        session.add(ArticleText(article_id=article.id, extracted_text="retained text",
                                stored_at=dump_datetime(now),
                                expires_at=dump_datetime(now + timedelta(days=1))))
        session.flush()
        for _ in range(MAX_ARTICLE_RETRIES):
            retry_article(session, article, actor_id=42)
            record_article_failure(session, article, RuntimeError("still failing"), phase="generation")
        session.commit()
        with pytest.raises(WorkflowError, match="retry limit"):
            retry_article(session, article, actor_id=42)
        assert article.status == "failed"
        assert article.rejection_reason == "generation:RuntimeError"


def test_confirmed_published_article_cannot_be_retried(sessions):
    with sessions() as session:
        _, article, draft = sample_rows(session)
        article.status = "failed"  # Even inconsistent status cannot override publication history.
        draft.draft_state, draft.approved_by, draft.approved_at = "published", 42, dump_datetime(utc_now())
        session.add(Publication(draft_id=draft.id, destination_id="test",
                                telegram_message_id="already-published",
                                published_at=dump_datetime(utc_now())))
        session.commit()
        with pytest.raises(WorkflowError, match="confirmed publication"):
            retry_article(session, article, actor_id=42)
