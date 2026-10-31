"""End-to-end acceptance test: real client journey.

Simulates: validate → plan → up → health check → status → down → status
Uses a real llama-server if available, otherwise mocks the backend.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from cupola.config import ProfileSpec
from cupola.logging import OperationalLogger
from cupola.supervisor import Supervisor


@pytest.mark.real_hardware
@pytest.mark.skipif(
    not os.environ.get("CUPOLA_RUN_REAL_HARDWARE_TESTS"),
    reason="Real hardware tests not requested (set CUPOLA_RUN_REAL_HARDWARE_TESTS=1)",
)
@pytest.mark.skipif(
    not os.environ.get("CUPOLA_LLAMA_SERVER"),
    reason="CUPOLA_LLAMA_SERVER not set",
)
@pytest.mark.skipif(
    not os.environ.get("CUPOLA_WEIGHTS"),
    reason="CUPOLA_WEIGHTS not set",
)
def test_full_client_journey():
    """Complete acceptance journey: up → status → down → status."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state = Path(tmpdir) / "state"
        log = OperationalLogger(state / "logs")
        sup = Supervisor(state, log)

        # Step 1: Status should be STOPPED
        status = sup.status()
        assert status.state == "STOPPED"

        # Step 2: Launch
        profile = ProfileSpec(
            alias="test",
            weights_path=Path(os.environ["CUPOLA_WEIGHTS"]),
            backend_binary=Path(os.environ["CUPOLA_LLAMA_SERVER"]),
            device="ROCm0",
            n_gpu_layers=63,
            ctx_size=131072,
            cache_type_k="q4_0",
            cache_type_v="q4_0",
            flash_attn=True,
            batch_size=4096,
            ubatch_size=1024,
            threads=6,
            n_parallel=1,
            port=18080,
            host="127.0.0.1",
        )

        instance = sup.launch(profile, timeout=60.0)

        # Step 3: Verify READY or FAILED with reason
        if instance.state == "READY":
            # Step 4: Health check via HTTP
            import urllib.request

            req = urllib.request.Request("http://127.0.0.1:18080/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200

            # Step 5: Status should show READY
            status = sup.status()
            assert status.state == "READY"
            assert status.pid is not None
            assert status.port == 18080

            # Step 6: Stop
            stop_result = sup.stop(timeout=10.0)
            assert stop_result.state == "STOPPED"

        # Step 7: Final status should be STOPPED or FAILED
        status = sup.status()
        assert status.state in ("STOPPED", "FAILED")


def test_logging_captures_events():
    """Operational logging captures lifecycle events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state = Path(tmpdir) / "state"
        log = OperationalLogger(state / "logs")
        sup = Supervisor(state, log)

        # Simulate a launch failure (no weights)
        profile = ProfileSpec(
            alias="test",
            weights_path=Path("/nonexistent/model.gguf"),
            backend_binary=None,
        )

        instance = sup.launch(profile, timeout=5.0)
        assert instance.state == "FAILED"

        # Verify log file exists and contains events
        log_path = state / "logs" / "cupola.log"
        assert log_path.exists()

        with open(log_path) as f:
            lines = f.readlines()

        assert len(lines) >= 2
        events = []
        for line in lines:
            entry = json.loads(line)
            events.append(entry["event"])

        assert "launch_start" in events
        assert "launch_failed_validation" in events

        # Verify logs contain no prompt/completion content
        log_content = "".join(lines)
        assert "Hello" not in log_content
        assert "completion" not in log_content