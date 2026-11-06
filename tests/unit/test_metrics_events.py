"""Tests for the event ledger behind ``sinter_vram_oom_events_total``.

The ledger is the only part of the metrics path that has state, and its
failure modes are the ones that make an alerting pipeline lie: double counting
after a restart, missing an event that straddles a scrape boundary, or calling
a deliberate stop a crash.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cupola.config import MetricsConfig
from cupola.metrics import EventLedger
from cupola.state import StateRecord


def _ledger(state_dir: Path, **overrides) -> EventLedger:
    return EventLedger(state_dir, MetricsConfig(**overrides))


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(text)


def _backend_log(state_dir: Path) -> Path:
    return state_dir / "logs" / "backend-coding.log"


class TestOomSignatures:
    def test_matching_line_counts_once(self, tmp_path):
        _append(_backend_log(tmp_path),
                "0.01 ggml_alloc.c:1126  HIP out of memory: tried to allocate 4096 MB\n"
                "0.02 some unrelated line\n")
        counters = _ledger(tmp_path).update(StateRecord())
        assert counters["vram_oom_backend_log"] == 1

    def test_scrape_is_not_rescanned(self, tmp_path):
        """The cursor advances: a scrape reports events since the last one."""
        _append(_backend_log(tmp_path), "out of memory\n")
        ledger = _ledger(tmp_path)
        assert ledger.update(StateRecord())["vram_oom_backend_log"] == 1
        assert ledger.update(StateRecord())["vram_oom_backend_log"] == 1

    def test_counters_survive_exporter_restart(self, tmp_path):
        """A reload must not re-count, and must not forget the total."""
        _append(_backend_log(tmp_path), "Failed to allocate buffer\n")
        assert _ledger(tmp_path).update(StateRecord())["vram_oom_backend_log"] == 1
        assert _ledger(tmp_path).update(StateRecord())["vram_oom_backend_log"] == 1

    def test_partial_line_is_left_for_the_next_scrape(self, tmp_path):
        """A signature split across scrapes must not be lost or double counted."""
        _append(_backend_log(tmp_path), "0.01 Failed to allo")
        assert _ledger(tmp_path).update(StateRecord()).get("vram_oom_backend_log", 0) == 0

        _append(_backend_log(tmp_path), "cate memory\n")
        assert _ledger(tmp_path).update(StateRecord())["vram_oom_backend_log"] == 1

    def test_rotation_is_detected_by_inode(self, tmp_path):
        """logrotate moves the file; the replacement must still be read."""
        _append(_backend_log(tmp_path), "out of memory\n")
        ledger = _ledger(tmp_path)
        assert ledger.update(StateRecord())["vram_oom_backend_log"] == 1

        os.unlink(_backend_log(tmp_path))
        _append(_backend_log(tmp_path), "out of memory\n")
        assert ledger.update(StateRecord())["vram_oom_backend_log"] == 2

    def test_truncation_resets_the_cursor(self, tmp_path):
        _append(_backend_log(tmp_path), "out of memory\n" * 5)
        ledger = _ledger(tmp_path)
        assert ledger.update(StateRecord())["vram_oom_backend_log"] == 5

        _backend_log(tmp_path).write_text("out of memory\n")
        assert ledger.update(StateRecord())["vram_oom_backend_log"] == 6

    def test_tail_cap_bounds_one_scrape_without_losing_events(self, tmp_path):
        """A huge log is drained over several scrapes, never skipped."""
        _append(_backend_log(tmp_path), "out of memory\n" * 40)
        ledger = _ledger(tmp_path, log_tail_max_bytes=200)

        first = ledger.update(StateRecord())["vram_oom_backend_log"]
        assert 0 < first < 40
        drained = first
        for _ in range(10):
            drained = ledger.update(StateRecord())["vram_oom_backend_log"]
        assert drained == 40

    def test_patterns_are_configurable(self, tmp_path):
        """The default list ignores this line; a configured pattern catches it."""
        _append(_backend_log(tmp_path), "allocation refused by the allocator\n")
        plain = _ledger(tmp_path)
        assert plain.update(StateRecord()).get("vram_oom_backend_log", 0) == 0

        _append(_backend_log(tmp_path), "allocation refused again\n")
        tuned = _ledger(tmp_path, oom_patterns=["allocation refused"])
        assert tuned.update(StateRecord())["vram_oom_backend_log"] == 1

    def test_absent_logs_produce_nothing(self, tmp_path):
        counters = _ledger(tmp_path).update(StateRecord())
        assert counters == {}


class TestLaunchAccounting:
    def test_launch_spawned_increments(self, tmp_path):
        _append(tmp_path / "logs" / "cupola.log",
                json.dumps({"event": "launch_spawned", "pid": 111}) + "\n")
        counters = _ledger(tmp_path).update(StateRecord())
        assert counters["launches"] == 1

    def test_malformed_line_is_skipped(self, tmp_path):
        _append(tmp_path / "logs" / "cupola.log", "{not json}\n")
        counters = _ledger(tmp_path).update(StateRecord())
        assert "launches" not in counters


class TestExitAccounting:
    """A backend that dies without a stop request is the headline signal.

    The supervisor has no crash event to log — it only notices a dead process on
    the next call — so the ledger compares what it saw last scrape against what
    is alive now.
    """

    @pytest.fixture
    def backend(self):
        """A stand-in for llama-server: a real pid that can be observed dying.

        It is reaped on kill because a zombie still answers a signal-0 liveness
        check, which would make the ledger call it alive.
        """
        proc = subprocess.Popen(["sleep", "30"])
        yield proc
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    @staticmethod
    def _kill_and_reap(proc):
        proc.kill()
        proc.wait()
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            return
        raise AssertionError(f"pid {proc.pid} still present after reap")

    def test_death_without_stop_request_is_unexpected(self, tmp_path, backend):
        state = StateRecord(state="READY", backend_pid=backend.pid)
        ledger = _ledger(tmp_path)
        assert ledger.update(state) == {}

        self._kill_and_reap(backend)
        counters = ledger.update(state)
        assert counters["unexpected_exits"] == 1
        assert "vram_oom_process_exit" not in counters

    def test_stop_request_explains_the_death(self, tmp_path, backend):
        state = StateRecord(state="READY", backend_pid=backend.pid)
        ledger = _ledger(tmp_path)
        ledger.update(state)

        _append(tmp_path / "logs" / "cupola.log",
                json.dumps({"event": "stop_sigterm", "pid": backend.pid}) + "\n")
        self._kill_and_reap(backend)

        counters = ledger.update(state)
        assert "unexpected_exits" not in counters

    def test_recorded_stop_state_explains_the_death(self, tmp_path, backend):
        """A stop performed without a log line still is not a crash."""
        state = StateRecord(state="READY", backend_pid=backend.pid)
        ledger = _ledger(tmp_path)
        ledger.update(state)

        self._kill_and_reap(backend)

        counters = ledger.update(StateRecord(state="STOPPED", backend_pid=backend.pid))
        assert "unexpected_exits" not in counters

    def test_oom_immediately_before_death_is_an_oom_death(self, tmp_path, backend):
        state = StateRecord(state="READY", backend_pid=backend.pid)
        ledger = _ledger(tmp_path)
        ledger.update(state)

        _append(_backend_log(tmp_path), "HIP out of memory: tried to allocate 8192 MB\n")
        self._kill_and_reap(backend)

        counters = ledger.update(state)
        assert counters["vram_oom_backend_log"] == 1
        assert counters["vram_oom_process_exit"] == 1
        assert counters["unexpected_exits"] == 1

    def test_death_is_counted_once(self, tmp_path, backend):
        """Repeated scrapes of a dead backend do not pile up the counter."""
        state = StateRecord(state="READY", backend_pid=backend.pid)
        ledger = _ledger(tmp_path)
        ledger.update(state)

        self._kill_and_reap(backend)
        assert ledger.update(state)["unexpected_exits"] == 1
        assert ledger.update(state)["unexpected_exits"] == 1

    def test_death_is_still_counted_after_an_exporter_restart(self, tmp_path, backend):
        """The ledger is on disk, so an exporter reload does not lose the death.

        This is the reason the counters are persisted at all: a counter that
        restarts from zero turns ``increase()`` over a rare event into noise.
        """
        state = StateRecord(state="READY", backend_pid=backend.pid)
        _ledger(tmp_path).update(state)

        self._kill_and_reap(backend)

        counters = _ledger(tmp_path).update(state)
        assert counters["unexpected_exits"] == 1
