"""Process locks (fcntl). Locks are scoped narrowly:

* ``intake`` / ``feedback`` - one gate run per entry point at a time (non-blocking).
* ``kanban-submit``         - covers query -> create -> record link for Kanban cards,
                              used by every entry point (blocking, short timeout).
* ``outbox``                - one outbox drain at a time (non-blocking).

No lock is held while a delegated worker executes; long agent runs never block the
Todo Manager.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from pathlib import Path
from typing import Iterator


class LockBusy(Exception):
    pass


@contextlib.contextmanager
def file_lock(path: Path, *, blocking: bool = False, timeout: float = 30.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if not blocking or time.monotonic() >= deadline:
                    raise LockBusy(str(path)) from None
                time.sleep(0.2)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lock_path(home: Path, name: str) -> Path:
    return home / "state" / "locks" / f"{name}.lock"
