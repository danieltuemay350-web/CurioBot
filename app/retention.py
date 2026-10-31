"""Retention cleanup for extracted article text."""

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.database.models import ArticleText
from app.timeutil import dump_datetime, utc_now


def cleanup_expired_text(session: Session) -> int:
    result = session.execute(delete(ArticleText).where(ArticleText.expires_at <= dump_datetime(utc_now())))
    count = result.rowcount or 0
    session.commit()
    return count
