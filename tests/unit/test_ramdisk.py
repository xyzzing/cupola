"""Tests for RAM disk management."""

from pathlib import Path
from unittest.mock import patch

from cupola.ramdisk import (
    RamdiskInfo,
    format_ramdisk_info,
    list_models_on_ramdisk,
    probe_ramdisk,
    remove_from_ramdisk,
    transfer_to_ramdisk,
)


def test_probe_ramdisk_missing():
    """Test probing a non-existent RAM disk path."""
    info = probe_ramdisk(Path("/nonexistent/path"))
    assert info.exists is False
    assert info.quality == "unavailable"
    assert "Path does not exist" in info.errors[0]


def test_probe_ramdisk_exists(tmp_path):
    """Test probing an existing RAM disk path."""
    info = probe_ramdisk(tmp_path)
    assert info.exists is True
    assert info.quality in ("ok", "degraded")
    assert info.total_bytes is not None
    assert info.available_bytes is not None
    assert info.used_bytes is not None


def test_probe_ramdisk_high_usage(tmp_path):
    """Test quality assessment with high usage."""
    # Mock statvfs to return high usage
    class MockStat:
        f_blocks = 1000
        f_bfree = 50  # Only 5% free
        f_bavail = 50
        f_frsize = 4096

    with patch("os.statvfs", return_value=MockStat()):
        info = probe_ramdisk(tmp_path, warn_pct=80.0, critical_pct=90.0)
        assert info.used_pct is not None
        assert info.used_pct >= 90.0
        assert info.quality == "degraded"


def test_transfer_to_ramdisk(tmp_path):
    """Test copying a model to the RAM disk."""
    # Create source file
    source = tmp_path / "source_model.gguf"
    source.write_bytes(b"fake model data" * 1000)

    # Create RAM disk directory
    ramdisk = tmp_path / "ramdisk"
    ramdisk.mkdir()

    # Transfer
    dest = transfer_to_ramdisk(source, ramdisk)
    assert dest.exists()
    assert dest.name == "source_model.gguf"
    assert dest.stat().st_size == source.stat().st_size


def test_transfer_to_ramdisk_custom_name(tmp_path):
    """Test copying with custom name."""
    source = tmp_path / "original.gguf"
    source.write_bytes(b"model data")

    ramdisk = tmp_path / "ramdisk"
    ramdisk.mkdir()

    dest = transfer_to_ramdisk(source, ramdisk, model_name="custom_name.gguf")
    assert dest.name == "custom_name.gguf"


def test_transfer_to_ramdisk_insufficient_space(tmp_path):
    """Test transfer fails when insufficient space."""
    source = tmp_path / "large_model.gguf"
    source.write_bytes(b"x" * 100_000_000)  # 100MB

    ramdisk = tmp_path / "ramdisk"
    ramdisk.mkdir()

    # Mock statvfs to report very little space
    class MockStat:
        f_blocks = 10
        f_bfree = 10
        f_bavail = 10
        f_frsize = 4096

    with patch("os.statvfs", return_value=MockStat()):
        try:
            transfer_to_ramdisk(source, ramdisk)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Insufficient space" in str(e)


def test_remove_from_ramdisk(tmp_path):
    """Test removing a model from RAM disk."""
    model = tmp_path / "model.gguf"
    model.write_bytes(b"model data")

    remove_from_ramdisk(model)
    assert not model.exists()


def test_remove_from_ramdisk_missing(tmp_path):
    """Test removing a non-existent model."""
    model = tmp_path / "missing.gguf"
    try:
        remove_from_ramdisk(model)
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError:
        pass


def test_list_models_on_ramdisk(tmp_path):
    """Test listing models on RAM disk."""
    # Create some model files
    model1 = tmp_path / "model1.gguf"
    model2 = tmp_path / "model2.gguf"
    other = tmp_path / "not_a_model.txt"

    model1.write_bytes(b"data1")
    model2.write_bytes(b"data2")
    other.write_bytes(b"data3")

    models = list_models_on_ramdisk(tmp_path)
    assert "model1.gguf" in models
    assert "model2.gguf" in models
    assert "not_a_model.txt" not in models


def test_list_models_on_missing_path(tmp_path):
    """Test listing models on non-existent path."""
    models = list_models_on_ramdisk(tmp_path / "nonexistent")
    assert models == []


def test_format_ramdisk_info_ok(tmp_path):
    """Test formatting RAM disk info for ok status."""
    info = probe_ramdisk(tmp_path)
    formatted = format_ramdisk_info(info)
    assert "RAM disk:" in formatted
    assert "Quality: ok" in formatted or "Quality: degraded" in formatted


def test_format_ramdisk_info_unavailable():
    """Test formatting RAM disk info for unavailable status."""
    info = RamdiskInfo(
        path="/nonexistent",
        exists=False,
        quality="unavailable",
        errors=["Path does not exist"],
    )
    formatted = format_ramdisk_info(info)
    assert "unavailable" in formatted


def test_ramdisk_info_to_dict():
    """Test RamdiskInfo serialization."""
    info = RamdiskInfo(
        path="/mnt/ai_ramdisk",
        exists=True,
        total_bytes=8589934592,
        used_bytes=2147483648,
        available_bytes=6442450944,
        used_pct=25.0,
        quality="ok",
        models=["model.gguf"],
    )

    data = info.to_dict()
    assert data["path"] == "/mnt/ai_ramdisk"
    assert data["exists"] is True
    assert data["total_gb"] == 8.0
    assert data["used_gb"] == 2.0
    assert data["available_gb"] == 6.0
    assert data["used_pct"] == 25.0
    assert data["quality"] == "ok"
    assert data["models"] == ["model.gguf"]
