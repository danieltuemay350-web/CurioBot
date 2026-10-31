"""Application entry point."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.bot.application import build_application
from app.config import get_settings
from app.database.session import make_engine, make_session_factory
from app.instance_lock import InstanceLock
from app.scheduler.jobs import start_scheduler


class JsonFormatter(logging.Formatter):
    """Emit structured UTC logs without serializing exception text by default."""

    def format(self, record: logging.LogRecord) -> str:
        item: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        reserved = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
        for key, value in record.__dict__.items():
            if key not in reserved and not key.startswith("_"):
                if isinstance(value, (str, int, float, bool)) or value is None:
                    item[key] = value
        return json.dumps(item, ensure_ascii=True)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def require_current_migrations(engine) -> None:
    if not inspect(engine).has_table("alembic_version"):
        raise RuntimeError("Database migrations are missing; run `python -m alembic upgrade head` before startup")
    with engine.connect() as connection:
        current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    if current not in heads:
        raise RuntimeError("Database migration revision is not current; run `python -m alembic upgrade head`")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        settings.require_runtime_credentials()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url.removeprefix("sqlite:///").split("?", 1)[0]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)
    require_current_migrations(engine)
    db_path = settings.database_url.removeprefix("sqlite:///").split("?", 1)[0]
    instance_lock = InstanceLock.acquire(db_path)
    sessions = make_session_factory(engine)

    async def post_init(application) -> None:
        application.bot_data["scheduler"] = start_scheduler(application, settings, sessions)

    async def post_shutdown(application) -> None:
        scheduler = application.bot_data.get("scheduler")
        if scheduler:
            scheduler.shutdown(wait=True)
        instance_lock.release()
        engine.dispose()

    application = build_application(settings, sessions, post_init=post_init,
                                    post_shutdown=post_shutdown)
    application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
