from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from app.config import load_settings
from app.database.session import make_engine


def test_alembic_upgrade_creates_schema_and_foreign_keys(tmp_path, monkeypatch):
    database = tmp_path / "migration.sqlite3"
    url = f"sqlite:///{database}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)
    engine = make_engine(url)
    inspector = inspect(engine)
    assert {"sources", "articles", "article_text", "drafts", "publication_attempts",
            "publications", "audit_events", "quota_usage"}.issubset(set(inspector.get_table_names()))
    assert inspector.get_indexes("publication_attempts")
    assert {fk["referred_table"] for fk in inspector.get_foreign_keys("article_text")} == {"articles"}
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0001_initial"
    engine.dispose()


def test_alembic_and_application_share_dotenv_database_url(tmp_path, monkeypatch):
    repository_root = Path(__file__).resolve().parents[1]
    database = tmp_path / "custom" / "app-state.sqlite3"
    database.parent.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL=sqlite:///{database}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    effective_url = load_settings().database_url
    assert effective_url == f"sqlite:///{database}"
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "migrations"))
    accidental_default = tmp_path / "ini-default.sqlite3"
    config.set_main_option("sqlalchemy.url", f"sqlite:///{accidental_default}")
    command.upgrade(config, "head")

    engine = make_engine(effective_url)
    try:
        assert inspect(engine).has_table("alembic_version")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0001_initial"
    finally:
        engine.dispose()
    assert not accidental_default.exists()


def test_process_environment_overrides_dotenv_database_url(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=sqlite:///from-dotenv.sqlite3\n")
    override = f"sqlite:///{tmp_path / 'from-environment.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", override)
    assert load_settings(env_file=str(env_file)).database_url == override
