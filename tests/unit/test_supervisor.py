"""Tests for CUPOLA supervisor."""

from __future__ import annotations

import tempfile
from pathlib import Path

from cupola.supervisor import InstanceRecord, Supervisor


def test_instance_record_serialization():
    """InstanceRecord can be serialized and deserialized."""
    rec = InstanceRecord(profile="test", port=8080, state="READY")
    data = rec.to_dict()
    loaded = InstanceRecord.from_dict(data)
    assert loaded.instance_uuid == rec.instance_uuid
    assert loaded.profile == "test"
    assert loaded.port == 8080
    assert loaded.state == "READY"


def test_supervisor_save_and_load():
    """Supervisor can save and load instance records."""
    with tempfile.TemporaryDirectory() as tmpdir:
        runtime = Path(tmpdir) / "runtime"
        state = Path(tmpdir) / "state"
        sup = Supervisor(runtime, state)

        rec = InstanceRecord(profile="test", port=8080, state="READY", pid=12345)
        sup.save_instance(rec)

        loaded = sup.get_instance()
        assert loaded is not None
        assert loaded.profile == "test"
        assert loaded.state == "READY"


def test_supervisor_status_no_instance():
    """Status returns STOPPED when no instance exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sup = Supervisor(Path(tmpdir), Path(tmpdir))
        status = sup.status()
        assert status.state == "STOPPED"


def test_supervisor_stop_no_instance():
    """Stop with no instance returns STOPPED."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sup = Supervisor(Path(tmpdir), Path(tmpdir))
        result = sup.stop()
        assert result.state == "STOPPED"


def test_read_proc_stat_missing():
    """Reading /proc/[pid]/stat for nonexistent PID returns None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sup = Supervisor(Path(tmpdir), Path(tmpdir))
        assert sup._read_proc_stat(999999999) is None


def test_process_exists_missing():
    """Process existence check for missing PID returns False."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sup = Supervisor(Path(tmpdir), Path(tmpdir))
        assert not sup._process_exists(999999999)


def test_send_signal_missing():
    """Sending signal to missing PID returns False."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sup = Supervisor(Path(tmpdir), Path(tmpdir))
        assert not sup._send_signal(999999999, 15)