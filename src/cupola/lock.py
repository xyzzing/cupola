"""Exclusive file lock for CUPOLA supervisor operations."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


@contextmanager
def acquire_lock(runtime_dir: Path, timeout: float = 5.0) -> Generator[Path, None, None]:
    """Acquire exclusive file lock at $XDG_RUNTIME_DIR/cupola/cupola.lock.

    Uses O_EXCL create semantics for atomicity with polling retry.
    The lock file persists until the owning process releases it (context exit).

    Yields the lock path on success. Raises TimeoutError if lock is held
    for longer than the timeout period.
    """
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / "cupola.lock"

    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Could not acquire lock at {lock_path} within {timeout}s"
                ) from None
            time.sleep(0.05)

    try:
        # Write our PID for debugging
        os.write(fd, str(os.getpid()).encode())
        yield lock_path
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(lock_path)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
        os.unlink(lock_path)