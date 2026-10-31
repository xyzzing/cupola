"""RAM disk management for Cupola.

Handles probing, model transfer, and management of the RAM disk
used for fast model loading by llama-server.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class RamdiskInfo:
    """RAM disk status information."""

    path: str
    exists: bool = False
    total_bytes: Optional[int] = None
    used_bytes: Optional[int] = None
    available_bytes: Optional[int] = None
    used_pct: Optional[float] = None
    quality: str = "unavailable"  # ok | degraded | unavailable
    errors: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = {
            "path": self.path,
            "exists": self.exists,
            "quality": self.quality,
            "errors": self.errors,
        }
        if self.total_bytes is not None:
            result["total_bytes"] = self.total_bytes
            result["total_gb"] = round(self.total_bytes / (1024**3), 1)
        if self.used_bytes is not None:
            result["used_bytes"] = self.used_bytes
            result["used_gb"] = round(self.used_bytes / (1024**3), 1)
        if self.available_bytes is not None:
            result["available_bytes"] = self.available_bytes
            result["available_gb"] = round(self.available_bytes / (1024**3), 1)
        if self.used_pct is not None:
            result["used_pct"] = round(self.used_pct, 1)
        result["models"] = self.models
        return result


def probe_ramdisk(
    path: Path,
    warn_pct: float = 80.0,
    critical_pct: float = 90.0,
) -> RamdiskInfo:
    """Probe RAM disk status using os.statvfs().

    Returns RamdiskInfo with quality assessment.
    """
    info = RamdiskInfo(path=str(path))

    if not path.exists():
        info.quality = "unavailable"
        info.errors.append(f"Path does not exist: {path}")
        return info

    if not path.is_dir():
        info.quality = "degraded"
        info.errors.append(f"Path is not a directory: {path}")
        return info

    info.exists = True

    try:
        stat = os.statvfs(path)
        total_bytes = stat.f_blocks * stat.f_frsize
        available_bytes = stat.f_bavail * stat.f_frsize
        used_bytes = total_bytes - (stat.f_bfree * stat.f_frsize)

        info.total_bytes = total_bytes
        info.available_bytes = available_bytes
        info.used_bytes = used_bytes

        if total_bytes > 0:
            info.used_pct = (used_bytes / total_bytes) * 100.0

        # List models on RAM disk
        try:
            for entry in path.iterdir():
                if entry.is_file() and entry.suffix == ".gguf":
                    info.models.append(entry.name)
        except OSError as e:
            info.errors.append(f"Failed to list models: {e}")

        # Quality assessment
        if info.used_pct is not None and info.used_pct >= critical_pct:
            info.quality = "degraded"
            info.errors.append(
                f"Usage {info.used_pct:.1f}% >= critical threshold {critical_pct}%"
            )
        elif info.used_pct is not None and info.used_pct >= warn_pct:
            info.quality = "degraded"
            info.errors.append(
                f"Usage {info.used_pct:.1f}% >= warning threshold {warn_pct}%"
            )
        else:
            info.quality = "ok"

    except OSError as e:
        info.quality = "degraded"
        info.errors.append(f"Failed to statvfs: {e}")

    return info


def transfer_to_ramdisk(
    source: Path,
    ramdisk_path: Path,
    model_name: Optional[str] = None,
) -> Path:
    """Copy model to RAM disk with size validation.

    Args:
        source: Source model file path
        ramdisk_path: RAM disk directory path
        model_name: Optional custom name for the model on RAM disk

    Returns:
        Path to the model on the RAM disk

    Raises:
        FileNotFoundError: If source doesn't exist
        ValueError: If insufficient space on RAM disk
        OSError: If copy fails
    """
    if not source.exists():
        raise FileNotFoundError(f"Source model not found: {source}")

    if not ramdisk_path.exists():
        raise FileNotFoundError(f"RAM disk path not found: {ramdisk_path}")

    source_size = source.stat().st_size

    # Check available space
    stat = os.statvfs(ramdisk_path)
    available_bytes = stat.f_bavail * stat.f_frsize

    if available_bytes < source_size:
        raise ValueError(
            f"Insufficient space on RAM disk. "
            f"Need {source_size / (1024**3):.1f} GB, "
            f"have {available_bytes / (1024**3):.1f} GB"
        )

    # Determine destination name
    if model_name is None:
        model_name = source.name

    dest = ramdisk_path / model_name

    # Copy with metadata preservation
    shutil.copy2(source, dest)

    # Verify copy completed successfully
    dest_size = dest.stat().st_size
    if dest_size != source_size:
        dest.unlink()
        raise OSError(
            f"Copy verification failed. "
            f"Source: {source_size} bytes, "
            f"Destination: {dest_size} bytes"
        )

    return dest


def transfer_from_ramdisk(
    ramdisk_path: Path,
    destination: Path,
) -> None:
    """Copy model back from RAM disk to persistent storage.

    Args:
        ramdisk_path: Path to model on RAM disk
        destination: Destination path for persistent storage

    Raises:
        FileNotFoundError: If source doesn't exist
        OSError: If copy fails
    """
    if not ramdisk_path.exists():
        raise FileNotFoundError(f"Model not found on RAM disk: {ramdisk_path}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ramdisk_path, destination)


def remove_from_ramdisk(ramdisk_path: Path) -> None:
    """Remove model from RAM disk.

    Args:
        ramdisk_path: Path to model on RAM disk

    Raises:
        FileNotFoundError: If model doesn't exist
    """
    if not ramdisk_path.exists():
        raise FileNotFoundError(f"Model not found on RAM disk: {ramdisk_path}")

    ramdisk_path.unlink()


def list_models_on_ramdisk(ramdisk_path: Path) -> list[str]:
    """List model files currently on the RAM disk.

    Args:
        ramdisk_path: RAM disk directory path

    Returns:
        List of .gguf filenames on the RAM disk
    """
    if not ramdisk_path.exists():
        return []

    models = []
    try:
        for entry in ramdisk_path.iterdir():
            if entry.is_file() and entry.suffix == ".gguf":
                models.append(entry.name)
    except OSError:
        pass

    return models


def format_ramdisk_info(info: RamdiskInfo) -> str:
    """Format RAM disk info for human-readable output."""
    lines = [f"  RAM disk: {info.path}"]

    if not info.exists:
        lines.append(f"    Status: unavailable ({', '.join(info.errors)})")
        return "\n".join(lines)

    if info.total_bytes is not None:
        total_gb = info.total_bytes / (1024**3)
        used_gb = (info.used_bytes or 0) / (1024**3)
        avail_gb = (info.available_bytes or 0) / (1024**3)

        lines.append(f"    Size: {total_gb:.1f} GB")
        lines.append(f"    Used: {used_gb:.1f} GB")
        lines.append(f"    Available: {avail_gb:.1f} GB")

        if info.used_pct is not None:
            lines.append(f"    Usage: {info.used_pct:.1f}%")

        lines.append(f"    Quality: {info.quality}")

    if info.models:
        lines.append(f"    Models: {len(info.models)}")
        for model in info.models:
            lines.append(f"      - {model}")

    if info.errors:
        for error in info.errors:
            lines.append(f"    Error: {error}")

    return "\n".join(lines)
