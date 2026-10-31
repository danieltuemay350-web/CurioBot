"""Local-process lock enforcing a single scheduler/publisher instance."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO


class InstanceAlreadyRunning(RuntimeError):
    """Another process owns the database's scheduler lock."""


class InstanceLock:
    def __init__(self, lock_file: TextIO):
        self._file = lock_file

    @classmethod
    def acquire(cls, database_path: str | Path) -> "InstanceLock":
        path = Path(f"{Path(database_path).resolve()}.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = path.open("a+", encoding="ascii")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise InstanceAlreadyRunning("Another bot process already owns this database") from exc
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{os.getpid()}\n")
        lock_file.flush()
        return cls(lock_file)

    def release(self) -> None:
        if not self._file.closed:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
