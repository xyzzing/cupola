"""Tests for CUPOLA hardware probing."""

from __future__ import annotations

from cupola.hardware import (
    check_llama_server,
    get_cpu_info,
    get_gpu_info,
    get_kernel,
    get_os_release,
    get_ram_info,
    get_rocm_version,
    get_vram_info,
    probe,
)


def test_get_kernel():
    kernel = get_kernel()
    assert kernel
    assert "linux" in kernel.lower() or "." in kernel


def test_get_os_release():
    release = get_os_release()
    assert release
    assert release != "unknown"


def test_get_cpu_info():
    model, cores, threads = get_cpu_info()
    assert model
    assert model != "unknown"
    assert cores >= 1
    assert threads >= cores


def test_get_ram_info():
    total, available = get_ram_info()
    assert total > 0
    assert 0 <= available <= total


def test_get_gpu_info():
    """GPU may or may not be detected depending on system."""
    name, pci = get_gpu_info()
    # On the test machine, GPU should be detected
    if name:
        assert pci
        assert ":" in pci  # PCI identity format


def test_get_rocm_version():
    """ROCm version may or may not be available."""
    version = get_rocm_version()
    if version:
        assert version


def test_get_vram_info():
    """VRAM info may or may not be available."""
    total, used = get_vram_info()
    if total is not None:
        assert total > 0
        if used is not None:
            assert 0 <= used <= total


def test_check_llama_server():
    """llama-server may or may not be running."""
    running, port, version = check_llama_server()
    if running and port:
        assert 1 <= port <= 65535


def test_probe():
    """Full probe should return HardwareInfo without errors."""
    info = probe()
    assert info.kernel
    assert info.os_release
    assert info.cpu_model
    assert info.cpu_cores >= 1
    assert info.ram_total_gb > 0
    # errors list may contain warnings, but probe should not crash
