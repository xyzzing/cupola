"""Tests for CUPOLA file lock."""

from __future__ import annotations

import tempfile
from pathlib import Path

from cupola.lock import acquire_lock


def test_lock_acquire_and_release():
    """Lock can be acquired and released."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with acquire_lock(Path(tmpdir)) as lock_path:
            assert lock_path.exists()
            assert lock_path.name == "cupola.lock"
        # After release, lock is gone
        assert not lock_path.exists()


def test_lock_is_exclusive():
    """Second acquire fails while first is held."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with acquire_lock(Path(tmpdir)):
            try:
                acquire_lock(Path(tmpdir), timeout=0.1).__enter__()
                assert False, "Should have failed to acquire"
            except TimeoutError:
                pass  # Expected: lock is held by first context manager