from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.database.models import Publication, PublicationAttempt
from app.publisher import AmbiguousPublishError, DefinitePublishError, publish_due, reconcile_unknown
from app.timeutil import dump_datetime
from tests.test_core import sample_rows


def setup_scheduled(session):
    _, _, draft = sample_rows(session)
    now = dump_datetime(datetime.now(UTC) - timedelta(minutes=1))
    draft.draft_state, draft.approved_by, draft.approved_at, draft.scheduled_at = "scheduled", 1, now, now
    session.commit()
    return draft


def test_success_persists_attempt_and_publication(sessions):
    async def run():
        settings = Settings(_env_file=None, publication_daily_limit=5)
        with sessions() as session:
            draft = setup_scheduled(session)
            async def send(_text):
                return "12345"
            attempt = await publish_due(session, draft, settings, send, destination_id="private-test")
            assert attempt.state == "succeeded"
            assert session.scalar(select(Publication).where(Publication.draft_id == draft.id))
            assert draft.draft_state == "published"
    asyncio.run(run())


def test_ambiguous_send_is_unknown_and_never_retried(sessions):
    async def run():
        settings = Settings(_env_file=None)
        with sessions() as session:
            draft = setup_scheduled(session)
            async def send(_text):
                raise TimeoutError("timeout")
            with pytest.raises(AmbiguousPublishError):
                await publish_due(session, draft, settings, send, destination_id="private-test")
            attempt = session.scalar(select(PublicationAttempt).where(PublicationAttempt.draft_id == draft.id))
            assert attempt.state == "unknown"
            with pytest.raises(Exception, match="manual reconciliation"):
                await publish_due(session, draft, settings, send, destination_id="private-test")
            reconcile_unknown(session, attempt, 1, exists_in_channel=False)
            async def accepted(_text):
                return "22"
            new_attempt = await publish_due(session, draft, settings, accepted, destination_id="private-test")
            assert new_attempt.attempt_number == 2
    asyncio.run(run())


def test_definite_failure_is_bounded(sessions):
    async def run():
        settings = Settings(_env_file=None, publish_retry_limit=1)
        with sessions() as session:
            draft = setup_scheduled(session)
            async def send(_text):
                raise DefinitePublishError("rejected")
            with pytest.raises(DefinitePublishError):
                await publish_due(session, draft, settings, send, destination_id="private-test")
            attempt = session.scalar(select(PublicationAttempt).where(PublicationAttempt.draft_id == draft.id))
            assert attempt.state == "failed"
            with pytest.raises(Exception, match="retry limit"):
                await publish_due(session, draft, settings, send, destination_id="private-test")
    asyncio.run(run())


def test_atomic_claim_prevents_two_publishers(sessions):
    async def run():
        settings = Settings(_env_file=None)
        with sessions() as first_session, sessions() as second_session:
            draft = setup_scheduled(first_session)
            second_draft = second_session.get(type(draft), draft.id)
            started = asyncio.Event()
            release = asyncio.Event()
            async def slow_send(_text):
                started.set()
                await release.wait()
                return "99"
            async def second_send(_text):
                return "100"
            first_task = asyncio.create_task(publish_due(first_session, draft, settings, slow_send,
                                                         destination_id="private-test"))
            await started.wait()
            from app.publisher import PublishBlocked
            with pytest.raises(PublishBlocked, match="manual reconciliation|claimed"):
                await publish_due(second_session, second_draft, settings, second_send,
                                  destination_id="private-test")
            release.set()
            await first_task
    asyncio.run(run())
