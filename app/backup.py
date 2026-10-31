"""SQLite-safe backup and non-destructive restore verification helpers."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def backup_database(source_path: str | Path, destination_path: str | Path) -> Path:
    source = Path(source_path).resolve()
    destination = Path(destination_path).resolve()
    if not source.is_file():
        raise FileNotFoundError("Source database does not exist")
    if source == destination or destination.exists():
        raise FileExistsError("Backup destination must be a new, separate file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
            with sqlite3.connect(destination) as backup_db:
                source_db.backup(backup_db)
                result = backup_db.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
        destination.chmod(0o600)
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def verify_restore(backup_path: str | Path, restore_path: str | Path) -> Path:
    """Restore a backup to a new path, verify integrity, and never overwrite a DB."""
    backup = Path(backup_path).resolve()
    restore = Path(restore_path).resolve()
    if not backup.is_file():
        raise FileNotFoundError("Backup file does not exist")
    if backup == restore or restore.exists():
        raise FileExistsError("Restore destination must be new and separate")
    restore.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as backup_db:
            with sqlite3.connect(restore) as restored_db:
                backup_db.backup(restored_db)
                check = restored_db.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError("Restored database failed integrity check")
                tables = {row[0] for row in restored_db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "alembic_version" not in tables:
                    raise RuntimeError("Restored database has no Alembic version table")
        restore.chmod(0o600)
        return restore
    except Exception:
        restore.unlink(missing_ok=True)
        raise


def _sqlite_file(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("Backup utility supports SQLite file databases only")
    path = database_url[len(prefix):].split("?", 1)[0]
    if path in {"", ":memory:"}:
        raise ValueError("A file-backed SQLite database is required")
    return Path(path)


def main() -> None:
    from app.config import get_settings
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("destination")
    restore_parser = sub.add_parser("verify-restore")
    restore_parser.add_argument("backup")
    restore_parser.add_argument("new_destination")
    args = parser.parse_args()
    if args.action == "backup":
        result = backup_database(_sqlite_file(settings.database_url), args.destination)
    else:
        result = verify_restore(args.backup, args.new_destination)
    print(f"Verified database copy: {result}")


if __name__ == "__main__":
    main()
