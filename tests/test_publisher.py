from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.database.models import Publication, PublicationAttempt
from app.publisher import (AmbiguousPublishError, DefinitePublishError, PublishBlocked,
                           normalize_retry_after, publish_due, reconcile_unknown,
                           recover_stale_attempts)
from app.timeutil import dump_datetime, utc_now
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
            assert draft.draft_state == "cancelled"
            assert session.scalar(select(Publication).where(Publication.draft_id == draft.id)) is None
    asyncio.run(run())


def test_telegram_retry_after_is_persisted_and_honored(sessions, monkeypatch):
    async def run():
        fixed_now = utc_now()
        monkeypatch.setattr("app.publisher.utc_now", lambda: fixed_now)
        settings = Settings(_env_file=None, publish_retry_limit=3, retry_base_seconds=1)
        with sessions() as session:
            draft = setup_scheduled(session)

            async def limited(_text):
                raise DefinitePublishError("Telegram rate limited the send", retry_after_seconds=120)

            with pytest.raises(DefinitePublishError):
                await publish_due(session, draft, settings, limited, destination_id="private-test")
            attempt = session.scalar(select(PublicationAttempt).where(
                PublicationAttempt.draft_id == draft.id))
            assert attempt.state == "failed"
            assert attempt.retry_after_seconds == 120

            async def accepted(_text):
                return "23"

            monkeypatch.setattr("app.publisher.utc_now", lambda: fixed_now + timedelta(seconds=60))
            with pytest.raises(PublishBlocked, match="backoff"):
                await publish_due(session, draft, settings, accepted, destination_id="private-test")
            monkeypatch.setattr("app.publisher.utc_now", lambda: fixed_now + timedelta(seconds=121))
            result = await publish_due(session, draft, settings, accepted, destination_id="private-test")
            assert result.state == "succeeded"
    asyncio.run(run())


@pytest.mark.parametrize("value,expected", [
    (30, 30.0), (1.5, 1.5), (timedelta(seconds=4), 4.0),
    (True, None), (-1, None), (float("inf"), None), ("bad", None),
])
def test_retry_after_value_is_normalized_safely(value, expected):
    assert normalize_retry_after(value) == expected


@pytest.mark.parametrize("message_id", [None, "None", "", "0", "-1", "１２"])
def test_malformed_success_response_is_unknown_not_published(sessions, message_id):
    async def run():
        with sessions() as session:
            draft = setup_scheduled(session)

            async def malformed(_text):
                return message_id

            with pytest.raises(AmbiguousPublishError):
                await publish_due(session, draft, Settings(_env_file=None), malformed,
                                  destination_id="private-test")
            attempt = session.scalar(select(PublicationAttempt).where(
                PublicationAttempt.draft_id == draft.id))
            assert attempt.state == "unknown"
            assert session.scalar(select(Publication).where(Publication.draft_id == draft.id)) is None
    asyncio.run(run())


def test_rejected_draft_cannot_be_published(sessions):
    async def run():
        calls = []
        with sessions() as session:
            draft = setup_scheduled(session)
            draft.draft_state = "rejected"
            draft.rejected_by = 123
            draft.rejected_at = dump_datetime(utc_now())
            draft.rejection_reason = "Rejected during test"
            session.commit()

            async def send(_text):
                calls.append(True)
                return "10"

            with pytest.raises(PublishBlocked):
                await publish_due(session, draft, Settings(_env_file=None), send,
                                  destination_id="private-test")
            assert not calls
            assert session.scalar(select(PublicationAttempt).where(
                PublicationAttempt.draft_id == draft.id)) is None
    asyncio.run(run())


def test_edit_after_approval_is_rejected_without_changing_approved_content(sessions):
    from app.workflow import WorkflowError, edit_draft

    with sessions() as session:
        draft = setup_scheduled(session)
        before = (draft.edited_text, draft.generated_text, draft.approved_by, draft.approved_at,
                  draft.draft_state)
        with pytest.raises(WorkflowError, match="pending drafts"):
            edit_draft(session, draft, 1, "New edited text that must not silently retain old approval.")
        assert (draft.edited_text, draft.generated_text, draft.approved_by, draft.approved_at,
                draft.draft_state) == before


def test_interrupted_send_is_unknown_after_restart_and_requires_reconciliation(sessions):
    async def crash_at_send_boundary():
        with sessions() as first_session:
            draft = setup_scheduled(first_session)

            async def process_crash(_text):
                raise SystemExit("simulated process termination after attempt commit")

            with pytest.raises(SystemExit):
                await publish_due(first_session, draft, Settings(_env_file=None), process_crash,
                                  destination_id="private-test")
            draft_id = draft.id

        with sessions() as restarted_session:
            assert recover_stale_attempts(restarted_session) == [1]
            draft = restarted_session.get(type(draft), draft_id)
            attempt = restarted_session.scalar(select(PublicationAttempt).where(
                PublicationAttempt.draft_id == draft_id))
            assert attempt.state == "unknown"
            send_calls = []

            async def send(_text):
                send_calls.append(True)
                return "456"

            with pytest.raises(PublishBlocked):
                await publish_due(restarted_session, draft, Settings(_env_file=None), send,
                                  destination_id="private-test")
            assert not send_calls
            reconcile_unknown(restarted_session, attempt, 1, exists_in_channel=False)
            result = await publish_due(restarted_session, draft, Settings(_env_file=None), send,
                                       destination_id="private-test")
            assert result.state == "succeeded"
            assert len(send_calls) == 1
    asyncio.run(crash_at_send_boundary())


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
