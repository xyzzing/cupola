"""Tests for checkpoint management."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from cupola.checkpoints import (
    CHECKPOINT_REF_PREFIX,
    create_checkpoint,
    create_plan_baseline,
    create_task_checkpoint,
    delete_checkpoint,
    list_checkpoints,
    restore_checkpoint,
)


def _init_git_repo(path: Path) -> None:
    """Initialize a test git repo."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test User"],
        check=True, capture_output=True
    )
    # Create initial commit
    (path / "file.txt").write_text("initial")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True
    )


def test_create_checkpoint():
    """Create a checkpoint ref."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        _init_git_repo(repo)

        ref = create_checkpoint("test-checkpoint", cwd=repo)
        assert ref is not None
        assert ref == f"{CHECKPOINT_REF_PREFIX}test-checkpoint"

        # Verify ref exists
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", ref],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0


def test_list_checkpoints():
    """List all checkpoint refs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        _init_git_repo(repo)

        create_checkpoint("checkpoint-1", cwd=repo)
        create_checkpoint("checkpoint-2", cwd=repo)

        checkpoints = list_checkpoints(cwd=repo)
        assert len(checkpoints) == 2
        names = [cp.name for cp in checkpoints]
        assert "checkpoint-1" in names
        assert "checkpoint-2" in names


def test_restore_checkpoint():
    """Restore workspace to checkpoint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        _init_git_repo(repo)

        # Create checkpoint
        create_checkpoint("before-change", cwd=repo)

        # Make a change
        (repo / "file.txt").write_text("modified")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "change"],
            check=True, capture_output=True
        )

        # Restore to checkpoint
        success = restore_checkpoint("before-change", cwd=repo)
        assert success is True

        # Verify content is restored
        content = (repo / "file.txt").read_text()
        assert content == "initial"


def test_delete_checkpoint():
    """Delete a checkpoint ref."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        _init_git_repo(repo)

        create_checkpoint("to-delete", cwd=repo)

        # Verify it exists
        checkpoints = list_checkpoints(cwd=repo)
        assert len(checkpoints) == 1

        # Delete it
        success = delete_checkpoint("to-delete", cwd=repo)
        assert success is True

        # Verify it's gone
        checkpoints = list_checkpoints(cwd=repo)
        assert len(checkpoints) == 0


def test_create_plan_baseline():
    """Create plan baseline checkpoint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        _init_git_repo(repo)

        ref = create_plan_baseline("v1", cwd=repo)
        assert ref is not None
        assert "plan_v1_baseline" in ref


def test_create_task_checkpoint():
    """Create per-task success checkpoint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        _init_git_repo(repo)

        ref = create_task_checkpoint("task-1", "success", cwd=repo)
        assert ref is not None
        assert "task-1_success" in ref


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
