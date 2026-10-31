"""Tests for CUPOLA GGUF parser."""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from cupola.gguf import read_gguf_header


def _make_gguf_file(path: Path, version: int = 3,
                    tensor_count: int = 10, kv_count: int = 2):
    """Create a minimal valid GGUF file."""
    with open(path, "wb") as f:
        # Magic
        f.write(b"GGUF")
        # Version
        f.write(struct.pack("<I", version))
        # Tensor count
        f.write(struct.pack("<Q", tensor_count))
        # KV count
        f.write(struct.pack("<Q", kv_count))

        # KV pairs
        # Key: general.architecture (string, type 7)
        key = b"general.architecture"
        f.write(struct.pack("<Q", len(key)))
        f.write(key)
        f.write(struct.pack("<I", 7))  # string
        val = b"llama"
        f.write(struct.pack("<Q", len(val)))
        f.write(val)

        # Key: general.parameter_count (int64, type 3)
        key = b"general.parameter_count"
        f.write(struct.pack("<Q", len(key)))
        f.write(key)
        f.write(struct.pack("<I", 3))  # int64
        f.write(struct.pack("<q", 27_000_000_000))


def test_read_valid_gguf():
    """Reading a valid GGUF header succeeds."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".gguf") as f:
        path = Path(f.name)
    _make_gguf_file(path)
    try:
        info = read_gguf_header(path)
        assert info is not None
        assert info.version == 3
        assert info.tensor_count == 10
        assert info.kv_count == 2
        assert info.arch == "llama"
        assert info.params == 27_000_000_000
        assert not info.errors
    finally:
        path.unlink()


def test_read_nonexistent_gguf():
    """Reading a nonexistent GGUF file returns error."""
    info = read_gguf_header(Path("/nonexistent/model.gguf"))
    assert info is not None
    assert info.errors
    assert any("not found" in e.lower() for e in info.errors)


def test_read_invalid_magic():
    """File with wrong magic is rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".gguf") as f:
        f.write(b"XXXX" + b"\x00" * 12)
        path = Path(f.name)
    try:
        info = read_gguf_header(path)
        assert info is not None
        assert info.errors
        assert any("magic" in e.lower() for e in info.errors)
    finally:
        path.unlink()


def test_read_too_small():
    """File smaller than GGUF header is rejected."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".gguf") as f:
        f.write(b"GG")
        path = Path(f.name)
    try:
        info = read_gguf_header(path)
        assert info is not None
        assert info.errors
        assert any("too small" in e.lower() for e in info.errors)
    finally:
        path.unlink()