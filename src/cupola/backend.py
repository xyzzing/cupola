"""Backend version detection and feature probing for llama-server."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cupola.config import DEFAULT_CACHE_DIR


@dataclass
class BackendFeatures:
    """Detected features of a llama-server binary."""
    version: Optional[str] = None
    version_hash: Optional[str] = None  # Git commit hash if available
    flash_attn: Optional[bool] = None
    cache_types: list[str] = field(default_factory=list)
    rocm_optimizations: Optional[bool] = None
    mamba_support: Optional[bool] = None
    speculative_decoding: Optional[bool] = None
    detected_at: float = 0.0
    binary_hash: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "version_hash": self.version_hash,
            "flash_attn": self.flash_attn,
            "cache_types": self.cache_types,
            "rocm_optimizations": self.rocm_optimizations,
            "mamba_support": self.mamba_support,
            "speculative_decoding": self.speculative_decoding,
            "detected_at": self.detected_at,
            "binary_hash": self.binary_hash,
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BackendFeatures":
        return cls(
            version=data.get("version"),
            version_hash=data.get("version_hash"),
            flash_attn=data.get("flash_attn"),
            cache_types=data.get("cache_types", []),
            rocm_optimizations=data.get("rocm_optimizations"),
            mamba_support=data.get("mamba_support"),
            speculative_decoding=data.get("speculative_decoding"),
            detected_at=data.get("detected_at", 0.0),
            binary_hash=data.get("binary_hash"),
            errors=data.get("errors", []),
        )


def compute_binary_hash(binary_path: Path) -> Optional[str]:
    """Compute SHA256 hash of the binary for cache invalidation."""
    try:
        sha256 = hashlib.sha256()
        with open(binary_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)  # 1 MiB chunks
                if not chunk:
                    break
                sha256.update(chunk)
        return sha256.hexdigest()
    except OSError:
        return None


def get_version_string(binary_path: Path) -> Optional[str]:
    """Get llama-server version string from --version output."""
    try:
        result = subprocess.run(
            [str(binary_path), "--version"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if result.returncode == 0:
            # llama.cpp version output format: "llama.cpp commit=xxxxx (yyyy)"
            # or just "version: x.y.z"
            output = result.stdout.strip()
            if output:
                return output
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def parse_version_info(version_string: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Parse version string into (version, commit_hash)."""
    if not version_string:
        return None, None

    version = None
    commit = None

    # Try to extract commit hash
    commit_match = re.search(r"commit=([a-f0-9]{7,40})", version_string)
    if commit_match:
        commit = commit_match.group(1)

    # Try to extract version number
    version_match = re.search(r"version:\s*(\d+\.\d+\.\d+)", version_string)
    if version_match:
        version = version_match.group(1)
    elif not commit:
        # If no commit found, the whole string might be the version
        version = version_string.split()[0]

    return version, commit


def test_flash_attn(binary_path: Path) -> Optional[bool]:
    """Test if binary supports --flash-attn flag."""
    try:
        result = subprocess.run(
            [str(binary_path), "--help"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if result.returncode == 0:
            return "--flash-attn" in result.stdout or "flash-attn" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def test_cache_types(binary_path: Path) -> list[str]:
    """Test which KV cache types the binary supports."""
    # Check --help for cache type options
    try:
        result = subprocess.run(
            [str(binary_path), "--help"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if result.returncode == 0:
            output = result.stdout
            supported = []
            # Look for cache type mentions
            for cache_type in ["f16", "f32", "q4_0", "q4_1", "q5_0", "q5_1", "q8_0"]:
                if cache_type in output:
                    supported.append(cache_type)
            return supported
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return []


def test_rocm_optimizations(binary_path: Path) -> Optional[bool]:
    """Test if binary has ROCm-specific optimizations."""
    # Check if binary mentions ROCm in help or version
    try:
        result = subprocess.run(
            [str(binary_path), "--help"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if result.returncode == 0:
            output = result.stdout.lower()
            return "rocm" in output or "hip" in output
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def detect_features(binary_path: Path, force: bool = False) -> BackendFeatures:
    """Detect version and features of a llama-server binary.

    Args:
        binary_path: Path to the llama-server binary
        force: Force re-detection even if cached

    Returns:
        BackendFeatures with detected information
    """
    features = BackendFeatures()
    features.detected_at = time.time()

    if not binary_path.exists():
        features.errors.append(f"Binary not found: {binary_path}")
        return features

    # Compute binary hash for cache invalidation
    features.binary_hash = compute_binary_hash(binary_path)

    # Check cache
    cache_dir = DEFAULT_CACHE_DIR
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Cache dir not writable, skip caching
        cache_file = None
    else:
        cache_file = cache_dir / "backend_features.json"

    if not force and cache_file and cache_file.exists():
        try:
            with open(cache_file, "r") as f:
                cached = json.load(f)
            if cached.get("binary_hash") == features.binary_hash:
                return BackendFeatures.from_dict(cached)
        except (json.JSONDecodeError, OSError):
            pass

    # Run detection
    version_string = get_version_string(binary_path)
    features.version, features.version_hash = parse_version_info(version_string)

    features.flash_attn = test_flash_attn(binary_path)
    features.cache_types = test_cache_types(binary_path)
    features.rocm_optimizations = test_rocm_optimizations(binary_path)

    # Cache results
    if cache_file:
        try:
            with open(cache_file, "w") as f:
                json.dump(features.to_dict(), f, indent=2)
        except OSError:
            pass

    return features


def get_backend_info(profile) -> BackendFeatures:
    """Get backend features for a profile's binary."""
    return detect_features(profile.backend_binary)


def format_backend_info(features: BackendFeatures) -> str:
    """Format backend info for doctor output."""
    lines = []

    if features.version:
        lines.append(f"llama-server version: {features.version}")
    elif features.version_hash:
        lines.append(f"llama-server commit: {features.version_hash}")
    else:
        lines.append("llama-server version: unknown")

    if features.flash_attn is not None:
        status = "✓" if features.flash_attn else "✗"
        lines.append(f"  {status} flash_attn supported")
    else:
        lines.append("  ? flash_attn support: unknown")

    if features.cache_types:
        lines.append(f"  ✓ KV cache types: {', '.join(features.cache_types)}")
    else:
        lines.append("  ? KV cache types: unknown")

    if features.rocm_optimizations is not None:
        status = "✓" if features.rocm_optimizations else "⚠"
        lines.append(f"  {status} ROCm optimizations")
    else:
        lines.append("  ? ROCm optimizations: unknown")

    if features.errors:
        for err in features.errors:
            lines.append(f"  Error: {err}")

    return "\n".join(lines)
