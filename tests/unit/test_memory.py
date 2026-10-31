"""Tests for CUPOLA memory admission."""

from __future__ import annotations

from cupola.config import ProfileSpec
from cupola.gguf import GGUFInfo
from cupola.memory import check_admission, estimate_kv_bytes, read_vram_free


def test_estimate_kv_bytes():
    """KV cache estimation returns positive value."""
    kv = estimate_kv_bytes(
        ctx_size=4096,
        n_layers=40,
        n_kv_heads=8,
        head_dim=128,
        kv_quant_bytes=1.0,
    )
    assert kv > 0
    # 4096 * 2 * 40 * 8 * 128 = 335,544,320 bytes = 320 MB
    assert kv == 4096 * 2 * 40 * 8 * 128


def test_estimate_kv_bytes_quantized():
    """Quantized KV cache uses less memory."""
    kv_f16 = estimate_kv_bytes(4096, 40, 8, 128, 1.0)
    kv_q4 = estimate_kv_bytes(4096, 40, 8, 128, 0.5)
    assert kv_q4 == kv_f16 // 2


def test_read_vram_free():
    """VRAM reading may or may not succeed depending on system."""
    free = read_vram_free()
    if free is not None:
        assert free > 0


def test_admission_insufficient_vram():
    """Admission fails when weights exceed available VRAM."""
    profile = ProfileSpec(
        alias="test",
        weights_path=None,
        ctx_size=4096,
        reserve_bytes=512 * 1024 * 1024,
    )
    # Simulate a huge model with no real file
    gguf = GGUFInfo(
        magic=b"GGUF", version=3, tensor_count=1, kv_count=0,
        kv={}, arch="llama", params=70_000_000_000,
        n_layers=80, n_heads=64, n_kv_heads=8,
        embedding_dim=8192, context_length=8192,
        total_bytes=40 * 1024**3,  # 40 GB model
    )
    result = check_admission(profile, gguf)
    # Should fail: 40GB model > any available VRAM
    assert result.admitted is False
    assert result.required_bytes > 0