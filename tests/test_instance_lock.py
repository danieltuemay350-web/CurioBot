from __future__ import annotations

import pytest

from app.instance_lock import InstanceAlreadyRunning, InstanceLock


def test_only_one_process_can_own_database_lock(tmp_path):
    database = tmp_path / "bot.sqlite3"
    first = InstanceLock.acquire(database)
    try:
        with pytest.raises(InstanceAlreadyRunning):
            InstanceLock.acquire(database)
    finally:
        first.release()
    second = InstanceLock.acquire(database)
    second.release()
