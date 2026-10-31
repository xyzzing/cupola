"""Backend update management for llama.cpp."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cupola.discovery import find_llama_cpp_source


@dataclass
class UpdateResult:
    """Result of a backend update."""
    success: bool = False
    previous_version: Optional[str] = None
    new_version: Optional[str] = None
    target_version: Optional[str] = None
    build_time_seconds: float = 0.0
    error: Optional[str] = None
    steps: list[str] = None  # Will be initialized in __post_init__

    def __post_init__(self):
        if self.steps is None:
            self.steps = []


def get_llama_cpp_source_dir() -> Optional[Path]:
    """Find llama.cpp source directory using discovery."""
    return find_llama_cpp_source()


def get_build_dir(source_dir: Path) -> Path:
    """Get build directory for source."""
    return source_dir / "build"


def run_git_command(source_dir: Path, args: list[str], timeout: float = 60.0) -> Optional[str]:
    """Run a git command in the source directory."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(source_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def get_current_commit(source_dir: Path) -> Optional[str]:
    """Get current git commit hash."""
    return run_git_command(source_dir, ["rev-parse", "--short", "HEAD"])


def get_current_version(source_dir: Path) -> Optional[str]:
    """Get current version from git tag or commit."""
    # Try to get version from git describe
    version = run_git_command(source_dir, ["describe", "--tags", "--always"])
    if version:
        return version
    # Fall back to commit hash
    return get_current_commit(source_dir)


def update_backend(
    target_version: Optional[str] = None,
    recompile: bool = False,
    verbose: bool = True,
) -> UpdateResult:
    """Update llama.cpp backend.

    Args:
        target_version: Specific version/tag to update to (None = latest)
        recompile: Force recompile even if already at target version
        verbose: Print progress messages

    Returns:
        UpdateResult with outcome
    """
    result = UpdateResult()

    source_dir = get_llama_cpp_source_dir()
    if not source_dir:
        result.error = "llama.cpp source directory not found"
        return result

    if verbose:
        print(f"Updating llama.cpp from {source_dir}")

    # Get current version
    current_version = get_current_version(source_dir)
    result.previous_version = current_version
    result.target_version = target_version or "latest"

    if verbose:
        print(f"Current version: {current_version}")
        print(f"Target version: {result.target_version}")

    # Check if already at target
    if target_version and current_version == target_version and not recompile:
        result.success = True
        result.new_version = current_version
        if verbose:
            print("Already at target version, no update needed.")
        return result

    # Step 1: Pull latest or checkout specific version
    if target_version:
        if verbose:
            print(f"Checking out {target_version}...")
        commit = run_git_command(
            source_dir, ["rev-parse", "--verify", target_version], timeout=30.0
        )
        if not commit:
            # Try as branch/tag
            checkout_result = run_git_command(source_dir, ["checkout", target_version])
            if not checkout_result:
                result.error = f"Failed to checkout {target_version}"
                return result
        else:
            checkout_result = run_git_command(source_dir, ["checkout", commit])
            if not checkout_result:
                result.error = f"Failed to checkout commit {commit}"
                return result
    else:
        if verbose:
            print("Pulling latest changes...")
        pull_result = run_git_command(source_dir, ["pull", "--ff-only"])
        if not pull_result:
            # Try without ff-only
            pull_result = run_git_command(source_dir, ["pull"])
            if not pull_result:
                result.error = "Failed to pull latest changes"
                return result

    # Get new version
    new_version = get_current_version(source_dir)
    result.new_version = new_version

    if verbose:
        print(f"Updated to version: {new_version}")

    # Step 2: Build
    build_dir = get_build_dir(source_dir)
    if verbose:
        print("Building llama.cpp...")

    build_start = time.time()

    # Configure
    if verbose:
        print("  Configuring...")
    configure_result = subprocess.run(
        ["cmake", "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release"],
        cwd=str(source_dir),
        capture_output=True,
        text=True,
        timeout=300.0,
    )
    if configure_result.returncode != 0:
        result.error = f"CMake configuration failed: {configure_result.stderr.strip()[-500:]}"
        return result

    # Build
    if verbose:
        print("  Compiling...")
    build_result = subprocess.run(
        ["cmake", "--build", str(build_dir), "-j", "6"],
        cwd=str(source_dir),
        capture_output=True,
        text=True,
        timeout=1800.0,  # 30 minute timeout
    )
    build_time = time.time() - build_start
    result.build_time_seconds = build_time

    if build_result.returncode != 0:
        result.error = f"Build failed: {build_result.stderr.strip()[-500:]}"
        return result

    if verbose:
        print(f"  Build completed in {build_time:.0f}s")

    # Step 3: Verify build
    llama_server = build_dir / "bin" / "llama-server"
    if not llama_server.exists():
        result.error = "llama-server binary not found after build"
        return result

    # Verify binary works
    if verbose:
        print("  Verifying build...")
    verify_result = subprocess.run(
        [str(llama_server), "--version"],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if verify_result.returncode != 0:
        result.error = "Built binary failed version check"
        return result

    result.success = True

    if verbose:
        print(f"Backend updated successfully to {new_version}")
        print(f"Build time: {build_time:.0f}s")

    return result


def format_update_result(result: UpdateResult) -> str:
    """Format update result for display."""
    lines = []

    if result.success:
        lines.append("Backend update completed successfully!")
        if result.previous_version and result.new_version:
            if result.previous_version != result.new_version:
                lines.append(f"  Previous: {result.previous_version}")
                lines.append(f"  New:      {result.new_version}")
        if result.build_time_seconds > 0:
            lines.append(f"  Build time: {result.build_time_seconds:.0f}s")
    else:
        lines.append("Backend update failed!")
        if result.error:
            lines.append(f"  Error: {result.error}")

    return "\n".join(lines)
