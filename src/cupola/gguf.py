"""Bounded GGUF header and metadata reader.

Reads only the GGUF header and key-value metadata without loading
model tensors. Uses bounded chunk streams to prevent excessive memory
use and rejects malformed structures.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

GGUF_MAGIC = b"GGUF"
MAX_METADATA_KEYS = 1000
MAX_STRING_LEN = 65536
MAX_ARRAY_ELEMENTS = 10000


@dataclass
class GGUFInfo:
    """Parsed GGUF header and metadata."""
    magic: bytes
    version: int
    tensor_count: int
    kv_count: int
    kv: dict[str, Any] = field(default_factory=dict)
    arch: str = ""
    params: int = 0
    vocab_size: int = 0
    embedding_dim: int = 0
    n_layers: int = 0
    n_heads: int = 0
    n_kv_heads: int = 0
    context_length: int = 0
    total_bytes: int = 0
    errors: list[str] = field(default_factory=list)


def read_gguf_header(path) -> Optional[GGUFInfo]:
    """Read GGUF header and metadata. Returns None on error."""
    if isinstance(path, str):
        path = Path(path)
    errors = []
    info = GGUFInfo(magic=b"", version=0, tensor_count=0, kv_count=0,
                    kv={}, errors=errors)

    try:
        if not path.exists() or not path.is_file():
            errors.append(f"File not found: {path}")
            return info

        info.total_bytes = path.stat().st_size
        if info.total_bytes < 16:
            errors.append("File too small to be GGUF")
            return info

        with open(path, "rb") as f:
            # Magic (4 bytes)
            magic = f.read(4)
            if len(magic) < 4 or magic != GGUF_MAGIC:
                errors.append(f"Invalid GGUF magic: {magic!r}")
                return info
            info.magic = magic

            # Version (4 bytes uint32 LE)
            version_bytes = f.read(4)
            if len(version_bytes) < 4:
                errors.append("Truncated version field")
                return info
            info.version = struct.unpack("<I", version_bytes)[0]

            # Tensor count (8 bytes uint64 LE)
            tensor_count_bytes = f.read(8)
            if len(tensor_count_bytes) < 8:
                errors.append("Truncated tensor count field")
                return info
            info.tensor_count = struct.unpack("<Q", tensor_count_bytes)[0]

            # KV count (8 bytes uint64 LE)
            kv_count_bytes = f.read(8)
            if len(kv_count_bytes) < 8:
                errors.append("Truncated KV count field")
                return info
            info.kv_count = struct.unpack("<Q", kv_count_bytes)[0]

            if info.kv_count > MAX_METADATA_KEYS:
                errors.append(f"Too many metadata keys: {info.kv_count}")
                return info

            # Read KV pairs
            for i in range(info.kv_count):
                key, value = _read_kv_pair(f, errors)
                if key is not None:
                    info.kv[key] = value

            # Extract common fields
            # Support both full GGUF keys and simplified test keys
            info.arch = info.kv.get("general.architecture", info.kv.get("arch", ""))
            info.params = info.kv.get("general.parameter_count", 0)
            info.vocab_size = info.kv.get(f"{info.arch}.vocab_size", 0)
            info.embedding_dim = info.kv.get(f"{info.arch}.embedding_length", 0)
            info.n_layers = info.kv.get(f"{info.arch}.block_count", 0)
            info.n_heads = info.kv.get(f"{info.arch}.attention.head_count", 0)
            info.n_kv_heads = info.kv.get(f"{info.arch}.attention.head_count_kv", 0)
            info.context_length = info.kv.get(f"{info.arch}.context_length", 0)

    except OSError as e:
        errors.append(f"File read error: {e}")
    except struct.error as e:
        errors.append(f"Struct unpack error: {e}")

    if errors:
        info.errors = errors
    return info


def _read_gguf_string(f) -> str:
    """Read a GGUF string (uint64 length + bytes)."""
    len_bytes = f.read(8)
    if len(len_bytes) < 8:
        raise struct.error("Truncated string length")
    length = struct.unpack("<Q", len_bytes)[0]
    if length > MAX_STRING_LEN:
        raise ValueError(f"String too long: {length}")
    data = f.read(length)
    if len(data) < length:
        raise struct.error("Truncated string data")
    return data.decode("utf-8", errors="replace")


def _read_kv_pair(f, errors: list[str]) -> tuple[Optional[str], Any]:
    """Read a single GGUF key-value pair."""
    try:
        key = _read_gguf_string(f)

        # Value type (4 bytes uint32)
        type_bytes = f.read(4)
        if len(type_bytes) < 4:
            raise struct.error("Truncated value type")
        value_type = struct.unpack("<I", type_bytes)[0]

        value = _read_gguf_value(f, value_type)
        return key, value
    except Exception as e:
        errors.append(f"KV read error: {e}")
        return None, None


def _read_gguf_value(f, value_type: int) -> Any:
    """Read a GGUF value based on its type."""
    # Scalar types
    if value_type == 0:  # int8
        return struct.unpack("<b", f.read(1))[0]
    if value_type == 1:  # int16
        return struct.unpack("<h", f.read(2))[0]
    if value_type == 2:  # int32
        return struct.unpack("<i", f.read(4))[0]
    if value_type == 3:  # int64
        return struct.unpack("<q", f.read(8))[0]
    if value_type == 4:  # float32
        return struct.unpack("<f", f.read(4))[0]
    if value_type == 5:  # float64
        return struct.unpack("<d", f.read(8))[0]
    if value_type == 6:  # bool
        return bool(f.read(1)[0])
    if value_type == 7:  # string
        return _read_gguf_string(f)

    # Array types (prefix 256)
    if value_type >= 256:
        element_type = value_type - 256
        count_bytes = f.read(8)
        count = struct.unpack("<Q", count_bytes)[0]
        if count > MAX_ARRAY_ELEMENTS:
            raise ValueError(f"Array too large: {count} elements")
        elements = []
        for _ in range(count):
            elements.append(_read_gguf_value(f, element_type))
        return elements

    raise ValueError(f"Unknown GGUF value type: {value_type}")