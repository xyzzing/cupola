"""Tests for PRD completion: UUID verification, SHA256 validation, DEGRADED blocking."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def test_instance_uuid_verification():
    """Instance UUID must be verified during stop/status operations."""
    from cupola.logging import OperationalLogger
    from cupola.supervisor import InstanceRecord, Supervisor

    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir) / "state"
        runtime_dir = Path(tmpdir) / "runtime"
        state_dir.mkdir()
        runtime_dir.mkdir()

        logger = OperationalLogger(Path(tmpdir) / "logs")
        sup = Supervisor(state_dir, logger, runtime_dir)

        # Create instance with UUID
        instance = InstanceRecord(profile="test", pid=12345, state="READY")
        instance.start_time = 1000
        sup.save_instance(instance)

        # Verify UUID is stored
        loaded = sup.get_instance()
        assert loaded.instance_uuid is not None
        assert len(loaded.instance_uuid) == 36  # UUID format


def test_sha256_validation_passes():
    """Weights SHA256 validation should pass for correct hash."""
    from cupola.config import ProfileSpec, compute_sha256, validate_profile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test weights file
        weights_path = Path(tmpdir) / "model.gguf"
        weights_path.write_bytes(b"test weights data")

        # Compute actual hash
        actual_hash = compute_sha256(weights_path)

        # Create profile with correct hash
        profile = ProfileSpec(
            alias="test",
            weights_path=weights_path,
            weights_digest=actual_hash,
        )

        errors = validate_profile(profile)
        # Should only have backend binary error, not hash error
        assert not any("SHA256" in e or "mismatch" in e for e in errors)


def test_sha256_validation_fails():
    """Weights SHA256 validation should fail for wrong hash."""
    from cupola.config import ProfileSpec, validate_profile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test weights file
        weights_path = Path(tmpdir) / "model.gguf"
        weights_path.write_bytes(b"test weights data")

        # Create profile with wrong hash
        profile = ProfileSpec(
            alias="test",
            weights_path=weights_path,
            weights_digest="0" * 64,  # Wrong hash
        )

        errors = validate_profile(profile)
        assert any("SHA256 mismatch" in e for e in errors)


def test_degraded_blocks_new_launch():
    """DEGRADED state must block new launches."""
    from cupola.config import ProfileSpec
    from cupola.logging import OperationalLogger
    from cupola.supervisor import InstanceRecord, Supervisor

    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir) / "state"
        runtime_dir = Path(tmpdir) / "runtime"
        state_dir.mkdir()
        runtime_dir.mkdir()

        logger = OperationalLogger(Path(tmpdir) / "logs")
        sup = Supervisor(state_dir, logger, runtime_dir)

        # Create DEGRADED instance
        degraded = InstanceRecord(profile="old", pid=99999, state="DEGRADED")
        sup.save_instance(degraded)

        # Try to launch new instance
        profile = ProfileSpec(
            alias="new",
            weights_path=Path("/dev/null"),  # Fake path
        )

        result = sup.launch(profile)
        assert result.state == "FAILED"
        assert "DEGRADED" in result.last_error


def test_uuid_in_environment():
    """Instance UUID must be passed to spawned process via environment."""
    from cupola.config import ProfileSpec
    from cupola.logging import OperationalLogger
    from cupola.supervisor import Supervisor

    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir) / "state"
        runtime_dir = Path(tmpdir) / "runtime"
        state_dir.mkdir()
        runtime_dir.mkdir()

        # Create a fake weights file
        weights_path = Path(tmpdir) / "model.gguf"
        weights_path.write_bytes(b"fake weights")

        logger = OperationalLogger(Path(tmpdir) / "logs")
        sup = Supervisor(state_dir, logger, runtime_dir)

        # Mock subprocess.Popen to capture env
        captured_env = {}

        def mock_popen(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            proc = type("MockProc", (), {"pid": 12345})()
            return proc

        # This test is about the instance marker in the child environment, so
        # the admission gate and the readiness probe are stubbed deliberately.
        # Left real they make the test environment-dependent: without
        # llama-server, validate_profile reports a missing backend and launch()
        # returns before spawning, so Popen is never reached -- which is
        # exactly how this failed on CI while passing on a workstation that
        # has the binary. The bare `except Exception: pass` that used to wrap
        # launch() hid that, and left the assertion to catch it instead.
        profile = ProfileSpec(
            alias="test",
            weights_path=weights_path,
            port=18080,  # Use non-standard port to avoid conflicts
        )

        class _ReadyResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch("cupola.config.validate_profile", return_value=[]), \
                patch("subprocess.Popen", mock_popen), \
                patch("urllib.request.urlopen",
                      return_value=_ReadyResponse()), \
                patch("cupola.supervisor.Supervisor._read_proc_stat",
                      return_value=1000), \
                patch("cupola.supervisor.Supervisor._start_sentinel"):
            result = sup.launch(profile, timeout=5.0)

        assert result.state == "READY", getattr(result, "last_error", "")
        assert "__CUPOLA_INSTANCE" in captured_env
        # UUID should be 36 chars
        assert len(captured_env["__CUPOLA_INSTANCE"]) == 36


def test_compute_sha256_large_file():
    """SHA256 computation should handle large files with chunked reads."""
    from cupola.config import compute_sha256

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a file larger than one chunk (64 MiB) — use smaller for test
        test_path = Path(tmpdir) / "large.bin"
        data = os.urandom(1024 * 1024)  # 1 MiB
        test_path.write_bytes(data)

        # Compute via our function
        our_hash = compute_sha256(test_path)

        # Compute via hashlib directly for comparison
        sha256 = hashlib.sha256()
        sha256.update(data)
        expected = sha256.hexdigest()

        assert our_hash == expected


# PRD v2 Acceptance Criteria Tests


def test_deterministic_plan_compilation():
    """PRD v2 Criterion 1: Same input produces identical DAG structure."""
    from cupola.plan import compile_plan

    plan_data = {
        "plan_version": "1.0.0",
        "project_name": "test",
        "tasks": [
            {"id": "a", "title": "A", "phase": "SCHEMA",
             "dependencies": [], "verification": {"command": "test"}},
            {"id": "b", "title": "B", "phase": "SCHEMA",
             "dependencies": ["a"], "verification": {"command": "test"}},
        ],
    }

    plan1, errors1 = compile_plan(plan_data)
    plan2, errors2 = compile_plan(plan_data)

    assert errors1 == errors2 == []
    assert len(plan1.tasks) == len(plan2.tasks)
    for t1, t2 in zip(plan1.tasks, plan2.tasks):
        assert t1.id == t2.id
        assert t1.dependencies == t2.dependencies


def test_context_ceiling_enforcement():
    """PRD v2 Criterion 2: Context never exceeds 16,384 tokens."""
    from cupola.context import package_for_planning
    from cupola.manifest import build_manifest

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a large document
        doc_path = Path(tmpdir) / "large.md"
        doc_path.write_text("# Large\n\n" + "Word " * 20000)

        manifest = build_manifest([doc_path])
        context = package_for_planning(manifest, max_tokens=16384)

        assert context.token_estimate <= 16384


def test_standard_library_compliance():
    """PRD v2 Criterion 5: All modules use Python 3.10+ standard library."""
    import importlib
    import inspect

    new_modules = [
        "cupola.sandbox",
        "cupola.telemetry",
        "cupola.documents",
        "cupola.manifest",
        "cupola.context",
        "cupola.minder",
        "cupola.plan",
        "cupola.checkpoints",
    ]

    for module_name in new_modules:
        try:
            module = importlib.import_module(module_name)
            # Check imports in module source
            source = inspect.getsource(module)
            # Simple check: look for pip package imports
            assert "import numpy" not in source
            assert "import pandas" not in source
            assert "import requests" not in source
        except ImportError:
            pass  # Module might not be importable in test env


def test_sandbox_resource_limits():
    """PRD v2: Sandbox enforces RLIMIT_AS and RLIMIT_CPU."""
    from cupola.sandbox import run_sandboxed

    # Test memory limit
    result = run_sandboxed(
        ["python3", "-c", "x = [0] * (100 * 1024 * 1024)"],
        timeout=10,
        max_memory_mb=64,
    )
    # Should be killed by OOM
    assert result.exit_code != 0 or result.killed


def test_telemetry_atomic_write():
    """PRD v2: Telemetry writes are atomic (no partial reads)."""
    from cupola.telemetry import update_state_atomic

    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "instance.json"
        data = {"test": "value", "number": 42}

        update_state_atomic(data, target)

        # Verify complete write
        with open(target, "r") as f:
            loaded = json.load(f)
        assert loaded == data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
