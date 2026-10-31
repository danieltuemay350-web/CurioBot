from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import Settings
from app.database.models import Article, AuditEvent, Draft, Publication, PublicationAttempt
from app.publisher import recover_stale_attempts
from app.timeutil import LOCAL_ZONE, dump_datetime, utc_now
from app.workflow import approve_draft, schedule_approved_drafts, within_publication_window
from tests.test_core import sample_rows


def test_schedule_spaces_posts_and_respects_daily_cap(sessions):
    config = Settings(_env_file=None, publication_daily_limit=2,
                      publication_window_start="00:01", publication_window_end="23:59",
                      catchup_min_interval_minutes=180)
    with sessions() as session:
        _, _, first = sample_rows(session)
        approve_draft(session, first, 1)
        now = dump_datetime(utc_now())
        article = Article(original_url="https://example.com/other", canonical_url="https://example.com/other",
                          content_hash="d" * 64, ingest_kind="admin_url", status="extracted",
                          discovered_at=now, created_at=now, updated_at=now, title="Other")
        session.add(article)
        session.flush()
        second = Draft(article_id=article.id, generated_text="Another useful post", category="History",
                       generation_model="configured", content_hash="e" * 64, draft_state="pending_approval",
                       created_at=now, updated_at=now)
        session.add(second)
        session.flush()
        approve_draft(session, second, 1)
        clock = datetime(2025, 1, 1, 7, tzinfo=UTC)
        assert schedule_approved_drafts(session, config, now=clock) == 2
        assert first.scheduled_at and second.scheduled_at
        assert datetime.fromisoformat(second.scheduled_at) - datetime.fromisoformat(first.scheduled_at) == timedelta(minutes=180)
        session.commit()
        assert schedule_approved_drafts(session, config, now=clock) == 0


def test_schedule_capacity_is_independent_for_three_local_days(sessions):
    config = Settings(_env_file=None, publication_daily_limit=2,
                      publication_window_start="08:00", publication_window_end="22:00",
                      catchup_min_interval_minutes=360, max_overdue_age_hours=168)
    clock = datetime(2025, 1, 1, 5, tzinfo=UTC)  # 08:00 in Addis Ababa.
    with sessions() as session:
        created = []
        for index in range(5):
            now = dump_datetime(clock)
            article = Article(original_url=f"https://example.com/day-{index}",
                              canonical_url=f"https://example.com/day-{index}",
                              content_hash=f"{index + 10:064x}", ingest_kind="admin_url",
                              status="processed", discovered_at=now, created_at=now,
                              updated_at=now, title=f"Article {index}")
            session.add(article)
            session.flush()
            draft = Draft(article_id=article.id, generated_text=f"Useful post {index}",
                          category=f"Topic {index}", generation_model="configured",
                          content_hash=f"{index + 100:064x}", draft_state="approved",
                          approved_by=1, approved_at=now, created_at=now, updated_at=now)
            session.add(draft)
            session.flush()
            created.append(draft)
        assert schedule_approved_drafts(session, config, now=clock) == 5
        dates = [datetime.fromisoformat(draft.scheduled_at).astimezone(LOCAL_ZONE).date()
                 for draft in created]
        assert dates == [datetime(2025, 1, day).date() for day in (1, 1, 2, 2, 3)]
        assert [sum(day == item for item in dates) for day in sorted(set(dates))] == [2, 2, 1]
        for left, right in zip(created, created[1:]):
            assert datetime.fromisoformat(right.scheduled_at) - datetime.fromisoformat(left.scheduled_at) >= timedelta(hours=6)
        assert schedule_approved_drafts(session, config, now=clock) == 0


def test_full_local_day_moves_to_future_day_and_published_day_does_not_consume_next_day(sessions):
    config = Settings(_env_file=None, publication_daily_limit=1,
                      publication_window_start="08:00", publication_window_end="22:00",
                      max_overdue_age_hours=168, catchup_min_interval_minutes=30)
    # 21:00 UTC is midnight at the start of Jan 2 in Addis Ababa.
    clock = datetime(2025, 1, 1, 21, tzinfo=UTC)
    with sessions() as session:
        _, _, first = sample_rows(session)
        now = dump_datetime(clock)
        first.draft_state, first.approved_by, first.approved_at = "approved", 1, now
        session.add(Publication(draft_id=first.id, destination_id="test",
                                telegram_message_id="old-day-message",
                                published_at=dump_datetime(datetime(2025, 1, 1, 20, 59, tzinfo=UTC))))
        session.flush()
        article = Article(original_url="https://example.com/next", canonical_url="https://example.com/next",
                          content_hash="f" * 64, ingest_kind="admin_url", status="processed",
                          discovered_at=now, created_at=now, updated_at=now, title="Next")
        session.add(article)
        session.flush()
        second = Draft(article_id=article.id, generated_text="Next post", category="Next",
                       generation_model="configured", content_hash="e" * 64,
                       draft_state="approved", approved_by=1, approved_at=now,
                       created_at=now, updated_at=now)
        session.add(second)
        session.flush()
        assert schedule_approved_drafts(session, config, now=clock) == 2
        local_dates = [datetime.fromisoformat(item.scheduled_at).astimezone(LOCAL_ZONE).date()
                       for item in (first, second)]
        assert local_dates == [datetime(2025, 1, 2).date(), datetime(2025, 1, 3).date()]
        assert all(datetime.fromisoformat(item.scheduled_at).utcoffset() == timedelta(0)
                   for item in (first, second))


def test_expired_approval_is_cancelled_and_window_is_checked(sessions):
    config = Settings(_env_file=None, publication_window_start="08:00", publication_window_end="21:00",
                      max_overdue_age_hours=1)
    with sessions() as session:
        _, _, draft = sample_rows(session)
        old = dump_datetime(utc_now() - timedelta(hours=2))
        draft.draft_state, draft.approved_by, draft.approved_at = "approved", 1, old
        assert schedule_approved_drafts(session, config) == 0
        assert draft.draft_state == "cancelled"
    assert within_publication_window(config, datetime(2025, 1, 1, 6, tzinfo=UTC)) is True
    assert within_publication_window(config, datetime(2025, 1, 1, 21, tzinfo=UTC)) is False


def test_restart_marks_interrupted_send_unknown(sessions):
    with sessions() as session:
        _, _, draft = sample_rows(session)
        now = dump_datetime(utc_now())
        attempt = PublicationAttempt(draft_id=draft.id, destination_id="private-test", attempt_number=1,
                                     state="sending", started_at=now)
        session.add(attempt)
        session.commit()
        ids = recover_stale_attempts(session)
        assert ids == [attempt.id]
        assert attempt.state == "unknown"
        assert session.scalar(select(AuditEvent).where(AuditEvent.event_type == "stale_attempt_marked_unknown"))
