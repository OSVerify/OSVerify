"""Process locks for Unix and native Windows, released when the handle closes."""
from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path


def _try_lock(handle):
    if os.name == 'nt':
        import msvcrt

        # Always lock the same byte, even when the lock file is empty. Windows
        # permits regions beyond EOF; append mode must not choose the offset.
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BlockingIOError(exc.errno, 'lock is held by another process') from exc
            raise
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle):
    if os.name == 'nt':
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, *, timeout: float = 0):
    """Acquire a bounded exclusive lock; zero timeout reports contention at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _try_lock(handle)
                break
            except BlockingIOError:
                if timeout == 0:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Timed out waiting for a file lock') from None
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            _unlock(handle)
