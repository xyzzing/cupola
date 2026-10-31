"""llama.cpp version management and build flag configuration.

Tracks installed versions, knows when to update, and provides
the correct build flags for each version. Manages version
transitions with automatic backup.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cupola.config import DEFAULT_STATE_DIR


@dataclass
class LlamaVersionInfo:
    """Information about a llama.cpp version."""
    version: str
    commit: Optional[str] = None
    installed_at: Optional[float] = None
    binary_path: Optional[Path] = None


@dataclass
class BuildFlags:
    """Build flags for a specific llama.cpp version."""
    cmake_flags: list[str]
    description: str


VERSION_FLAG_MAP = {
    "b4400": BuildFlags(
        cmake_flags=["-DGGML_HIPBLAS=ON", "-DGGML_CUDA=OFF"],
        description="ROCm support via GGML_HIPBLAS"
    ),
    "b4500": BuildFlags(
        cmake_flags=["-DGGML_HIPBLAS=ON", "-DGGML_CUDA=OFF"],
        description="ROCm support via GGML_HIPBLAS"
    ),
    "b4600": BuildFlags(
        cmake_flags=["-DGGML_HIPBLAS=ON", "-DGGML_CUDA=OFF"],
        description="ROCm support via GGML_HIPBLAS"
    ),
    "b4700": BuildFlags(
        cmake_flags=["-DGGML_HIPBLAS=ON", "-DGGML_CUDA=OFF"],
        description="ROCm support via GGML_HIPBLAS"
    ),
    "b4800": BuildFlags(
        cmake_flags=["-DGGML_HIPBLAS=ON", "-DGGML_CUDA=OFF"],
        description="ROCm support via GGML_HIPBLAS"
    ),
    "b4900": BuildFlags(
        cmake_flags=["-DGGML_HIPBLAS=ON", "-DGGML_CUDA=OFF"],
        description="ROCm support via GGML_HIPBLAS"
    ),
    "b5000": BuildFlags(
        cmake_flags=["-DGGML_HIPBLAS=ON", "-DGGML_CUDA=OFF"],
        description="ROCm support via GGML_HIPBLAS"
    ),
}

# Latest known version
LATEST_KNOWN_VERSION = "b5000"


def get_version_flags(version: str) -> BuildFlags:
    """Get build flags for a specific version."""
    # Try exact match first
    if version in VERSION_FLAG_MAP:
        return VERSION_FLAG_MAP[version]

    # Try to find closest version
    for known_version in sorted(VERSION_FLAG_MAP.keys(), reverse=True):
        try:
            if int(version.replace("b", "")) >= int(known_version.replace("b", "")):
                return VERSION_FLAG_MAP[known_version]
        except ValueError:
            continue

    # Fallback to latest known
    return VERSION_FLAG_MAP[LATEST_KNOWN_VERSION]


class LlamaVersionManager:
    """Manages llama.cpp versions and updates."""

    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = state_dir or DEFAULT_STATE_DIR
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.version_file = self.state_dir / "llama_version.json"
        self.install_dir = Path.home() / "llama.cpp"
        self.build_dir = self.install_dir / "build"

    def get_installed_version(self) -> Optional[LlamaVersionInfo]:
        """Get the currently installed version."""
        # Try to read from version file
        if self.version_file.exists():
            try:
                with open(self.version_file, "r") as f:
                    data = json.load(f)
                    return LlamaVersionInfo(
                        version=data["version"],
                        commit=data.get("commit"),
                        installed_at=data.get("installed_at"),
                    )
            except (json.JSONDecodeError, KeyError):
                pass

        # Try to detect from binary
        binary = self.build_dir / "bin" / "llama-server"
        if binary.exists():
            try:
                result = subprocess.run(
                    [str(binary), "--version"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    version = result.stdout.strip()
                    return LlamaVersionInfo(version=version, binary_path=binary)
            except (subprocess.TimeoutExpired, OSError):
                pass

        return None

    def get_latest_version(self) -> str:
        """Get the latest known version."""
        return LATEST_KNOWN_VERSION

    def needs_update(self) -> bool:
        """Check if an update is needed."""
        installed = self.get_installed_version()
        if installed is None:
            return True

        try:
            installed_num = int(installed.version.replace("b", ""))
            latest_num = int(self.get_latest_version().replace("b", ""))
            return installed_num < latest_num
        except ValueError:
            return True

    def backup_current_version(self) -> Optional[Path]:
        """Backup the current llama.cpp installation."""
        installed = self.get_installed_version()
        if installed is None:
            return None

        backup_dir = self.state_dir / f"llama_backup_{int(time.time())}"
        backup_dir.mkdir(exist_ok=True)

        # Copy binary
        binary = self.build_dir / "bin" / "llama-server"
        if binary.exists():
            shutil.copy2(binary, backup_dir / "llama-server")

        # Save version info
        with open(backup_dir / "version.json", "w") as f:
            json.dump({
                "version": installed.version,
                "commit": installed.commit,
                "backed_up_at": time.time(),
            }, f, indent=2)

        return backup_dir

    def restore_version(self, backup_dir: Path) -> bool:
        """Restore a backed-up version."""
        try:
            # Read version info
            with open(backup_dir / "version.json", "r") as f:
                data = json.load(f)

            # Restore binary
            binary = backup_dir / "llama-server"
            if binary.exists():
                self.build_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(binary, self.build_dir / "bin" / "llama-server")

            # Update version file
            with open(self.version_file, "w") as f:
                json.dump({
                    "version": data["version"],
                    "commit": data.get("commit"),
                    "installed_at": data.get("backed_up_at"),
                    "restored_at": time.time(),
                }, f, indent=2)

            return True
        except (OSError, json.JSONDecodeError) as e:
            print(f"Failed to restore version: {e}")
            return False

    def update(self) -> bool:
        """Update llama.cpp to the latest version."""
        print("Updating llama.cpp...")

        # Backup current version
        backup = self.backup_current_version()
        if backup:
            print(f"Backed up current version to: {backup}")

        # Pull latest code
        if self.install_dir.exists():
            try:
                subprocess.run(
                    ["git", "pull"],
                    cwd=self.install_dir,
                    check=True
                )
            except subprocess.CalledProcessError as e:
                print(f"Failed to pull latest code: {e}")
                return False
        else:
            try:
                subprocess.run(
                    ["git", "clone", "https://github.com/ggerganov/llama.cpp.git",  # noqa: E501
                     str(self.install_dir)],
                    check=True
                )
            except subprocess.CalledProcessError as e:
                print(f"Failed to clone repo: {e}")
                return False

        # Get build flags for new version
        flags = get_version_flags(self.get_latest_version())
        print(f"Building with flags: {' '.join(flags.cmake_flags)}")

        # Build
        try:
            subprocess.run(
                ["cmake", "-B", str(self.build_dir), "-DCMAKE_BUILD_TYPE=Release"] + flags.cmake_flags,  # noqa: E501
                cwd=self.install_dir,
                check=True
            )
            subprocess.run(
                ["cmake", "--build", str(self.build_dir), "-j", str(__import__("os").cpu_count())],
                cwd=self.install_dir,
                check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"Failed to build: {e}")
            # Try to restore backup
            if backup:
                print("Restoring previous version...")
                if self.restore_version(backup):
                    print("Previous version restored successfully.")
                else:
                    print("Failed to restore previous version.")
            return False

        # Update version file
        new_version = self.get_latest_version()
        with open(self.version_file, "w") as f:
            json.dump({
                "version": new_version,
                "installed_at": time.time(),
            }, f, indent=2)

        print(f"Updated to version {new_version}")
        return True

    def get_build_flags(self) -> BuildFlags:
        """Get build flags for the current version."""
        installed = self.get_installed_version()
        if installed:
            return get_version_flags(installed.version)
        return get_version_flags(self.get_latest_version())