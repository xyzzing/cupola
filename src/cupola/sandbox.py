"""Sandboxed command execution with resource limits (Shield).

Implements `cupola exec --sandbox` with:
- RLIMIT_AS: memory fence
- RLIMIT_CPU: CPU time fence
- Process group isolation (setsid)
- Environment scrubbing
- Timeout with SIGKILL cleanup
"""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class SandboxResult:
    """Result of a sandboxed command execution."""
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    killed: bool
    elapsed_seconds: float


def _scrub_environment(env_allowlist: Optional[list[str]] = None) -> dict[str, str]:
    """Scrub environment: drop sensitive vars, keep essentials.

    Drops: *TOKEN*, *KEY*, *SECRET*, SSH_*
    Keeps: PATH, LANG, VIRTUAL_ENV, plus any allowlisted vars.
    """
    if env_allowlist is None:
        env_allowlist = []

    keep_prefixes = ("PATH", "LANG", "VIRTUAL_ENV")
    scrubbed = {}

    for key, value in os.environ.items():
        # Always keep essential vars
        if any(key.startswith(prefix) for prefix in keep_prefixes):
            scrubbed[key] = value
            continue

        # Keep explicitly allowlisted vars
        if key in env_allowlist:
            scrubbed[key] = value
            continue

        # Drop sensitive vars
        upper_key = key.upper()
        if any(marker in upper_key for marker in ("TOKEN", "KEY", "SECRET", "SSH_")):
            continue

        # Keep everything else
        scrubbed[key] = value

    return scrubbed


def _set_resource_limits(
    max_memory_mb: int,
    cpu_time_seconds: int,
) -> None:
    """Set RLIMIT_AS and RLIMIT_CPU for the child process."""
    # RLIMIT_AS: limit virtual memory (in bytes)
    if max_memory_mb > 0:
        mem_bytes = max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

    # RLIMIT_CPU: limit CPU time (in seconds)
    if cpu_time_seconds > 0:
        resource.setrlimit(
            resource.RLIMIT_CPU, (cpu_time_seconds, cpu_time_seconds + 5)
        )


def run_sandboxed(
    command: list[str],
    timeout: int = 15,
    max_memory_mb: int = 2048,
    cpu_time_seconds: int = 15,
    env_allowlist: Optional[list[str]] = None,
    cwd: Optional[str] = None,
) -> SandboxResult:
    """Run a command in a sandboxed environment.

    Args:
        command: Command and arguments to execute.
        timeout: Wall-clock timeout in seconds.
        max_memory_mb: Maximum virtual memory in MB (RLIMIT_AS).
        cpu_time_seconds: Maximum CPU time in seconds (RLIMIT_CPU).
        env_allowlist: Additional environment variables to preserve.
        cwd: Working directory for the command.

    Returns:
        SandboxResult with exit code, output, and metadata.
    """
    env = _scrub_environment(env_allowlist)

    start_time = time.monotonic()

    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            preexec_fn=lambda: (
                os.setsid(),
                _set_resource_limits(max_memory_mb, cpu_time_seconds),
            ),
            close_fds=True,
        )
    except OSError as e:
        elapsed = time.monotonic() - start_time
        return SandboxResult(
            exit_code=-1,
            stdout="",
            stderr=f"Failed to spawn process: {e}",
            timed_out=False,
            killed=False,
            elapsed_seconds=elapsed,
        )

    timed_out = False
    killed = False

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = True
        # Kill the entire process group
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        # Wait for process to actually exit
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b""

    elapsed = time.monotonic() - start_time

    # Decode output
    stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""

    return SandboxResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_str,
        stderr=stderr_str,
        timed_out=timed_out,
        killed=killed,
        elapsed_seconds=elapsed,
    )
