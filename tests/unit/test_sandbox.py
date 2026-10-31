"""Tests for sandboxed command execution."""

import os
import tempfile

import pytest

from cupola.sandbox import SandboxResult, run_sandboxed


def test_sandbox_basic_command():
    """Basic command execution in sandbox."""
    result = run_sandboxed(["echo", "hello"], timeout=5)
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert not result.timed_out
    assert not result.killed


def test_sandbox_command_failure():
    """Command that exits with non-zero code."""
    result = run_sandboxed(["false"], timeout=5)
    assert result.exit_code == 1
    assert not result.timed_out


def test_sandbox_timeout():
    """Command that exceeds wall-clock timeout."""
    result = run_sandboxed(["sleep", "10"], timeout=2)
    assert result.timed_out
    assert result.killed


def test_sandbox_memory_limit():
    """Command that exceeds memory limit (RLIMIT_AS)."""
    # Create a Python script that allocates lots of memory
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("import sys\nx = [0] * (100 * 1024 * 1024)\n")  # ~800MB
        script_path = f.name

    try:
        result = run_sandboxed(
            ["python3", script_path],
            timeout=10,
            max_memory_mb=64,  # 64MB limit
        )
        # Should be killed by OOM (exit code 137 = SIGKILL)
        assert result.exit_code != 0 or result.killed
    finally:
        os.unlink(script_path)


def test_sandbox_cpu_time_limit():
    """Command that exceeds CPU time limit (RLIMIT_CPU)."""
    # Create a Python script that loops forever
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("while True: pass\n")
        script_path = f.name

    try:
        result = run_sandboxed(
            ["python3", script_path],
            timeout=10,
            cpu_time_seconds=2,
        )
        # Should be killed by CPU timeout (exit code 152 = SIGXCPU) or SIGKILL
        assert result.exit_code != 0 or result.killed
    finally:
        os.unlink(script_path)


def test_sandbox_environment_scrubbing():
    """Sensitive environment variables are dropped."""
    # Set a sensitive env var
    os.environ["TEST_SECRET_KEY"] = "secret123"
    os.environ["API_TOKEN"] = "token456"

    try:
        # Command that prints environment
        result = run_sandboxed(
            ["sh", "-c", "env | grep -E 'SECRET|TOKEN'"],
            timeout=5,
        )
        # Should not find any SECRET or TOKEN vars
        assert "TEST_SECRET_KEY" not in result.stdout
        assert "API_TOKEN" not in result.stdout
    finally:
        del os.environ["TEST_SECRET_KEY"]
        del os.environ["API_TOKEN"]


def test_sandbox_environment_allowlist():
    """Allowlisted environment variables are preserved."""
    os.environ["ALLOWED_VAR"] = "allowed_value"

    try:
        result = run_sandboxed(
            ["sh", "-c", "echo $ALLOWED_VAR"],
            timeout=5,
            env_allowlist=["ALLOWED_VAR"],
        )
        assert "allowed_value" in result.stdout
    finally:
        del os.environ["ALLOWED_VAR"]


def test_sandbox_result_dataclass():
    """SandboxResult dataclass fields."""
    result = SandboxResult(
        exit_code=0,
        stdout="output",
        stderr="error",
        timed_out=False,
        killed=False,
        elapsed_seconds=1.5,
    )
    assert result.exit_code == 0
    assert result.stdout == "output"
    assert result.elapsed_seconds == 1.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
