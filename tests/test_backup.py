from __future__ import annotations

import pytest
from sqlalchemy import text

from app.backup import backup_database, verify_restore
from app.database.session import make_engine


def test_sqlite_backup_and_restore_are_non_destructive(tmp_path):
    source = tmp_path / "source.sqlite3"
    engine = make_engine(f"sqlite:///{source}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('0001_initial')"))
        conn.execute(text("CREATE TABLE sample (value TEXT)"))
        conn.execute(text("INSERT INTO sample VALUES ('retained')"))
    engine.dispose()
    backup = backup_database(source, tmp_path / "backup.sqlite3")
    restored = verify_restore(backup, tmp_path / "restored.sqlite3")
    restored_engine = make_engine(f"sqlite:///{restored}")
    with restored_engine.connect() as conn:
        assert conn.execute(text("SELECT value FROM sample")).scalar() == "retained"
    restored_engine.dispose()
    with pytest.raises(FileExistsError):
        verify_restore(backup, restored)
