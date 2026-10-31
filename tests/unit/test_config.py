"""Tests for CUPOLA configuration loading and validation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cupola.config import CupolaConfig, ProfileSpec, load_config, validate_profile


def test_default_config():
    config = CupolaConfig()
    assert config.profiles == {}
    assert config.default_profile == "coding"


def test_load_missing_config():
    """Loading a non-existent config returns default."""
    config = load_config(Path("/nonexistent/path/config.toml"))
    assert config.profiles == {}


def test_validate_profile_missing_weights():
    profile = ProfileSpec(
        alias="test",
        weights_path=Path("/nonexistent/model.gguf"),
    )
    errors = validate_profile(profile)
    assert any("weights file not found" in e for e in errors)


def test_validate_profile_missing_backend():
    profile = ProfileSpec(
        alias="test",
        weights_path=Path("/tmp/model.gguf"),
        backend_binary=Path("/nonexistent/llama-server"),
    )
    errors = validate_profile(profile)
    assert any("backend binary not found" in e for e in errors)


def test_validate_profile_negative_gpu_layers():
    profile = ProfileSpec(
        alias="test",
        weights_path=Path("/tmp/model.gguf"),
        n_gpu_layers=-1,
    )
    errors = validate_profile(profile)
    assert any("n_gpu_layers" in e for e in errors)


def test_validate_profile_invalid_cache_type():
    profile = ProfileSpec(
        alias="test",
        weights_path=Path("/tmp/model.gguf"),
        cache_type_k="invalid_type",
    )
    errors = validate_profile(profile)
    assert any("invalid cache_type_k" in e for e in errors)


def test_validate_profile_out_of_range_port():
    profile = ProfileSpec(
        alias="test",
        weights_path=Path("/tmp/model.gguf"),
        port=70000,
    )
    errors = validate_profile(profile)
    assert any("port must be 1-65535" in e for e in errors)


def test_validate_profile_valid():
    """A profile with existing files and valid params passes validation."""
    # Create temp files
    with tempfile.NamedTemporaryFile(delete=False, suffix=".gguf") as f:
        f.write(b"dummy")
        weights = Path(f.name)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"dummy")
        backend = Path(f.name)

    try:
        profile = ProfileSpec(
            alias="test",
            weights_path=weights,
            backend_binary=backend,
            n_gpu_layers=63,
            ctx_size=131072,
            cache_type_k="q4_0",
            cache_type_v="q4_0",
            threads=6,
            port=8080,
        )
        errors = validate_profile(profile)
        assert errors == []
    finally:
        weights.unlink()
        backend.unlink()


def test_load_toml_config():
    """Loading a valid TOML config parses correctly."""
    toml_content = """
[profiles.coding]
weights_path = "/tmp/test.gguf"
device = "ROCm0"
n_gpu_layers = 63
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        config_path = Path(f.name)

    try:
        config = load_config(config_path)
        assert "coding" in config.profiles
        assert config.profiles["coding"].alias == "coding"
        assert config.profiles["coding"].weights_path == Path("/tmp/test.gguf")
        assert config.profiles["coding"].n_gpu_layers == 63
    finally:
        config_path.unlink()


def test_malformed_toml_config():
    """Malformed TOML raises an exception."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write("invalid toml [[[")
        config_path = Path(f.name)

    try:
        with pytest.raises(Exception):
            load_config(config_path)
    finally:
        config_path.unlink()
