"""Tests for CUPOLA state management."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from cupola.state import AtomicFile, StateRecord, load_state, save_state


def test_default_state_record():
    record = StateRecord()
    assert record.state == "STOPPED"
    assert record.backend_pid is None
    assert record.last_error is None


def test_load_missing_state():
    """Loading state from empty directory returns default."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state = load_state(Path(tmpdir))
        assert state.state == "STOPPED"


def test_save_and_load_state():
    """State can be saved and loaded round-trip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        record = StateRecord(
            state="READY",
            backend_pid=12345,
            profile_alias="coding",
            instance_id="test-123",
        )
        save_state(Path(tmpdir), record)

        loaded = load_state(Path(tmpdir))
        assert loaded.state == "READY"
        assert loaded.backend_pid == 12345
        assert loaded.profile_alias == "coding"
        assert loaded.instance_id == "test-123"


def test_atomic_file_write():
    """AtomicFile writes and renames correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.txt"
        atomic = AtomicFile(path)
        atomic.write("hello world")

        assert path.exists()
        assert path.read_text() == "hello world"


def test_atomic_file_creates_parent_dirs():
    """AtomicFile creates parent directories if needed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "nested" / "dir" / "test.txt"
        atomic = AtomicFile(path)
        atomic.write("content")

        assert path.exists()
        assert path.read_text() == "content"


def test_state_json_format():
    """State is stored as valid JSON."""
    with tempfile.TemporaryDirectory() as tmpdir:
        record = StateRecord(state="STARTING", backend_pid=999)
        save_state(Path(tmpdir), record)

        state_file = Path(tmpdir) / "state.json"
        assert state_file.exists()

        with open(state_file) as f:
            data = json.load(f)
        assert data["state"] == "STARTING"
        assert data["backend_pid"] == 999