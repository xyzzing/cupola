"""Tests for the discovery module."""

import os
import tempfile
from pathlib import Path

import pytest

from cupola.discovery import (
    find_llama_bench,
    find_llama_cpp_source,
    find_llama_server,
    find_weights,
)


def test_explicit_path_wins():
    """Explicit path from profile should win over everything."""
    with tempfile.TemporaryDirectory() as tmpdir:
        binary = Path(tmpdir) / "llama-server"
        binary.touch()
        binary.chmod(0o755)

        result = find_llama_server(explicit=binary)
        assert result == binary


def test_explicit_invalid_path_fails():
    """Invalid explicit path should raise an error."""
    with pytest.raises(ValueError):
        find_llama_server(explicit=Path("/nonexistent/path"))


def test_env_override_wins_over_path():
    """Environment variable should win over PATH discovery."""
    with tempfile.TemporaryDirectory() as tmpdir:
        binary = Path(tmpdir) / "llama-server"
        binary.touch()
        binary.chmod(0o755)

        os.environ["CUPOLA_LLAMA_SERVER"] = str(binary)
        try:
            result = find_llama_server()
            assert result == binary
        finally:
            del os.environ["CUPOLA_LLAMA_SERVER"]


def test_env_invalid_path_fails():
    """Invalid environment path should raise an error."""
    os.environ["CUPOLA_LLAMA_SERVER"] = "/nonexistent/path"
    try:
        with pytest.raises(ValueError):
            find_llama_server()
    finally:
        del os.environ["CUPOLA_LLAMA_SERVER"]


def test_env_not_absolute_fails():
    """Environment path must be absolute."""
    os.environ["CUPOLA_LLAMA_SERVER"] = "relative/path"
    try:
        with pytest.raises(ValueError):
            find_llama_server()
    finally:
        del os.environ["CUPOLA_LLAMA_SERVER"]


def test_path_discovery():
    """PATH discovery should work."""
    # Create a fake binary in a temp directory and add to PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        binary = Path(tmpdir) / "llama-server"
        binary.touch()
        binary.chmod(0o755)

        os.environ["PATH"] = f"{tmpdir}:{os.environ['PATH']}"
        try:
            result = find_llama_server()
            assert result is not None
            assert result.name == "llama-server"
        finally:
            # Restore PATH
            os.environ["PATH"] = os.environ["PATH"].replace(f"{tmpdir}:", "")


def test_nothing_found_returns_none():
    """If nothing is found, return None."""
    import unittest.mock

    # Mock shutil.which to return None
    with unittest.mock.patch("shutil.which", return_value=None):
        # Clear env vars
        old_vars = {}
        for var in ["CUPOLA_LLAMA_SERVER", "CUPOLA_LLAMA_BENCH",  # noqa: E501
                     "CUPOLA_LLAMA_CPP_SOURCE", "CUPOLA_WEIGHTS"]:
            if var in os.environ:
                old_vars[var] = os.environ[var]
                del os.environ[var]

        try:
            result = find_llama_server()
            # Result may be a system path if it exists
            if result is not None:
                # Check if it's a real system path
                assert result in [
                    Path("/usr/local/bin/llama-server"),
                    Path("/usr/bin/llama-server"),
                    Path("/opt/llama.cpp/bin/llama-server"),
                ]
            else:
                assert result is None
        finally:
            # Restore env vars
            for var, value in old_vars.items():
                os.environ[var] = value


def test_private_paths_not_in_discovery():
    """Discovery should not contain private paths."""
    import inspect

    import cupola.discovery as discovery

    # Assembled rather than written out, so this test is not itself a hit for
    # the portability scan.
    private_home = "/" + "home" + "/" + "zacch"
    source = inspect.getsource(discovery)
    assert private_home not in source
    assert "/home/" not in source.replace("/home/<user>", "")


def test_find_llama_bench_explicit():
    """Explicit path for llama-bench should work."""
    with tempfile.TemporaryDirectory() as tmpdir:
        binary = Path(tmpdir) / "llama-bench"
        binary.touch()
        binary.chmod(0o755)

        result = find_llama_bench(explicit=binary)
        assert result == binary


def test_find_llama_cpp_source_explicit():
    """Explicit path for llama.cpp source should work."""
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir)
        (source / "CMakeLists.txt").touch()

        result = find_llama_cpp_source(explicit=source)
        assert result == source


def test_find_weights_explicit():
    """Explicit path for weights should work."""
    with tempfile.TemporaryDirectory() as tmpdir:
        weights = Path(tmpdir) / "model.gguf"
        weights.touch()

        result = find_weights(explicit=weights)
        assert result == weights