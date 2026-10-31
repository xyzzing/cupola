"""Workspace checkpoint management via Git refs.

Manages ephemeral Git refs under refs/cupola/checkpoints/* for
plan baselines and per-task success checkpoints.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class CheckpointInfo:
    """Information about a checkpoint."""
    name: str
    ref: str
    commit: str
    message: str = ""


CHECKPOINT_REF_PREFIX = "refs/cupola/checkpoints/"


def _run_git(args: list[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
    """Run a git command and return (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except OSError as e:
        return -1, "", str(e)


def create_checkpoint(
    name: str,
    message: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> Optional[str]:
    """Create a checkpoint Git ref.

    Args:
        name: Checkpoint name (will be prefixed with refs/cupola/checkpoints/).
        message: Optional commit message.
        cwd: Working directory (defaults to current).

    Returns:
        Full ref path if successful, None otherwise.
    """
    ref = f"{CHECKPOINT_REF_PREFIX}{name}"

    # First, make sure we have a commit to point to
    # Stage any changes and commit if needed
    rc, stdout, stderr = _run_git(["status", "--porcelain"], cwd)
    if rc == 0 and stdout.strip():
        # There are changes, commit them
        rc, stdout, stderr = _run_git(["add", "-A"], cwd)
        if rc != 0:
            return None
        commit_msg = message or f"cupola checkpoint: {name}"
        rc, stdout, stderr = _run_git(
            ["commit", "-m", commit_msg, "--allow-empty"], cwd
        )
        if rc != 0:
            return None

    # Create the ref
    rc, stdout, stderr = _run_git(["update-ref", ref, "HEAD"], cwd)
    if rc != 0:
        return None

    return ref


def list_checkpoints(cwd: Optional[Path] = None) -> list[CheckpointInfo]:
    """List all cupola checkpoint refs."""
    rc, stdout, stderr = _run_git(
        ["for-each-ref", CHECKPOINT_REF_PREFIX, "--format=%(refname) %(objectname)"],
        cwd,
    )
    if rc != 0:
        return []

    checkpoints = []
    for line in stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            ref, commit = parts
            name = ref.replace(CHECKPOINT_REF_PREFIX, "")
            checkpoints.append(CheckpointInfo(
                name=name,
                ref=ref,
                commit=commit[:8],
            ))

    return checkpoints


def restore_checkpoint(
    name: str,
    cwd: Optional[Path] = None,
) -> bool:
    """Restore workspace to a checkpoint.

    Args:
        name: Checkpoint name.
        cwd: Working directory.

    Returns:
        True if successful, False otherwise.
    """
    ref = f"{CHECKPOINT_REF_PREFIX}{name}"

    # Checkout the commit
    rc, stdout, stderr = _run_git(["checkout", ref], cwd)
    if rc != 0:
        return False

    return True


def delete_checkpoint(
    name: str,
    cwd: Optional[Path] = None,
) -> bool:
    """Delete a checkpoint ref.

    Args:
        name: Checkpoint name.
        cwd: Working directory.

    Returns:
        True if successful, False otherwise.
    """
    ref = f"{CHECKPOINT_REF_PREFIX}{name}"

    rc, stdout, stderr = _run_git(["update-ref", "-d", ref], cwd)
    return rc == 0


def create_plan_baseline(
    version: str = "v1",
    cwd: Optional[Path] = None,
) -> Optional[str]:
    """Create a plan baseline checkpoint.

    Args:
        version: Plan version (e.g., "v1").
        cwd: Working directory.

    Returns:
        Full ref path if successful, None otherwise.
    """
    return create_checkpoint(f"plan_{version}_baseline", cwd=cwd)


def create_task_checkpoint(
    task_id: str,
    status: str = "success",
    cwd: Optional[Path] = None,
) -> Optional[str]:
    """Create a per-task success checkpoint.

    Args:
        task_id: Task identifier.
        status: Task status (success/failure).
        cwd: Working directory.

    Returns:
        Full ref path if successful, None otherwise.
    """
    return create_checkpoint(f"{task_id}_{status}", cwd=cwd)
